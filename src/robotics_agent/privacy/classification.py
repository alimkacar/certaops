"""D0-D3 veri siniflandirmasi ve tool bazli `DataPolicy` sozlesmesi.

Tek bir `data_classification` etiketi ("internal"/"confidential") yeterli
degildir: ayni tool'un
sonucunda hem tesis kodu (D1) hem tedarikci IBAN'i (D3) bulunabilir ve bunlarin
model/log/cache/export davranisi ayni olamaz.

Seviyeler:

    D0 Public        Kamuya acik veri (birim kodu, para birimi kodu).
    D1 Internal      Kurum ici is verisi (malzeme, tesis, miktar, durum).
    D2 Confidential  Ticari veya kisisel hassas veri (fiyat, tedarikci skoru,
                     e-posta, telefon, kisi adi).
    D3 Restricted    Kritik finansal/kimlik/gizli veri (IBAN, vergi kimligi,
                     token, banka hesabi). Modele **verilmez**.

Fail-closed kural: uretim profilinde siniflandirilmamis
alan otomatik D3 kabul edilir. Gelistirmede bu davranis alanin `default_class`
degerine duser ki yeni bir tool yazarken her alani onceden bildirmek zorunda
kalinmasin; ama `APP_ENV=production` altinda bilinmeyen alan modele gitmez.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Any

__all__ = [
    "FIELD_CLASS_INVENTORY",
    "DataClass",
    "DataPolicy",
    "classify_field",
    "is_personal_field",
    "max_class",
    "walk_fields",
]


class DataClass(str, Enum):
    """Veri hassasiyet seviyesi. Risk (R0-R4) ile karistirilmaz."""

    D0 = "D0"
    D1 = "D1"
    D2 = "D2"
    D3 = "D3"

    @property
    def level(self) -> int:
        return int(self.value[1])

    @property
    def label(self) -> str:
        return {
            DataClass.D0: "public",
            DataClass.D1: "internal",
            DataClass.D2: "confidential",
            DataClass.D3: "restricted",
        }[self]

    @property
    def cacheable_by_default(self) -> bool:
        """D3 verinin hicbir kosulda cache'lenemeyecegini bildirir."""
        return self.level <= 2

    @property
    def model_allowed_by_default(self) -> bool:
        """D3 verinin modele ham haliyle verilemeyecegini bildirir."""
        return self.level <= 2

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, DataClass):
            return self.level >= other.level
        return NotImplemented

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, DataClass):
            return self.level > other.level
        return NotImplemented

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, DataClass):
            return self.level <= other.level
        return NotImplemented

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if isinstance(other, DataClass):
            return self.level < other.level
        return NotImplemented


def max_class(classes: Iterable[DataClass]) -> DataClass:
    """Bir kumedeki en yuksek hassasiyet. Bos kume D0 doner."""
    best = DataClass.D0
    for item in classes:
        if item.level > best.level:
            best = item
    return best


# ---------------------------------------------------------------------------
# SAP business object -> merkezi alan envanteri
#
# Anahtarlar normalize edilmis alan adlaridir: kucuk harf, ayraclar atilmis.
# Boylece `SupplierEmail`, `supplier_email` ve `SUPPLIER-EMAIL` ayni kurala
# duser. Hem SAP teknik alan adlari (LIFNR, IBAN) hem bu projedeki mantiksal
# adlar (vendor_id, supplier_email) listelenir.
# ---------------------------------------------------------------------------
_D0_FIELDS = (
    "currency", "unit", "baseunit", "uom", "country", "language",
    "documenttype", "objecttype",
)

_D1_FIELDS = (
    "materialid", "material", "matnr", "description", "materialgroup", "matkl",
    "plant", "werks", "companycode", "bukrs", "purchasingorg", "ekorg",
    "purchasinggroup", "ekgrp", "storagelocation", "lgort",
    "quantity", "menge", "openqty", "deliveredqty", "status", "itemno",
    "poid", "ebeln", "requisitionid", "banfn", "materialdocument", "mblnr",
    "invoiceid", "belnr", "wbselement", "posid", "costcenter", "kostl",
    "deliverydate", "postingdate", "createdon", "documentdate",
    "leadtimedays", "mrpcontroller", "abcindicator", "batch", "charg",
    "serialnumber", "sernr", "movementtype", "bwart",
)

_D2_FIELDS = (
    # Ticari kosullar
    "netprice", "netpr", "unitprice", "lineitemamount", "linetotal",
    "netvalue", "totalvalue", "amount", "grossamount", "taxamount",
    "priceunit", "peinh", "scaleprices", "paymentterms", "zterm", "incoterms",
    "discount", "margin", "plancost", "actualcost", "commitment", "budget",
    # Tedarikci degerlendirmesi
    "vendorid", "lifnr", "vendorname", "suppliername", "supplierid",
    "overallscore", "pricescore", "deliveryscore", "qualityscore",
    "qualityppm", "ontimedeliverypct", "supplierscore",
    # Kisisel veri
    "email", "supplieremail", "contactemail", "phone", "telephone", "mobile",
    "fax", "contactperson", "requestername", "approvername", "createdby",
    "changedby", "processor", "agentname", "username", "displayname",
    "address", "street", "postalcode", "city",
)

_D3_FIELDS = (
    "iban", "bankaccount", "bankaccountnumber", "bankkey", "swift", "bic",
    "taxnumber", "taxid", "vatnumber", "stceg", "stcd1", "stcd2",
    "nationalid", "tckn", "passportnumber", "socialsecuritynumber",
    "password", "secret", "token", "accesstoken", "refreshtoken", "apikey",
    "clientsecret", "privatekey", "credential", "authorization", "cookie",
    "salary", "payroll", "creditcard", "cardnumber", "cvv",
)

FIELD_CLASS_INVENTORY: dict[str, DataClass] = {
    **{name: DataClass.D0 for name in _D0_FIELDS},
    **{name: DataClass.D1 for name in _D1_FIELDS},
    **{name: DataClass.D2 for name in _D2_FIELDS},
    **{name: DataClass.D3 for name in _D3_FIELDS},
}

# Tam eslesme tutmadiginda kullanilan alt-dize kurallari. Sira onemlidir:
# en yuksek sinif once denenir ki `supplier_bank_account` D2 degil D3 olsun.
_SUBSTRING_RULES: tuple[tuple[tuple[str, ...], DataClass], ...] = (
    (
        (
            "iban", "bankaccount", "bankkey", "swift", "taxnumber", "taxid",
            "vatnumber", "nationalid", "passport", "password", "secret",
            "token", "apikey", "clientsecret", "privatekey", "credential",
            "salary", "payroll", "creditcard", "cardnumber",
        ),
        DataClass.D3,
    ),
    (
        (
            "email", "phone", "mobile", "address", "contactperson", "price",
            "amount", "cost", "score", "vendorname", "suppliername",
            "createdby", "changedby", "approver", "requester",
        ),
        DataClass.D2,
    ),
)

# D2 icinde **kisisel veri** olan alanlar. Ticari D2 (fiyat, skor) ile ayni
# sinifta ama ayni degil: modelin fiyati gormesi karar icin gerekir, kisinin
# e-postasini gormesi degil. Model hedefinde bu alanlar tool acikca izin
# vermedikce maskelenir.
_PERSONAL_MARKERS = (
    "email", "phone", "telephone", "mobile", "fax", "gsm",
    "contactperson", "contact", "address", "street", "postalcode",
    "requestername", "approvername", "createdby", "changedby", "processor",
    "username", "displayname", "agentname",
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Alan adlari kucuk ve tekrar eden bir kumedir: 200 satirlik bir sonucta ayni
# 15 ad 200 kez normalize edilir. Bu fonksiyon DLP gezinmesinin en sicak
# noktasiydi (240 tool cagrisinda 470.760 cagri). Onbellek sinirli tutulur ki
# beklenmedik bir alan patlamasi bellegi buyutmesin.
_NORMALIZE_CACHE_SIZE = 8192


@lru_cache(maxsize=_NORMALIZE_CACHE_SIZE)
def _normalize_cached(name: str) -> str:
    return _NON_ALNUM.sub("", name.lower())


def _normalize(name: str) -> str:
    # str olmayan anahtar (int indeks vb.) hashlenebilir ama onbellegi kirletir;
    # once metne cevrilir.
    return _normalize_cached(name if isinstance(name, str) else str(name))


@lru_cache(maxsize=_NORMALIZE_CACHE_SIZE)
def is_personal_field(name: str) -> bool:
    """Alan kisisel veri tasiyor mu (KVKK/GDPR anlaminda)?"""
    normalized = _normalize(name)
    return any(marker in normalized for marker in _PERSONAL_MARKERS)


@lru_cache(maxsize=_NORMALIZE_CACHE_SIZE)
def _classify_normalized(normalized: str) -> DataClass | None:
    """Merkezi envanter + alt-dize kurallari. Politika override'i HARIC.

    Override'lar tool'a ozgudur ve burada degerlendirilemez; bu fonksiyon
    yalniz global kurallari onbellekler.
    """
    known = FIELD_CLASS_INVENTORY.get(normalized)
    if known is not None:
        return known
    for needles, data_class in _SUBSTRING_RULES:
        if any(needle in normalized for needle in needles):
            return data_class
    return None


def classify_field(
    name: str,
    *,
    overrides: Mapping[str, DataClass] | None = None,
    default: DataClass = DataClass.D1,
) -> DataClass:
    """Bir alan adinin veri sinifini cozer.

    Sira: tool'a ozgu `overrides` -> merkezi envanter -> alt-dize kurallari ->
    `default`. Tool'un kendi bildirimi merkezi envanteri **yukseltebilir de
    dusurebilir de**; bu bilincli bir tercihtir, cunku ayni ad farkli baglamda
    farkli anlama gelebilir (`amount` bir sayaç da olabilir). Ancak bir tool'un
    D3 bir alani D1'e dusurmesi `DataPolicy.validate()` ile denetlenir.
    """
    normalized = _normalize(name)
    if overrides:
        for key, value in overrides.items():
            if _normalize(key) == normalized:
                return value
    resolved = _classify_normalized(normalized)
    return resolved if resolved is not None else default


def walk_fields(payload: Any, *, prefix: str = "", max_depth: int = 12) -> list[tuple[str, str, Any]]:
    """Ic ice payload'i `(yol, alan_adi, deger)` uclulerine acar.

    Liste indeksleri yola eklenir ama alan adi degismez: `items[3].net_price`
    ile `net_price` ayni kurala tabidir.
    """
    out: list[tuple[str, str, Any]] = []

    def _walk(node: Any, path: str, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                if isinstance(value, Mapping | list | tuple):
                    _walk(value, child, depth + 1)
                else:
                    out.append((child, str(key), value))
        elif isinstance(node, list | tuple):
            for index, value in enumerate(node):
                child = f"{path}[{index}]"
                if isinstance(value, Mapping | list | tuple):
                    _walk(value, child, depth + 1)
                else:
                    out.append((child, path.rsplit(".", 1)[-1], value))

    _walk(payload, prefix, 0)
    return out


@dataclass(frozen=True)
class DataPolicy:
    """Bir tool'un alan bazli veri sozlesmesi.

    Alanlar:
        default_class   Bildirilmemis alanlarin sinifi (gelistirme profilinde).
        fields          Alan adi -> sinif override'lari.
        model_allowed   Modele verilmesine acikca izin verilen alan adlari.
                        Bos ise sinif kurallari gecerlidir; dolu ise D2 ve uzeri
                        alanlar yalniz bu listede varsa modele gider.
        export_scope    Toplu disa aktarma icin gereken ek kapsam.
        purpose         Isleme amaci; audit ve KVKK/GDPR kaydinda tutulur.
        data_owner      Veri sahibi birim.
        retention_minutes  Tool sonucunun evidence store'da tutulma suresi.
    """

    default_class: DataClass = DataClass.D1
    fields: Mapping[str, DataClass] = field(default_factory=dict)
    model_allowed: tuple[str, ...] = ()
    export_scope: str = ""
    purpose: str = "sap_operations"
    data_owner: str = "sap_process_owner"
    retention_minutes: int = 120

    # Normalize edilmis arama tablolari. Eskiden `classify()` her alan icin
    # TUM politika anahtarlarini yeniden normalize ediyordu; 200 kayitlik bir
    # sonucta bu on binlerce gereksiz regex cagrisi demekti. Anahtarlar sabit
    # oldugu icin bir kez hesaplanir. `compare=False`: frozen dataclass'in
    # esitlik/hash sozlesmesine karismaz.
    _field_index: dict[str, DataClass] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _model_allowed_index: frozenset[str] = field(
        default_factory=frozenset, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "_field_index", {_normalize(k): v for k, v in self.fields.items()}
        )
        object.__setattr__(
            self,
            "_model_allowed_index",
            frozenset(_normalize(name) for name in self.model_allowed),
        )

    def classify(self, field_name: str, *, strict: bool = False) -> DataClass:
        """Alan sinifini dondurur.

        `strict=True` (uretim profili) bilinmeyen alani D3 kabul eder:
        siniflandirilmamis veri modele/loga/cache'e sizmaz.
        """
        normalized = _normalize(field_name)
        override = self._field_index.get(normalized)
        if override is not None:
            return override
        resolved = _classify_normalized(normalized)
        if resolved is not None:
            return resolved
        return DataClass.D3 if strict else self.default_class

    def is_model_allowed(self, field_name: str, data_class: DataClass) -> bool:
        """Alan modele verilebilir mi?

        `model_allowed` bir **allowlist**tir: doldurulmussa D2/D3 alanlar yalniz
        listede yer aliyorsa modele gider. D3 hicbir kosulda ham gitmez.
        """
        if data_class is DataClass.D3:
            return False
        if not self.model_allowed or data_class.level <= DataClass.D1.level:
            return True
        return _normalize(field_name) in self._model_allowed_index

    @property
    def max_declared_class(self) -> DataClass:
        return max_class([self.default_class, *self.fields.values()])

    def validate(self) -> list[str]:
        """Sozlesme tutarliligi. Kayit sirasinda cagrilir."""
        problems: list[str] = []
        for name, declared in self.fields.items():
            central = FIELD_CLASS_INVENTORY.get(_normalize(name))
            if central is not None and declared.level < central.level:
                problems.append(
                    f"'{name}' alani merkezi envanterde {central.value}; tool "
                    f"{declared.value} bildiremez (siniflandirma dusurulemez)."
                )
        for name in self.model_allowed:
            declared = self.classify(name)
            if declared is DataClass.D3:
                problems.append(
                    f"'{name}' D3 sinifinda; model_allowed listesinde yer alamaz."
                )
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_class": self.default_class.value,
            "max_class": self.max_declared_class.value,
            "fields": {k: v.value for k, v in sorted(self.fields.items())},
            "model_allowed": list(self.model_allowed),
            "export_scope": self.export_scope,
            "purpose": self.purpose,
            "data_owner": self.data_owner,
            "retention_minutes": self.retention_minutes,
        }
