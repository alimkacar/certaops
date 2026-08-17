"""SAP ECC 6.0 EHP8 adapter katmani.

Bu paket **protokol katmanini yeniden yazmaz**. `adapters.sap` icindeki HTTP
cekirdegi, CSRF akisi, OAuth2/Destination cozumlemesi, hata ayristirma ve
`$metadata` kontrat dogrulamasi ECC'de de aynen gecerlidir: EHP8, NetWeaver
7.50 uzerinde kosar ve `SAP_GWFND 7.50` (embedded Gateway) OData V2'yi
yerlesik saglar.

ECC'ye ozgu olan tek sey **hangi servisin nerede oldugu ve neyi dondurdugu**;
o da `capabilities.py` icindeki manifesttir.

Neden OData V2, neden RFC degil:

  - `SAP_GWFND 7.50` OData **V4** icin RAP gerektirir (ABAP 7.53+). EHP8'de yok.
    Bu yuzden V4 istemcisi (`odata_v4.py`) bu adapterda hic kullanilmaz.
  - PyRFC yolu NW RFC SDK binary'si ister (pip ile kurulamaz) ve `adapters.sap`
    icindeki HTTP/auth/retry/allowed_hosts katmanini tumden ise yaramaz kilardi.
  - Z-Gateway servisleri BAPI'leri sarmalar; boylece is mantigi SAP tarafinda,
    sozlesme dogrulamasi `$metadata` uzerinden Python tarafinda kalir.

Manifestteki her servis `STATUS_CUSTOM`'dur ve bu **bilincli**: ECC'de released
public OData API yoktur. `sap_discover_capabilities` bunu oldugu gibi raporlar,
"released" gibi gostermez.
"""

from __future__ import annotations

from .capabilities import (
    ABAP_SOURCES,
    ECC_CAPABILITY_MANIFEST,
    IDEMPOTENCY_ENTITY_SET,
    MRP_ELEMENT_LABELS,
    ecc_manifest_summary,
    ecc_service_path,
)

__all__ = [
    "ABAP_SOURCES",
    "ECC_CAPABILITY_MANIFEST",
    "IDEMPOTENCY_ENTITY_SET",
    "MRP_ELEMENT_LABELS",
    "ecc_manifest_summary",
    "ecc_service_path",
]
