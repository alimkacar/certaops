"""Calisma zamani ayar override'larinin diskteki deposu.

Neden `.env.local` DEGIL: o dosya duz metin principal token'i tasiyor.
Ayar yazan bir kod yolunun sir tasiyan bir dosyaya yazma yetkisi olmamali;
bir hata token'lari bozarsa kurtarmak zor, sessizce bozarsa daha da zor.
Bu yuzden override'lar kendi dosyasinda durur ve o dosya **yalniz** izin
listesindeki anahtarlari tasir.

Oncelik sirasi (yuksekten dusuge):

    1. Dis ortam degiskeni  (kabuk / container / systemd)
    2. Bu dosya             (arayuzden yapilan degisiklik)
    3. `.env`
    4. Kod icindeki varsayilan

Birinci siranin ustte olmasi bilincli: container secret'lari ve acil durum
guvenlik kapilari tarayicidan yapilan bir degisiklikle sessizce geri
alinamamali. Operator her zaman kabuktan son sozu soyleyebilir --
`SAP_DRY_RUN=true` ile baslatilan bir surec arayuzden kuru calismadan
cikarilamaz. Arayuz bu durumu "ortam tarafindan sabitlenmis" olarak gosterir.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .registry import SETTABLE

log = logging.getLogger(__name__)

__all__ = [
    "overrides_path",
    "read_document",
    "write_document",
    "read_overrides",
    "write_overrides",
    "apply_overrides",
    "snapshot_process_env",
    "pinned_by_environment",
]

_DOSYA_ADI = "overrides.json"

# `load_dotenv()` cagrilmadan ONCE alinan anlik goruntu. `.env` degerleri de
# `os.environ`e yazildigi icin, sonradan bakildiginda bir anahtarin kabuktan
# mi yoksa `.env`den mi geldigi ayirt edilemez. Ayrim guvenlik acisindan
# onemli: yalniz GERCEKTEN dis ortamdan gelen bir anahtar override'i yener.
_SHELL_ENV: frozenset[str] = frozenset()


def snapshot_process_env() -> None:
    """Surec baslarken, `.env` yuklenmeden once cagrilmali."""
    global _SHELL_ENV
    _SHELL_ENV = frozenset(os.environ)


def pinned_by_environment(key: str) -> bool:
    """Anahtar dis ortamdan geldi mi? Geldiyse arayuzden degistirilemez."""
    return key in _SHELL_ENV


def overrides_path() -> Path:
    """Dosyanin yeri. `AGENT_CONFIG_DIR` ile tasinabilir."""
    kok = os.getenv("AGENT_CONFIG_DIR", "").strip()
    taban = Path(kok) if kok else Path.cwd() / "config"
    return taban / _DOSYA_ADI


def read_document(path: Path | None = None) -> dict[str, Any]:
    """Dosyanin tamamini okur (`settings` + `pending`). Bozuksa bos sozluk."""
    yol = path or overrides_path()
    try:
        ham = json.loads(yol.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        log.warning("ayar override dosyasi okunamadi (%s): %s", yol, exc)
        return {}
    return ham if isinstance(ham, dict) else {}


def write_document(govde: dict[str, Any], path: Path | None = None) -> Path:
    """Belgenin tamamini ATOMIK olarak yazar; izin 0600.

    Atomik: gecici dosyaya yazip `os.replace` ile yerine koyar. Yarim
    yazilmis bir yapilandirma dosyasi hicbir zaman diskte kalmaz.
    """
    yol = path or overrides_path()
    yol.parent.mkdir(parents=True, exist_ok=True)
    metin = json.dumps(govde, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    fd, gecici = tempfile.mkstemp(dir=str(yol.parent), prefix=".overrides-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(metin)
            f.flush()
            os.fsync(f.fileno())
        os.replace(gecici, yol)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(gecici)
        raise
    with contextlib.suppress(OSError):
        os.chmod(yol, 0o600)
    return yol


def read_overrides(path: Path | None = None) -> dict[str, Any]:
    """Diskteki override'lari okur.

    Izin listesinde olmayan anahtarlar okurken de elenir: dosya elle
    duzenlenmis ya da baska bir surumden kalmis olabilir. Bozuk bir dosya
    servisi durdurmaz -- bos sozluk doner ve durum loglanir.
    """
    girdiler = read_document(path).get("settings")
    if not isinstance(girdiler, dict):
        return {}

    temiz: dict[str, Any] = {}
    for anahtar, kayit in girdiler.items():
        if anahtar not in SETTABLE:
            log.warning("ayar override'i izin listesinde degil, atlandi: %s", anahtar)
            continue
        if isinstance(kayit, dict) and "value" in kayit:
            temiz[anahtar] = kayit
        else:
            temiz[anahtar] = {"value": kayit}
    return temiz


def write_overrides(girdiler: dict[str, Any], path: Path | None = None) -> Path:
    """Override'lari atomik olarak yazar.

    Atomik: gecici dosyaya yazip `os.replace` ile yerine koyar; yarim yazilmis
    bir yapilandirma dosyasi hicbir zaman diskte kalmaz. Izin `0600`: dosya
    sir tasimasa da hangi kipte calisildigini acik etmesi gerekmez.
    """
    mevcut = read_document(path)
    mevcut["settings"] = {
        anahtar: kayit for anahtar, kayit in girdiler.items()
        if anahtar in SETTABLE  # izin listesi yazarken de gecerli
    }
    return write_document(mevcut, path)


def apply_overrides(path: Path | None = None) -> dict[str, str]:
    """Override'lari `os.environ`e uygular. Uygulananlari dondurur.

    `.env` yuklendikten SONRA cagrilir. Dis ortamdan gelen bir anahtara
    dokunmaz -- bu, oncelik sirasinin tek uygulama noktasidir.
    """
    uygulanan: dict[str, str] = {}
    for anahtar, kayit in read_overrides(path).items():
        if pinned_by_environment(anahtar):
            continue
        deger = kayit.get("value")
        if deger is None:
            continue
        os.environ[anahtar] = str(deger)
        uygulanan[anahtar] = str(deger)
    return uygulanan
