"""Log baglami: correlation, execution, tenant, subject, channel.

Bir log satirinin **hangi istege ait oldugu** bilinmiyorsa olay incelemesi
tahmine dayanir. Bu modul o baglami `contextvars` uzerinden tasir: kanal
katmani (API middleware / CLI) ve runtime baglami bir kez baglar, ondan sonra
ayni gorevde uretilen her log kaydi bu alanlari otomatik tasir.

`contextvars` bilerek secildi: thread-local degildir, asyncio gorevleri ve
`run_in_threadpool` cagrilari baglami **kopyalayarak** devralir. Bir alt
gorevde yapilan `bind()` ust goreve sizmaz; istekler arasi karisma olmaz.

Sinir: FastAPI senkron bagimliliklari (`Depends`) her biri ayri bir
threadpool cagrisinda calisir, dolayisiyla bir bagimlilik icinde yapilan
`bind()` uc nokta fonksiyonuna **ulasmaz**. Bu yuzden tenant/subject
baglamasi bagimlilikta degil, runtime turunun basinda yapilir.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

#: Log satirlarina tasinabilecek alanlar. Liste bilerek kapalidir: serbest
#: anahtar kabul edilseydi bir yazim hatasi (`tenat=`) sessizce kaybolur ve
#: olay incelemesinde eksik alan fark edilmezdi.
CONTEXT_FIELDS: tuple[str, ...] = (
    "correlation_id",
    "execution_id",
    "session_id",
    "tenant",
    "subject",
    "channel",
)

_EMPTY: dict[str, str] = {}

_context: ContextVar[dict[str, str]] = ContextVar("certaops_log_context", default=_EMPTY)


class UnknownContextField(KeyError):
    """Tanimsiz bir baglam alani baglanmaya calisildi."""


def current_context() -> dict[str, str]:
    """Aktif baglamin kopyasi. Cagiran taraf serbestce degistirebilir."""
    return dict(_context.get())


def bind(**values: object) -> Token[dict[str, str]]:
    """Baglama alan ekler ve geri alma token'i dondurur.

    Bos degerler (None, "") mevcut degeri **silmez**: eksik bir parametre
    yuzunden zaten dogru olan bir correlation ID'nin kaybolmasi, o degeri hic
    baglamamaktan daha kotudur.
    """
    unknown = sorted(set(values) - set(CONTEXT_FIELDS))
    if unknown:
        raise UnknownContextField(
            f"Tanimsiz log baglam alani: {', '.join(unknown)}. "
            f"Gecerli alanlar: {', '.join(CONTEXT_FIELDS)}"
        )
    merged = dict(_context.get())
    for key, value in values.items():
        if value:
            merged[key] = str(value)
    return _context.set(merged)


def reset(token: Token[dict[str, str]]) -> None:
    """`bind()` ile alinan token'i geri alir."""
    _context.reset(token)


def clear() -> None:
    """Baglami tumden bosaltir (test ve uzun omurlu worker'lar icin)."""
    _context.set(_EMPTY)


@contextmanager
def log_context(**values: object) -> Iterator[dict[str, str]]:
    """Kapsam bitince baglami eski haline dondurur."""
    token = bind(**values)
    try:
        yield current_context()
    finally:
        reset(token)
