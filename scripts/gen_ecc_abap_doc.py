#!/usr/bin/env python3
"""ECC ABAP gereksinim dokumanini manifestten uretir.

Dokuman elle yazilirsa Python sozlesmesiyle ayrisir: bir alan `capabilities.py`
icinde degisir, dokumanda eski hali kalir, ABAP ekibi yanlis alani uygular.
Bu script tek kaynak kuralini zorlar - alan listeleri **her zaman** manifestten
uretilir.

Kullanim:
    python scripts/gen_ecc_abap_doc.py          # docs/ECC_ABAP_REQUIREMENTS.md yazar
    python scripts/gen_ecc_abap_doc.py --check  # guncel mi diye bakar (CI icin)
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from robotics_agent.adapters.ecc.capabilities import (  # noqa: E402
    ABAP_SOURCES,
    ECC_CAPABILITY_MANIFEST,
)

OUTPUT = ROOT / "docs" / "ECC_ABAP_REQUIREMENTS.md"

# Alan adindan onerilen EDM tipi. Gateway'de tip yanlis secilirse $filter
# literalleri (Edm.Decimal icin `m` soneki, Edm.DateTime icin datetime'...')
# parse edilemez ve servis 400 doner.
_DECIMAL_HINTS = (
    "Quantity", "Price", "Amount", "Value", "Score", "Percent", "Cost",
    "Commitment", "Weight", "Stock", "LotSize",
)
_DATE_HINTS = ("Date", "ValidTo", "ValidFrom")
_DATETIME_HINTS = ("At",)
_INT_HINTS = ("Year", "Number", "Days", "Unit")


def edm_type(field: str) -> str:
    if field.endswith(("StartedAt", "CompletedAt", "DueAt", "CreatedAt")):
        return "Edm.DateTime"
    if any(h in field for h in _DECIMAL_HINTS):
        return "Edm.Decimal"
    if any(field.endswith(h) or h in field for h in _DATE_HINTS):
        return "Edm.DateTime"
    if field.endswith("Days") or field.endswith("Year") or field == "StepNumber":
        return "Edm.Int32"
    if field.endswith(("Indicator", "Considered", "Block")):
        return "Edm.String(1)"
    return "Edm.String"


HEADER = """# ECC 6.0 EHP8 - ABAP Gereksinim Dokumani

> **Bu dosya elle duzenlenmez.** `python scripts/gen_ecc_abap_doc.py` ile
> `src/robotics_agent/adapters/ecc/capabilities.py` manifestinden uretilir.
> Alan listesi degisecekse once manifest degisir.

## Ozet

Python tarafi (`src/robotics_agent/sap/ecc.py`) **hazirdir ve bu sozlesmeyi
bekler**. ABAP ekibinin isi, asagidaki entity set'leri listelenen alanlarla
donduren Z-Gateway servislerini uygulamaktir. Servisler devreye alindikca
`sap_discover_capabilities` ilgili tool'u yesile cevirir; hicbir Python
degisikligi gerekmez.

## Platform varsayimlari

| Konu | Deger |
|---|---|
| ERP | SAP ECC 6.0, Enhancement Package 8 |
| ABAP stack | SAP NetWeaver 7.50 |
| Gateway | `SAP_GWFND 7.50` (embedded - ayri hub gerekmez) |
| OData surumu | **V2 (yalniz)** |
| OData V4 | **Kullanilamaz.** RAP, ABAP 7.53+ ister; EHP8 7.50'dir |
| ABAP CDS | Kullanilabilir (NW 7.40 SP05+) - join'ler CDS'te yapilmali |

## On kosullar

1. `SAP_GWFND` bileseni aktif; `/IWFND/MAINT_SERVICE` erisilebilir.
2. Her Z servisi SICF'te aktive edilmis (`/sap/opu/odata/sap/<SERVIS>`).
3. Servis kullanicisinda `S_SERVICE` yetki nesnesi ilgili servisler icin acik.
4. CSRF token akisi acik (Gateway varsayilani). Python tarafi `x-csrf-token`
   fetch/yenileme dongusunu zaten uyguluyor.
5. Kimlik dogrulama: Basic (yalniz gelistirme), OAuth 2.0 veya SAP Cloud
   Connector principal propagation. Uretimde Basic **kullanilmamali**.

## Sozlesme kurallari

**Adlandirma.** Entity set adlari asagida verildigi gibi birebir olmali.
Navigation property'ler `To<Hedef>` kalibindadir: `ToItems`, `ToHeader`,
`ToScheduleLines`, `ToScales`, `ToBlocks`.

**EDM tipleri.** Asagidaki tablolarda "onerilen tip" sutunu bilgilendiricidir
ama miktar/tarih alanlarinda **kritiktir**: Python `$filter` icinde
`Edm.Decimal` icin `10.000m`, `Edm.DateTime` icin `datetime'2026-08-06T00:00:00'`
literali uretir. Tip `Edm.String` secilirse bu filtreler 400 doner.

**Bos deger politikasi.** Bilinmeyen bir sayisal alan **bos** dondurulmeli,
`0` degil. Python tarafi bos degeri `estimated_fields` ile isaretler; `0`
gonderilirse gercek veri saniliP yanlis karara yol acar. Bu ozellikle
`SupplierScoreSet` icin gecerlidir.

**Hata formati.** Standart Gateway hata govdesi (`error.message.value`,
`error.innererror.errordetails`) kullanilmali. Python tarafi bunu ayristirip
SAP mesajlarini denetim defterine yazar.

**Silinmis kayitlar.** `DeletionIndicator` alanini dondurun ve filtrelenebilir
yapin; Python `DeletionIndicator eq ''` ile eler.

**Sayfalama.** `$top` ve `$skip` desteklenmeli, `$inlinecount` opsiyonel.

---

"""

CRITICAL = """
---

## Kritik konular

Bu dort madde, ECC gecisinin gercek is yukudur. Geri kalan servisler duz
CDS projeksiyonudur.

### ZAGENT_IDEMPOTENCY (zorunlu)

Yazma islemi tek bir yerdedir: satinalma talebi olusturma. Timeout sonrasi
"yazildi mi yazilmadi mi" sorusunu cozen tek mekanizma budur.

| Alan | Tip | Aciklama |
|---|---|---|
| `MANDT` | CLNT(3) | Istemci |
| `IDEMPOTENCY_KEY` | CHAR(64) | **Birincil anahtar** |
| `OBJECT_TYPE` | CHAR(30) | `PURCHASE_REQUISITION` |
| `OBJECT_ID` | CHAR(10) | Olusan BANFN |
| `CREATED_AT` | TIMESTAMPL | UTC |
| `CREATED_BY` | CHAR(12) | SY-UNAME |

Yazma akisi (**tek LUW**):

```abap
ENQUEUE_EMEBANE                     " EBAN kilidi
INSERT zagent_idempotency           " cakisirsa: mevcut OBJECT_ID'i dondur, CIK
CALL FUNCTION 'BAPI_PR_CREATE'
IF return has error -> ROLLBACK, hata dondur
CALL FUNCTION 'BAPI_TRANSACTION_COMMIT' EXPORTING wait = 'X'
DEQUEUE_EMEBANE
```

**Cakisma davranisi:** HTTP 409 DEGIL. Mevcut PR numarasi ile HTTP 201/200
donmeli ve `AlreadyExisted` alani `'X'` olmali. Boylece mutabakat tekrar POST
etmeden cozulur - Python tarafi bunu `PurchaseRequisitionResult.created=False`
olarak raporlar.

**Neden baslik metni kullanilamaz:** S/4 adapteri referans hash'ini PR baslik
metnine gomup `contains()` ile arar. ECC'de PR baslik metni STXH/STXL'de yasar
ve OData'dan **filtrelenemez**. Bu tablo o yuzden opsiyonel degildir.

### ATP (`AvailabilitySet`)

`BAPI_MATERIAL_AVAILABILITY` sarmalanir. Girdi `$filter` uzerinden gelir:

```
Material eq '...' and Plant eq '...' and
RequestedQuantity eq 25.000m and RequestedDate eq datetime'2026-09-01T00:00:00'
```

Cikti `WMDVEX` tablosunun satirlaridir - her satir bir `AvailabilitySet` kaydi
(tarih + taahhut miktari + arz elementi).

**`CalendarConsidered` sabit `'X'` DONDURULMEMELI.** Kullanilan kontrol
kuralinin fabrika takvimini dikkate alip almadigini gercekten bildirmeli;
Python bu deger `false` iken kullaniciya "teslim gunu is gunu olmayabilir"
uyarisi ekler. Yanlis `'X'` termin taahhudunu bozar.

gATP/APO varsa `BAPI_APO_AVAILABILITY_CHECK` tercih edilir; hangi motorun
kullanildigi `SupplyElement` alaninda bildirilmeli.

### Belge akisi (`PurchaseOrderHistorySet`)

ECC'nin S/4'e ustunlugu burada. EKBE mal kabul ve fatura girisini kalem
bazinda zaten tasir; zincir tek okumadan kurulur.

`HistoryCategory` = `EKBE-BEWTP`:

| Deger | Anlam | Python dugumu |
|---|---|---|
| `E` | Mal kabul | `goods_receipt`, `linked_by = EKBE-BELNR (BEWTP=E)` |
| `Q` | Fatura girisi | `supplier_invoice`, `linked_by = EKBE-BELNR (BEWTP=Q)` |
| `U` | Stok transferi | (kullanilmiyor) |

PR bagi `EKPO-BANFN` alanindan gelir ve `PurchaseOrderItemSet.PurchaseRequisition`
olarak dondurulmelidir. **Bag alani bos ise Python o dugumu zincire eklemez** -
tahmini bag uretilmez.

Iptal hareketleri (102/122/162) ayri satir olarak dondurulmeli; netlestirme
Python tarafinda yapilir.

### Tedarikci skoru (`SupplierScoreSet`)

ECC klasik degerlendirmesi (ELBK/ELBP, ME6H) su alanlari verir: fiyat, kalite,
teslimat, servis, genel puan.

`OnTimeDeliveryPct` ve `QualityPPM` **ECC'de standart alan degildir**:

- Hesaplanabiliyorsa: `EKES` (tedarikci teyidi) ile `EKBE` (fiili mal kabul)
  karsilastirmasindan zamaninda teslim yuzdesi; kalite icin `QMEL` bildirimleri.
- Hesaplanamiyorsa: **alani bos birakin.** Python bos degeri
  `estimated_fields` listesine koyar ve model karari buna gore verir. `0`
  gondermek "kalite mukemmel" veya "hicbir teslimat zamaninda degil" gibi
  okunur; ikisi de yanlistir.

---

## Yetkilendirme

Servis kullanicisi icin gereken ABAP yetki nesneleri:

| Nesne | Alanlar | Servisler |
|---|---|---|
| `S_SERVICE` | SRV_NAME = ilgili Gateway servisleri | tumu |
| `M_MATE_MAT` | ACTVT 03 | malzeme ana verisi |
| `M_MSEG_WMB` | ACTVT 03, WERKS | stok, mal kabul |
| `M_BANF_BSA` | ACTVT 01/03, BSART = NB | PR olusturma/okuma |
| `M_BEST_BSA` | ACTVT 03 | PO okuma |
| `M_RECH_WRK` | ACTVT 03 | fatura okuma |
| `M_LFM1_EKO` | ACTVT 03, EKORG | tedarikci, bilgi kaydi |
| `C_PRPS_KOK` | ACTVT 03 | proje maliyeti |
| `S_WF_WI` | ACTVT 03 | is akisi durumu |

Yazma yetkisi **yalniz** `M_BANF_BSA` ACTVT 01 ile sinirli olmali. PO, mal
kabul ve fatura tarafinda yazma yetkisi verilmemeli - bu tool'lar salt okunur
olarak tasarlandi.

## Kabul kriterleri

Bir servis "bitti" sayilmadan once:

1. `sap_discover_capabilities` (probe=true) ilgili alias icin
   `contract_ok: true` dondurmeli - yani `$metadata` beklenen entity set ve
   alanlarin tamamini icermeli.
2. `$metadata` belgesi `tests/contract/fixtures/` altina kaydedilmeli; kontrat
   testi CI'da ag olmadan kosar.
3. Performans butcesi: her tool'un bildirdigi `max_sap_calls` asilmamali.
   Ozellikle `PurchaseOrderItemSet` icin `DeliveredQuantity` / `InvoicedQuantity`
   CDS icinde EKBE'den toplanmali - kalem basina ek cagri butceyi kirar.
4. `$filter` uzerinde `substringof`, `eq`, `and`, `or`, `startswith` ve coklu
   `or` zinciri calismali (Python coklu malzemeyi tek cagride okur).

## Onerilen calisma sirasi

| Sira | Servis | Neden once |
|---|---|---|
| 1 | `ZAGENT_MM_MATERIAL_SRV` | Diger her seyin bagimliligi; en basit |
| 2 | `ZAGENT_MM_STOCK_SRV` | Planlama tool'lari acilir |
| 3 | `ZAGENT_MM_SOURCING_SRV` | PR hazirligi icin fiyat gerekli |
| 4 | `ZAGENT_MM_PO_SRV` | P2P okuma tool'larinin cogunu acar |
| 5 | `ZAGENT_MM_PR_SRV` | Tek yazma yolu; en dikkatli test edilecek |
| 6 | `ZAGENT_FI_INVOICE_SRV` | Fatura gorunurlugu |
| 7 | `ZAGENT_WF_STATUS_SRV` | Onay durumu |
| 8 | `ZAGENT_PS_COST_SRV` | Proje finansmani |

Her adimdan sonra sistem calisir durumda kalir: uygulanmamis servisler
`SAPNotSupported` doner, ilgili tool "bu backend bunu desteklemiyor" der ve
tahmini veri uretmez. Big-bang gecis gerekmez.
"""


def render() -> str:
    parts = [HEADER]
    # Servisleri SICF yoluna gore grupla: bir SEGW projesi = bir baslik.
    by_path: dict[str, list[str]] = {}
    for alias, capability in ECC_CAPABILITY_MANIFEST.items():
        by_path.setdefault(capability.service_path, []).append(alias)

    parts.append("## Servisler\n")
    for path, aliases in by_path.items():
        service = path.rsplit("/", 1)[-1]
        parts.append(f"### `{service}`\n")
        parts.append(f"SICF yolu: `{path}`  \n")
        parts.append("OData surumu: **V2**  \n")
        parts.append(f"Kapsadigi yetenekler: {', '.join(f'`{a}`' for a in aliases)}\n")

        for alias in aliases:
            capability = ECC_CAPABILITY_MANIFEST[alias]
            sources = ABAP_SOURCES.get(alias, {})
            parts.append(f"\n#### {alias} - {capability.purpose}\n")

            for label, key in (
                ("Tablolar", "tables"),
                ("Fonksiyon modulleri", "function_modules"),
                ("CDS view'lari", "cds"),
                ("Islemler", "transactions"),
            ):
                values = sources.get(key)
                if values:
                    parts.append(
                        f"- **{label}:** {', '.join(f'`{v}`' for v in values)}\n"
                    )

            for entity_set in capability.entity_sets:
                fields = capability.critical_properties.get(entity_set)
                parts.append(f"\n**`{entity_set}`**")
                if not fields:
                    parts.append(
                        " - kritik alan tanimlanmadi (kontrat testi yalniz varligini arar)\n"
                    )
                    continue
                parts.append("\n\n| Alan | Onerilen tip |\n|---|---|\n")
                for field in fields:
                    parts.append(f"| `{field}` | {edm_type(field)} |\n")

            notes = sources.get("notes")
            if notes:
                parts.append("\n**Notlar:**\n\n")
                for note in notes:
                    parts.append(f"- {note}\n")
            parts.append("\n")
        parts.append("\n")

    parts.append(CRITICAL)
    return "".join(parts)


def main() -> int:
    content = render()
    if "--check" in sys.argv:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != content:
            print("ECC_ABAP_REQUIREMENTS.md guncel degil. Yeniden uretin:")
            print("  python scripts/gen_ecc_abap_doc.py")
            return 1
        print("ECC_ABAP_REQUIREMENTS.md guncel.")
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(content, encoding="utf-8")
    print(f"Yazildi: {OUTPUT.relative_to(ROOT)} ({len(content.splitlines())} satir)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
