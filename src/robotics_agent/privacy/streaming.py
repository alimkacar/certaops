"""Akan (streaming) metin icin sinir-guvenli DLP.

Sorun
-----
Model yaniti parca parca gelir. Her parcayi tek basina `sanitize_text`ten
gecirip yollamak **ses gibi gorunen ama saglam olmayan** bir cozumdur: bir
sirri iki parcaya bolen bir sinir, hicbir parcada desene uymaz.

    parca 1: "...token: Bearer sk-ab"
    parca 2: "cdef1234567890..."

Ikisi de ayri ayri temizdir; birlestirildiginde sir aciga cikmistir. Ayni
sey IBAN, kart numarasi ve e-posta icin de gecerlidir.

Cozum
-----
`StreamSanitizer` bir **tampon** tutar ve yalnizca "artik hicbir desenin
icine dusmeyecegi kesin olan" on eki yayimlar:

  1. Gelen parca tampona eklenir.
  2. Tamponun sonundan `LOOKBEHIND` karakterlik bir kuyruk AYRILIR ve
     bekletilir - bir sonraki parcayla birlesip desen olusturabilir.
  3. Kalan on ek temizlenir ve yayimlanir.
  4. Akis bitince (`flush`) kuyruk da temizlenip yayimlanir.

Yayimlanan her parca `sanitize_text`ten gecer; yani akis yolu, tek seferlik
yolun sagladigi garantiyi ZAYIFLATMAZ - yalnizca gecikmeyi kullaniciya
dagitir.

Bir ek incelik: temizleyici bir sirri maskeye cevirdiginde metnin uzunlugu
degisebilir. Bu yuzden tampon "ham" tutulur ve her yayimda YALNIZ o yayimin
on eki temizlenir; daha once yayimlanmis metin bir daha islenmez.
"""

from __future__ import annotations

from typing import Any

from ..contracts import ActorContext
from .output import sanitize_text

#: Iki parcaya bolunmus bir sirri yakalayabilmek icin bekletilen kuyruk.
#: En uzun ilgi alani olan desenden (IBAN ~34, kart 19, uzun API anahtarlari
#: ~64) rahatca buyuk secilir; maliyeti yalnizca bu kadar karakterin bir
#: parca gecikmesidir.
LOOKBEHIND = 96


class StreamSanitizer:
    """Parca parca gelen metni sinir-guvenli sekilde temizleyip yayimlar.

    Kullanim::

        s = StreamSanitizer(actor=actor, settings=settings, dlp=dlp)
        for chunk in provider_stream:
            out = s.feed(chunk)
            if out:
                yield out
        yield s.flush()
    """

    def __init__(
        self,
        *,
        actor: ActorContext,
        settings: Any = None,
        dlp: Any = None,
        sink: str = "client",
        purpose: str = "",
        lookbehind: int = LOOKBEHIND,
    ) -> None:
        self._actor = actor
        self._settings = settings
        self._dlp = dlp
        self._sink = sink
        self._purpose = purpose
        self._lookbehind = max(0, lookbehind)
        self._buffer = ""
        self._closed = False

    def feed(self, chunk: str) -> str:
        """Yeni parcayi alir; yayimlanabilir temiz metni dondurur (bos olabilir)."""
        if self._closed:
            raise RuntimeError("Akis kapandi; feed cagrilamaz.")
        if not chunk:
            return ""
        self._buffer += chunk
        if len(self._buffer) <= self._lookbehind:
            # Henuz guvenle yayimlanabilecek bir on ek yok.
            return ""
        cut = len(self._buffer) - self._lookbehind
        prefix, self._buffer = self._buffer[:cut], self._buffer[cut:]
        return self._clean(prefix)

    def flush(self) -> str:
        """Akis bitti: bekletilen kuyrugu temizleyip dondurur."""
        self._closed = True
        tail, self._buffer = self._buffer, ""
        return self._clean(tail)

    def _clean(self, text: str) -> str:
        if not text:
            return ""
        return sanitize_text(
            text,
            actor=self._actor,
            sink=self._sink,  # type: ignore[arg-type]
            settings=self._settings,
            dlp=self._dlp,
            purpose=self._purpose,
        )
