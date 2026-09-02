"""Yapilandirilmis, maskelenen ve canli okunabilen loglama altyapisi.

Uc sey ayni anda gerekir ve bunlar birbirinin yerine gecmez:

* **Baglam** - her satir hangi correlation/execution/tenant'a ait oldugunu
  tasir (`observability.context`).
* **Maskeleme** - e-posta, IBAN, kart ve secret desenleri log'a **yazilmadan
  once** temizlenir. `log.exception` bir SAP yanit govdesini traceback'e
  dokebilir; disk ve stdout guvenli bolge degildir.
* **Canli tampon** - son N kayit bellekte dairesel tamponda tutulur; operator
  arayuzu servisin loglarini dosyaya erisim olmadan gorebilir.

Bu katman `core.audit` (hash zincirli denetim defteri) ve
`observability.telemetry` (tur/tool metrikleri) yerine GECMEZ. Audit "ne
yapildi" sorusunun hukuki cevabidir; log "ne oldu" sorusunun teshis cevabi.
Biri digerinin kaynagi olarak kullanilamaz.

Not: bu modulun adi `logging`; Python 3 mutlak import kullandigi icin
`import logging` yine standart kutuphaneyi getirir.
"""

from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .context import CONTEXT_FIELDS, current_context
from .masking import mask_text

#: Kendi kurdugumuz handler'lari isaretleriz. Yeniden yapilandirmada yalniz
#: bunlar kaldirilir; pytest'in caplog handler'i veya operatorun ekledigi bir
#: handler'a dokunulmaz.
_MANAGED = "_certaops_managed"

#: Gurultulu kutuphaneler. DEBUG seviyesinde bunlar tam istek/yanit dokebilir.
_NOISY = ("httpx", "httpcore", "pdfminer", "anthropic", "urllib3", "google_genai")

_DEFAULT_TEXT_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DEFAULT_DATE_FORMAT = "%H:%M:%S"

_lock = threading.Lock()
_ring: RingBufferHandler | None = None


def _record_context(record: logging.LogRecord) -> dict[str, str]:
    """Kayda ait baglam alanlari.

    Handler'lar `Logger.handle` icinde **ayni thread ve ayni contextvar
    baglaminda** senkron calisir, dolayisiyla format aninda okunan baglam log
    cagrisi anindaki baglamdir. (Bir `QueueHandler` araya girerse bu varsayim
    bozulur; o gun baglam kaydin uzerine tasinmalidir.)

    `extra=` ile acikca verilen alan contextvar'i EZER: cagiran taraf daha iyi
    biliyordur.
    """
    data = current_context()
    for name in CONTEXT_FIELDS:
        value = getattr(record, name, None)
        if value:
            data[name] = str(value)
    return data


class _MaskingFormatter(logging.Formatter):
    """Mesaji ve traceback'i maskeleyen ortak taban.

    Maskeleme yalnizca **mesaj metnine** uygulanir, formatlanmis satirin
    tamamina degil: ISO tarih damgasi (`2026-08-24`) kart/telefon desenlerine
    yakalanip `***` olurdu ve loglar okunamaz hale gelirdi.
    """

    def __init__(self, *args: Any, mask: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.mask = mask

    def format(self, record: logging.LogRecord) -> str:
        if not self.mask:
            return super().format(record)
        original_msg, original_args = record.msg, record.args
        # `getMessage()` %-formatlamayi burada uygular; sonucu maskeleyip
        # args'i dusururuz. Kayit handler'lar arasinda paylasildigi icin
        # degisiklik hemen geri alinir.
        record.msg = mask_text(record.getMessage())
        record.args = None
        try:
            return super().format(record)
        finally:
            record.msg, record.args = original_msg, original_args

    def formatException(self, ei: Any) -> str:
        text = super().formatException(ei)
        return mask_text(text) if self.mask else text


class TextFormatter(_MaskingFormatter):
    """Insan okunur satir; baglam alanlari sona eklenir."""

    def __init__(self, *, mask: bool = True) -> None:
        super().__init__(_DEFAULT_TEXT_FORMAT, datefmt=_DEFAULT_DATE_FORMAT, mask=mask)

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        context = _record_context(record)
        if not context:
            return line
        suffix = " ".join(f"{key}={value}" for key, value in context.items())
        # Traceback varsa baglam govdenin ustunde kalmali; aksi halde cok
        # satirli bir hata kaydinda correlation ID en alta dusup kaybolur.
        head, sep, tail = line.partition("\n")
        return f"{head} | {suffix}{sep}{tail}"


class JsonFormatter(_MaskingFormatter):
    """Log toplayicilar (Kibana/Cloud Logging) icin tek satir JSON."""

    def __init__(self, *, mask: bool = True) -> None:
        super().__init__(mask=mask)

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": mask_text(message) if self.mask else message,
        }
        payload.update(_record_context(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class RingBufferHandler(logging.Handler):
    """Son N kaydi bellekte tutan dairesel tampon.

    Operator arayuzunun `/logs` ucu buradan okur. Tampon **sinirlidir**:
    sinirsiz bir liste uzun omurlu bir serviste bellegi sessizce tuketirdi.
    Kalici kayit dosya handler'inin ya da toplayicinin isidir.

    `stream` niteligi bilerek yoktur: MCP stdio tasimasinda stdout'a dusen
    handler'lari stderr'e ceviren dongu (`certaops.mcp_server`) bu handler'i
    gormezden gelir.
    """

    def __init__(self, capacity: int = 500, *, mask: bool = True) -> None:
        super().__init__()
        self.capacity = max(1, capacity)
        self.mask = mask
        self._buffer: deque[dict[str, Any]] = deque(maxlen=self.capacity)
        # Handler'in kendi `lock`'u format/emit boyunca tutulur; okuma yolu
        # (`snapshot`) o kilidi beklemesin diye tampona ayri kilit verilir.
        self._buffer_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            entry: dict[str, Any] = {
                "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "levelno": record.levelno,
                "logger": record.name,
                "message": mask_text(message) if self.mask else message,
            }
            entry.update(_record_context(record))
            if record.exc_info:
                text = logging.Formatter().formatException(record.exc_info)
                entry["exception"] = mask_text(text) if self.mask else text
            with self._buffer_lock:
                self._buffer.append(entry)
        except Exception:  # noqa: BLE001 - loglama hicbir kosulda cagirani dusurmez
            self.handleError(record)

    def snapshot(self, *, limit: int = 100, min_level: int = 0) -> list[dict[str, Any]]:
        """En yeniden en eskiye dogru siralanmis kayitlar."""
        with self._buffer_lock:
            rows = list(self._buffer)
        if min_level:
            rows = [row for row in rows if row["levelno"] >= min_level]
        rows.reverse()
        return rows[: max(0, limit)]

    def clear(self) -> None:
        with self._buffer_lock:
            self._buffer.clear()


def _build_formatter(fmt: str, *, mask: bool) -> logging.Formatter:
    return JsonFormatter(mask=mask) if fmt == "json" else TextFormatter(mask=mask)


def _remove_managed(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if getattr(handler, _MANAGED, False):
            root.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()


def configure_logging(
    *,
    level: str = "INFO",
    fmt: str = "text",
    mask: bool = True,
    buffer_size: int = 500,
    log_file: str | Path = "",
    file_max_bytes: int = 5_242_880,
    file_backup_count: int = 3,
) -> logging.Logger:
    """Kok logger'i yeniden yapilandirir ve `robotics_agent` logger'ini dondurur.

    Idempotent: tekrar cagrildiginda kendi handler'larini kaldirip yeniden
    kurar. Testler modulleri `importlib.reload` ile yeniden yukledigi icin bu
    zorunlu; aksi halde her reload'da handler yigilir ve satirlar coklanir.

    Bu fonksiyon `config` modulunu **import etmez**: ayarlarin okunmasi
    `config.setup_logging`in isidir. Ters yonde bir import dairesel olurdu.
    """
    resolved_level = getattr(logging, str(level).upper(), logging.INFO)
    resolved_fmt = "json" if str(fmt).lower() == "json" else "text"

    global _ring
    with _lock:
        root = logging.getLogger()
        _remove_managed(root)
        root.setLevel(resolved_level)

        # Konsol akisi STDERR olmak zorunda: MCP stdio tasimasinda STDOUT saf
        # JSON-RPC'dir ve oraya dusen tek bir log satiri protokolu bozar.
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(_build_formatter(resolved_fmt, mask=mask))
        stream.setLevel(resolved_level)
        setattr(stream, _MANAGED, True)
        root.addHandler(stream)

        if log_file:
            path = Path(log_file).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            rotating = logging.handlers.RotatingFileHandler(
                path,
                maxBytes=max(1024, file_max_bytes),
                backupCount=max(0, file_backup_count),
                encoding="utf-8",
            )
            # Dosyaya her zaman JSON yazariz: dosya loglari makine tarafindan
            # okunur, insan tarafindan degil.
            rotating.setFormatter(JsonFormatter(mask=mask))
            rotating.setLevel(resolved_level)
            setattr(rotating, _MANAGED, True)
            root.addHandler(rotating)

        _ring = None
        if buffer_size > 0:
            ring = RingBufferHandler(buffer_size, mask=mask)
            ring.setLevel(resolved_level)
            setattr(ring, _MANAGED, True)
            root.addHandler(ring)
            _ring = ring

        for noisy in _NOISY:
            logging.getLogger(noisy).setLevel(max(logging.WARNING, resolved_level))

    return logging.getLogger("robotics_agent")


def get_log_buffer() -> RingBufferHandler | None:
    """Kurulu dairesel tampon; loglama yapilandirilmamissa None."""
    return _ring
