"""Bagimsiz SAP okumalarini sinirli eszamanlilikla calistirir.

Neden gerekli
-------------
Olcum acik: tur suresi neredeyse tamamen **ardisik ag gidis-donusu**. Her
backend metodu net 1-31 ms CPU harcarken, sandbox'ta bir OData cagrisi
~150-360 ms suruyor. `sap_material_360` alti bagimsiz okuma yapiyor ve
bunlari sirayla bekliyor; altisi ayni anda gitse tur suresi %60 dusuyor.

Neden thread, neden async degil
-------------------------------
`httpx.Client` thread-safe'dir ve `CircuitBreaker` kilit korumalidir, yani
mevcut senkron cagri zinciri oldugu gibi paralellestirilebilir. Async'e
gecmek butun tool katmanini yeniden yazmak demekti; kazanc ayni, maliyet
kiyaslanamaz.

Guvenlik siniri
---------------
Dagitim **yalniz tek bir tool cagrisinin icinde** guvenlidir. `execute_tool`
handler'in etrafinda `ctx.sap.set_acting_subject()` kurup temizler; iki ayri
tool'un okumasini ayni havuzda karistirmak o kimligi bulanirdi. Bu yuzden
yardimci fonksiyon tek bir cagri listesi alir ve doner - kalici bir havuz
tutmaz.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Uzerinde eszamanliligin anlamsiz oldugu sinir. Tek okuma icin havuz
#: kurmak, olcumde kazanctan buyuk bir ek yuk getiriyor.
_MIN_FANOUT = 2


def gather_reads(
    thunks: Sequence[Callable[[], T]],
    *,
    max_workers: int = 4,
    timeout: float | None = None,
) -> list[T]:
    """Verilen okumalari paralel calistirir; **sirayi korur**.

    Her thunk argumansiz cagrilabilir olmalidir. Sonuc listesi girdi
    sirasindadir; boylece cagiran taraf indeksle eslestirebilir ve
    eszamanlilik cagri yerindeki okunabilirligi bozmaz.

    Bir thunk istisna firlatirsa istisna **cagirana aynen yukselir** -
    sessizce yutulmaz. Ilk hata digerlerinin tamamlanmasini beklemeden
    doner; yarim kalan okumalar salt-okunur oldugu icin geri alinacak bir
    sey yoktur.

    `max_workers <= 1` veya tek elemanli liste verildiginde havuz hic
    kurulmaz ve cagrilar sirayla yapilir: testlerde ve tek okumali
    yollarda davranis birebir eskisi gibi kalir.
    """
    calls = list(thunks)
    if not calls:
        return []
    if len(calls) < _MIN_FANOUT or max_workers <= 1:
        return [call() for call in calls]

    workers = min(max_workers, len(calls))
    # Thread'ler daemon degildir ve yorumlayici cikisinda join edilir; bu
    # yuzden havuz `with` blogu icinde tutulur ve blok bitmeden kapanir.
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sap-read") as pool:
        futures = [pool.submit(call) for call in calls]
        return [future.result(timeout=timeout) for future in futures]


def gather_named(
    thunks: dict[str, Callable[[], Any]],
    *,
    max_workers: int = 4,
    timeout: float | None = None,
) -> dict[str, Any]:
    """`gather_reads`in sozluk hali: sonuclar ayni anahtarlarla doner."""
    keys = list(thunks)
    values = gather_reads([thunks[k] for k in keys], max_workers=max_workers, timeout=timeout)
    return dict(zip(keys, values, strict=True))
