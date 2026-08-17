"""Cikti kapisi: modelin ve hata yollarinin urettigi metni disariya vermeden
once temizler.

Neden ayri bir modul:

`DLPEngine` "bu alan bu hedefe gidebilir mi" sorusunu cevaplar. Ama iki yol
tarihsel olarak bu motoru hic cagirmiyordu:

  1. **Hata yollari.** `execute_tool` icindeki `except` dallari DLP adimindan
     once `return` ediyordu. SAP OData hata govdeleri basarisiz alanin degerini
     yankilar; yani hata mesaji da bir veri ciktisidir.
  2. **Model cevabi.** `reply` dogrudan HTTP yanitina gidiyordu.

OWASP karsiliklari:

  LLM02 Sensitive Information Disclosure - hassas veri prompt'tan **ve
  ciktidan** temizlenmeli.
  LLM05 Improper Output Handling - model bir kullanici gibi gorulmeli
  (zero-trust); ciktisi dogrulanmadan asagi akise verilmemeli.

Tasarim kurali: temizleme **is baglamini korur**. Belge numarasi, malzeme,
tesis, tutar gibi D0/D1 veriler gecer; yalniz IBAN/vergi no/e-posta/token gibi
D2-D3 degerler maskelenir veya tokenlestirilir. Hatayi okunamaz hale getiren
bir temizleme, hatayi gizlemekten daha kotudur: kimse neyin yanlis gittigini
anlamaz.
"""

from __future__ import annotations

import logging
from typing import Any

from ..contracts.actor import ActorContext
from .dlp import DLPEngine, build_dlp_engine
from .field_policy import Sink

log = logging.getLogger(__name__)

__all__ = [
    "REDACTED",
    "sanitize_error_body",
    "sanitize_for_client",
    "sanitize_for_log",
    "sanitize_text",
]

#: Temizleme motorunun kendisi patlarsa kullanilan tam gizleme.
#: Fail-closed: DLP calismiyorsa ham veri disariya CIKMAZ.
REDACTED = "[gizlendi: veri temizleme uygulanamadi]"

_engine_cache: dict[int, DLPEngine] = {}


def _engine(settings: Any = None, dlp: DLPEngine | None = None) -> DLPEngine | None:
    """Hazir motoru kullan, yoksa ayarlardan uret ve onbellekle.

    Her cagrida yeni motor kurmak regex derlemesini tekrarlardi; motor
    durumsuzdur, ayar nesnesi basina bir tane yeter.
    """
    if dlp is not None:
        return dlp
    if settings is None:
        return None
    key = id(settings)
    cached = _engine_cache.get(key)
    if cached is None:
        cached = build_dlp_engine(settings)
        # Sinirsiz buyumeyi engelle (uzun omurlu surecte ayar nesnesi tektir).
        if len(_engine_cache) > 32:
            _engine_cache.clear()
        _engine_cache[key] = cached
    return cached


def sanitize_text(
    text: str,
    *,
    actor: ActorContext,
    sink: Sink,
    settings: Any = None,
    dlp: DLPEngine | None = None,
    purpose: str = "",
) -> str:
    """Serbest metni hedefe gore temizler. Motor yoksa/patlarsa gizler."""
    if not text:
        return text
    engine = _engine(settings, dlp)
    if engine is None:
        return REDACTED
    try:
        result = engine.apply_text(text, actor=actor, sink=sink, purpose=purpose)
    except Exception:  # noqa: BLE001 - temizleme hatasi ham veri sizdirmamali
        log.exception("DLP metin temizlemesi basarisiz; govde gizlendi (sink=%s)", sink)
        return REDACTED
    payload = result.payload
    return payload if isinstance(payload, str) else REDACTED


def sanitize_for_client(
    text: str,
    *,
    actor: ActorContext,
    settings: Any = None,
    dlp: DLPEngine | None = None,
    purpose: str = "",
) -> str:
    """Model cevabini istemciye vermeden once temizler (OWASP LLM05).

    `sink="client"` politikasi D3'u yalniz acik `sap.data.restricted` kapsami +
    `detail=full` + gecerli amac kodu ile gecirir; aksi halde tokenlestirir.
    """
    return sanitize_text(
        text, actor=actor, sink="client", settings=settings, dlp=dlp, purpose=purpose
    )


def sanitize_for_log(
    text: str, *, actor: ActorContext, dlp: DLPEngine | None = None, settings: Any = None
) -> str:
    """Log satirini temizler.

    `field_policy` sozlesmesi: merkezi log hicbir kosulda ticari/kisisel veri
    almaz (D2 -> mask, D3 -> drop). Log agregasyonu genis erisimli ve uzun
    saklamalidir; sizintinin en pahali oldugu yer burasidir.
    """
    return sanitize_text(text, actor=actor, sink="log", settings=settings, dlp=dlp)


def sanitize_error_body(
    body: dict[str, Any],
    *,
    actor: ActorContext,
    sink: Sink = "model",
    dlp: DLPEngine | None = None,
    settings: Any = None,
    text_keys: tuple[str, ...] = ("error", "detail", "message", "remediation", "hint"),
) -> dict[str, Any]:
    """Hata govdesindeki serbest metin alanlarini temizler.

    Yalniz metin alanlari taranir: `denial_code`, `sap_code`, `timeout_s` gibi
    yapisal alanlar kararin kendisidir ve degistirilmemeli - model bunlara
    bakarak dogru davranisi secer.
    """
    out = dict(body)
    for key in text_keys:
        value = out.get(key)
        if isinstance(value, str) and value:
            out[key] = sanitize_text(
                value, actor=actor, sink=sink, settings=settings, dlp=dlp
            )
    return out
