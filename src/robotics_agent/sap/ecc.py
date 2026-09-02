"""SAP ECC 6.0 EHP8 backend'i (Z-Gateway OData V2).

Bu dosya `odata.py`'nin kopyasi **degildir** ve olmamalidir. S/4 adapteri V4
`$expand`, ETag ve released API sozlesmesi uzerine kuruludur; ECC'de bunlarin
hicbiri yoktur. Sorgu desenleri temelden farklidir:

    S/4 (odata.py)                      ECC (bu dosya)
    ------------------------------      ------------------------------
    V4 $expand ile derin okuma          CDS'te join, tek duz entity set
    substringof(...) iki asamali arama  CDS'te birlesik aciklama alani
    ETag ile optimistic concurrency     ENQUEUE + ZAGENT_IDEMPOTENCY
    Baslik metnine gomulu REF# hash'i   Anahtar esitligiyle mutabakat
    Kalem basina ek gecmis GET'i        EKBE ozetleri CDS icinde toplanmis

Veri dogrulugu invariantlari (odata.py ile ayni sozlesme, farkli yol):

  A. Malzeme aramasi aciklamada da arar; siniflandirma yalniz gerektiginde
     okunur (her aramada karakteristik cekmek bosa cagridir).
  B. Stok fotografi ATP yerine gecirilmez; MRP arz/talep ayri porttur.
  C. Belge akisi EKBE'den kurulur. Her dugum kendisini onceki belgeye baglayan
     **gercek SAP alanini** tasir; bag gosterilemiyorsa dugum eklenmez.
  D. Tedarikci skorlari ELBK/ELBP'den okunur. ECC'de standart olmayan alanlar
     (zamaninda teslim %, kalite PPM) uydurulmaz; `estimated_fields` ile
     isaretlenir.
  E. Yazma tek yoldan gecer: PR olusturma. Idempotency SAP tarafinda
     ZAGENT_IDEMPOTENCY tablosunda tutulur, ayni LUW'da yazilir.

Servis yollari ve beklenen alanlar `adapters.ecc.capabilities` manifestindedir.
Hedef sistemde kontrat farkliysa `sap_discover_capabilities` bunu raporlar; kod
sessiz bos veri dondurmez.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import threading
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..adapters.ecc import (
    ABAP_SOURCES,
    ECC_CAPABILITY_MANIFEST,
    IDEMPOTENCY_ENTITY_SET,
    MRP_ELEMENT_LABELS,
    ecc_manifest_summary,
)
from ..adapters.ecc.capabilities import DEMAND_ELEMENTS, distinct_service_paths
from ..adapters.sap import (
    ODataHttpCore,
    ODataV2Client,
    SAPCallBudget,
    SAPError,
    breaker_for,
    build_http_client,
    expanded_rows,
    parse_metadata,
    parse_odata_datetime,
    quote,
    resolve_connection,
    verify_contract,
)
from ..core.tenant_profile import DEFAULT_DOCUMENT_TYPE
from .base import SAPBackend, effective_unit_price, wbs_matches
from .models import (
    DocumentFlowNode,
    GoodsReceipt,
    InfoRecord,
    InvoiceBlock,
    Material,
    MaterialClassification,
    PurchaseOrder,
    PurchaseOrderItem,
    PurchaseRequisitionDraft,
    PurchaseRequisitionItem,
    PurchaseRequisitionResult,
    ScheduleLine,
    StockLevel,
    SupplierInvoice,
    SupplierScore,
    SupplyDemandItem,
    ValidationFinding,
    Vendor,
)

log = logging.getLogger(__name__)

_MATERIAL_TYPE_FALLBACK = {"FERT", "HALB", "ROH", "HIBE", "DIEN"}

#: EKBE-BEWTP gecmis kategorileri.
_HIST_GOODS_RECEIPT = "E"
_HIST_INVOICE_RECEIPT = "Q"

#: Iptal / iade hareket tipleri (MSEG-BWART).
_REVERSAL_MOVEMENTS = {"102", "122", "162", "106", "124"}

#: ZAGENT_IDEMPOTENCY.OBJECT_TYPE degeri.
_IDEMPOTENCY_OBJECT_PR = "PURCHASE_REQUISITION"

#: ZAGENT_IDEMPOTENCY.IDEMPOTENCY_KEY alan genisligi (CHAR64).
_IDEMPOTENCY_KEY_WIDTH = 64


# ---------------------------------------------------------------------------
# OData V2 literal yardimcilari
# ---------------------------------------------------------------------------
def _decimal_literal(value: float) -> str:
    """OData V2 Edm.Decimal literali. `m` soneki spesifikasyon geregidir."""
    return f"{float(value):.3f}m"


def _date_literal(value: date) -> str:
    """OData V2 Edm.DateTime literali (gun basi)."""
    return f"datetime'{value.isoformat()}T00:00:00'"


def _or_filter(field: str, values: Iterable[str]) -> str:
    """`field eq 'a' or field eq 'b'` - coklu anahtari TEK cagriya indirir.

    Malzeme listesi icin dongu kurup her birine ayri GET atmak N+1 uretir ve
    `PerformanceBudget.max_sap_calls` sozlesmesini kirar.
    """
    clauses = [f"{field} eq '{quote(v)}'" for v in values if v]
    return f"({' or '.join(clauses)})" if clauses else ""


def _and(*clauses: str) -> str:
    return " and ".join(c for c in clauses if c)


def _num(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _opt_num(value: Any) -> float | None:
    """Bos degeri 0.0'a DUSURMEZ. Bilinmeyen ile sifir ayri seylerdir."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _flag(value: Any) -> bool:
    """ABAP tek karakterli bayrak ('X'/'') veya Edm.Boolean."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().upper() in {"X", "TRUE", "1"}


def _coerce_number(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, int | float):
        return value
    text = str(value).strip()
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return text
    return int(number) if number.is_integer() else number


def reference_token(idempotency_key: str) -> str:
    """Idempotency anahtarinin ZAGENT_IDEMPOTENCY'de saklanan hali.

    S/4 adapterinden farki: burada **hash zorunlu degildir**. PR baslik metni
    40 karakterle sinirli oldugu icin S/4 tarafi sha256'nin ilk 16 hanesini
    kullanir; ECC'de anahtar CHAR64 bir tabloda yasar, dolayisiyla anahtarin
    kendisi saklanir. Yalniz 64 karakteri asarsa hash'e duser.
    """
    if len(idempotency_key) <= _IDEMPOTENCY_KEY_WIDTH:
        return idempotency_key
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:_IDEMPOTENCY_KEY_WIDTH]


class ECCSAPBackend(SAPBackend):
    """ECC 6.0 EHP8 OData V2 istemcisi (embedded SAP_GWFND 7.50)."""

    name = "ecc"

    def __init__(self, settings) -> None:
        self.settings = settings
        #: Aktif tenant profili (belge tipi, zorunlu alanlar, Z-alan eslemesi).
        self._profile: Any = None
        self._call_budget_lock = threading.Lock()
        cfg = settings.sap
        problems = cfg.validate()
        if problems:
            raise SAPError("SAP konfigurasyonu eksik: " + "; ".join(problems), code="CONFIG")

        self.connection = resolve_connection(cfg)
        for warning in self.connection.warnings:
            log.warning("SAP baglanti uyarisi: %s", warning)

        http_client = build_http_client(self.connection, cfg)
        # ECC'de TEK protokol vardir: OData V2. V4 istemcisi bilerek kurulmaz -
        # yanlislikla V4 yoluna sapmak calisma zamaninda 404 uretirdi.
        self.breaker = breaker_for(cfg)
        self._core = ODataHttpCore(
            client=http_client,
            odata_version="v2",
            sap_client=cfg.client,
            accept_language=cfg.description_language,
            allowed_hosts=settings.security.allowed_sap_hosts,
            read_only=cfg.read_only,
            token_provider=self.connection.token_provider,
            breaker=self.breaker,
        )
        self.v2 = ODataV2Client(self._core, page_size=cfg.page_size, max_pages=cfg.max_pages)
        self._metadata_cache: dict[str, Any] = {}

    def close(self) -> None:
        self._core.close()

    @property
    def sap_call_count(self) -> int:
        return self._core.call_count

    @contextlib.contextmanager
    def enforce_call_budget(self, max_calls: int):
        """ECC V2 gidiş-donuslarini tool butcesinde gercekten sinirlar."""
        budget = SAPCallBudget(max_calls=max_calls)
        with self._call_budget_lock:
            self._core.call_budget = budget
            try:
                yield
            finally:
                self._core.call_budget = None

    # --- Servis erisimi -----------------------------------------------------
    def _service(self, alias: str) -> str:
        capability = ECC_CAPABILITY_MANIFEST.get(alias)
        if capability is None:
            raise SAPError(f"Bilinmeyen ECC servis alias'i: {alias}", code="UNKNOWN_SERVICE")
        return capability.service_path

    def _read(
        self,
        alias: str,
        entity_set: str,
        *,
        filter_expr: str = "",
        select: Sequence[str] | None = None,
        expand: Sequence[str] | None = None,
        top: int = 100,
        correlation_id: str = "",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"$top": top}
        if filter_expr:
            params["$filter"] = filter_expr
        if select:
            params["$select"] = ",".join(select)
        if expand:
            params["$expand"] = ",".join(expand)
        return self.v2.read(
            self._service(alias), entity_set, params=params, correlation_id=correlation_id
        )

    # --- Yetenek kesfi ------------------------------------------------------
    def metadata_contract(self, alias: str, *, correlation_id: str = ""):
        """Servisin $metadata sozlesmesini onbellekli okur."""
        if alias in self._metadata_cache:
            return self._metadata_cache[alias]
        capability = ECC_CAPABILITY_MANIFEST[alias]
        raw = self.v2.metadata(capability.service_path, correlation_id=correlation_id)
        contract = parse_metadata(raw)
        self._metadata_cache[alias] = contract
        return contract

    def probe_capabilities(self, aliases: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Manifestteki servisleri hedef ECC sisteminde dogrular.

        Birden fazla alias ayni SICF yolunu paylasabilir (ornegin product /
        classification / valuation hepsi ZAGENT_MM_MATERIAL_SRV'dir). $metadata
        cache'i sayesinde ayni belge bir kez okunur.
        """
        keys = list(aliases) if aliases else list(ECC_CAPABILITY_MANIFEST)
        out: list[dict[str, Any]] = []
        for alias in keys:
            capability = ECC_CAPABILITY_MANIFEST.get(alias)
            if capability is None:
                continue
            started = datetime.now(timezone.utc)
            try:
                contract = self.metadata_contract(alias)
                check = verify_contract(capability, contract)
            except SAPError as exc:
                out.append(
                    {
                        "alias": alias,
                        "available": False,
                        "contract_ok": False,
                        "status": capability.status,
                        "expected_odata": capability.odata_version,
                        "service": capability.service_path,
                        "error": str(exc),
                        "remediation": (
                            "Servis SICF'te aktif mi (/IWFND/MAINT_SERVICE) ve kullanicinin "
                            "S_SERVICE yetkisi var mi kontrol edin."
                        ),
                    }
                )
                continue
            payload = check.to_dict()
            payload["service"] = capability.service_path
            payload["latency_ms"] = round(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000, 1
            )
            if not check.contract_ok:
                payload["abap_sources"] = {
                    k: list(v) for k, v in ABAP_SOURCES.get(alias, {}).items()
                }
            out.append(payload)
        return out

    def service_manifest(self, aliases: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Backend'e ozgu servis manifesti (`sap_discover_capabilities` kullanir)."""
        return ecc_manifest_summary(aliases)

    def manifest_aliases(self) -> frozenset[str]:
        return frozenset(ECC_CAPABILITY_MANIFEST)

    def preferred_service_order(self) -> str:
        """ECC'de tercih sirasi S/4'ten farklidir ve model bunu bilmelidir.

        Released public API olmadigi icin "released once" kurali burada
        uygulanamaz; onemli olan Z servisin BAPI sarmaladigi, dogrudan tablo
        okumadigidir.
        """
        return (
            "Z-Gateway OData V2 (CDS + BAPI sarmalayici) -> BAPI/RFC -> dogrudan tablo "
            "okuma (yalniz belgelenmis istisna). ECC 6.0 EHP8'de released public OData "
            "API ve OData V4 YOKTUR."
        )

    def set_active_profile(self, profile: Any) -> None:
        self._profile = profile

    @property
    def document_type(self) -> str:
        """Belge tipi sirkete gore degisir; profil yoksa SAP standardi."""
        return getattr(self._profile, "document_type", None) or DEFAULT_DOCUMENT_TYPE

    def capabilities(self) -> dict[str, Any]:
        payload = super().capabilities()
        payload["connection"] = self.connection.describe()
        payload["odata_preference"] = "v2"
        payload["platform"] = "SAP ECC 6.0 EHP8 / NetWeaver 7.50 (SAP_GWFND 7.50)"
        payload["services"] = list(distinct_service_paths())
        payload["notes"] = [
            "Tum servisler Z (custom) namespace'indedir; released public API yoktur.",
            "OData V4 bu platformda kullanilamaz (RAP icin ABAP 7.53+ gerekir).",
            "Concurrency ETag ile degil, ENQUEUE + ZAGENT_IDEMPOTENCY ile saglanir.",
        ]
        return payload

    def ping(self) -> dict[str, str]:
        try:
            self._read("product", "MaterialSet", top=1, select=("Material",))
        except SAPError as exc:
            return {"backend": self.name, "status": "error", "detail": str(exc)}
        return {
            "backend": self.name,
            "status": "ok",
            "host": self.connection.base_url,
            "client": self.settings.sap.client,
            "auth": self.connection.describe()["auth"],
        }

    # =======================================================================
    # ProductPort
    # =======================================================================
    def search_materials(
        self,
        query: str = "",
        *,
        material_group: str | None = None,
        plant: str | None = None,
        attribute_filters: dict[str, tuple[float, float]] | None = None,
        limit: int = 20,
    ) -> list[Material]:
        """Serbest metin + malzeme grubu + karakteristik araligi ile arama.

        S/4 adapteri once aciklamada, sonra malzeme numarasinda iki ayri cagri
        yapar. ECC'de `MaterialSet` CDS'i MARA+MARC+MAKT'i birlestirdigi icin
        **tek cagri** yeter: arama her iki alanda ayni $filter icinde yapilir.
        """
        target_plant = plant or self.settings.sap.plant
        clauses: list[str] = [f"Plant eq '{quote(target_plant)}'"]

        if query:
            tokens = [t for t in query.split() if t]
            token_clauses = [
                f"(substringof('{quote(t)}',MaterialDescription) "
                f"or substringof('{quote(t)}',Material))"
                for t in tokens
            ]
            if token_clauses:
                clauses.append(f"({' or '.join(token_clauses)})")
        if material_group:
            clauses.append(f"MaterialGroup eq '{quote(material_group)}'")

        # Karakteristik filtresi varsa aday havuzu genis tutulur: eleme
        # siniflandirma okumasindan sonra yapilir.
        fetch = limit * 4 if attribute_filters else limit
        rows = self._read("product", "MaterialSet", filter_expr=_and(*clauses), top=fetch)
        materials = [self._map_material(r) for r in rows]

        if attribute_filters:
            materials = self._filter_by_characteristics(materials, attribute_filters)

        return materials[:limit]

    def _filter_by_characteristics(
        self, materials: list[Material], filters: dict[str, tuple[float, float]]
    ) -> list[Material]:
        """Siniflandirmayi TEK cagride okur ve araliga gore eler.

        Malzeme basina ayri siniflandirma cagrisi N+1 uretirdi; burada tum
        adaylarin karakteristikleri tek $filter ile cekilir.
        """
        ids = [m.material_id for m in materials if m.material_id]
        if not ids:
            return []
        id_filter = _or_filter("Material", ids)
        rows = self._read(
            "classification",
            "MaterialCharcValueSet",
            filter_expr=_and(id_filter, "ClassType eq '001'"),
            top=max(200, len(ids) * 20),
        )
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            material_id = str(row.get("Material", ""))
            name = str(row.get("Characteristic") or "").strip().lower()
            if not material_id or not name:
                continue
            grouped.setdefault(material_id, {})[name] = _coerce_number(row.get("CharcValue"))

        out: list[Material] = []
        for material in materials:
            characteristics = grouped.get(material.material_id)
            # Siniflandirmasi okunamayan malzeme ELENIR: "bilinmiyor" ile
            # "araliga uyuyor" ayni sey degildir.
            if not characteristics:
                continue
            material.attributes = dict(characteristics)
            if self._matches(characteristics, filters):
                out.append(material)
        return out

    @staticmethod
    def _matches(characteristics: dict[str, Any], filters: dict[str, tuple[float, float]]) -> bool:
        for key, (low, high) in filters.items():
            raw = characteristics.get(key.strip().lower())
            value = _opt_num(raw)
            if value is None or not (low <= value <= high):
                return False
        return True

    def _map_material(self, row: dict[str, Any]) -> Material:
        material_type = str(row.get("MaterialType") or "ROH")
        return Material(
            material_id=str(row.get("Material", "")),
            description=str(row.get("MaterialDescription") or ""),
            material_type=(
                material_type if material_type in _MATERIAL_TYPE_FALLBACK else "ROH"
            ),
            material_group=str(row.get("MaterialGroup") or ""),
            base_unit=str(row.get("BaseUnit") or "ST"),
            gross_weight_kg=_opt_num(row.get("GrossWeight")),
            procurement_type=(
                str(row.get("ProcurementType") or "F")
                if str(row.get("ProcurementType") or "F") in {"E", "F", "X"}
                else "F"
            ),
            planned_delivery_days=_int(row.get("PlannedDeliveryDays")),
            # MBEW CDS join'den geldigi icin ayri degerleme cagrisi gerekmez.
            moving_avg_price=_num(row.get("MovingAveragePrice")),
            currency=str(row.get("Currency") or self.settings.sap.currency),
            price_unit=_int(row.get("PriceUnit"), 1) or 1,
            min_order_qty=_num(row.get("MinimumLotSize"), 1.0) or 1.0,
            lot_size_key=str(row.get("LotSizeKey") or "EX"),
            mrp_controller=str(row.get("MRPController") or ""),
            abc_indicator=str(row.get("ABCIndicator") or ""),
            plant=str(row.get("Plant") or ""),
            attributes={},
        )

    def get_material(self, material_id: str, *, plant: str | None = None) -> Material | None:
        target_plant = plant or self.settings.sap.plant
        rows = self._read(
            "product",
            "MaterialSet",
            filter_expr=_and(
                f"Material eq '{quote(material_id)}'", f"Plant eq '{quote(target_plant)}'"
            ),
            top=1,
        )
        return self._map_material(rows[0]) if rows else None

    def get_valuation(self, material_id: str, *, plant: str | None = None) -> dict[str, Any] | None:
        """Degerleme (MBEW). `MaterialSet` zaten fiyat tasir; bu port ayri
        okuma yolu isteyen cagirici icindir (or. degerleme alani != tesis)."""
        valuation_area = plant or self.settings.sap.plant
        try:
            rows = self._read(
                "valuation",
                "MaterialValuationSet",
                filter_expr=_and(
                    f"Material eq '{quote(material_id)}'",
                    f"ValuationArea eq '{quote(valuation_area)}'",
                ),
                top=5,
            )
        except SAPError as exc:
            log.info("Degerleme okunamadi (%s): %s", material_id, exc)
            return None
        if not rows:
            return None
        row = rows[0]
        return {
            "moving_avg_price": _num(row.get("MovingAveragePrice")),
            "standard_price": _num(row.get("StandardPrice")),
            "price_control": str(row.get("PriceControl") or ""),
            "currency": str(row.get("Currency") or self.settings.sap.currency),
            "price_unit": _int(row.get("PriceUnit"), 1) or 1,
            "source_api": self._service("valuation"),
        }

    def get_material_classification(
        self, material_id: str, *, class_type: str = "001"
    ) -> MaterialClassification | None:
        service = self._service("classification")
        rows = self._read(
            "classification",
            "MaterialCharcValueSet",
            filter_expr=_and(
                f"Material eq '{quote(material_id)}'", f"ClassType eq '{quote(class_type)}'"
            ),
            top=200,
        )
        if not rows:
            # "Sinif atanmamis" ile "okunamadi" ayri seylerdir: bos ama gecerli
            # bir siniflandirma dondurulur ki cagiran ikisini ayirt edebilsin.
            return MaterialClassification(
                material_id=material_id,
                class_type=class_type,
                characteristics={},
                units={},
                source=service,
            )

        characteristics: dict[str, Any] = {}
        units: dict[str, str] = {}
        for row in rows:
            name = str(row.get("Characteristic") or "").strip().lower()
            if not name:
                continue
            characteristics[name] = _coerce_number(row.get("CharcValue"))
            unit = str(row.get("CharcValueUnit") or "")
            if unit:
                units[name] = unit

        class_rows = self._read(
            "classification",
            "MaterialClassSet",
            filter_expr=_and(
                f"Material eq '{quote(material_id)}'", f"ClassType eq '{quote(class_type)}'"
            ),
            top=5,
        )
        return MaterialClassification(
            material_id=material_id,
            class_type=class_type,
            class_name=str(class_rows[0].get("ClassName", "")) if class_rows else "",
            characteristics=characteristics,
            units=units,
            source=service,
        )

    # =======================================================================
    # PlanningPort
    # =======================================================================
    def get_stock(self, material_ids: list[str], *, plant: str | None = None) -> list[StockLevel]:
        """Depo yeri satirlarini toplayarak malzeme/tesis stogu uretir.

        Tum malzemeler TEK cagride okunur (S/4 adapteri malzeme basina dongu
        kurar). Emniyet stogu tesis bazlidir: depo yeri satirlarinda tekrar
        eder, bu yuzden toplanmaz - maksimumu alinir.
        """
        target_plant = plant or self.settings.sap.plant
        ids = [m for m in material_ids if m]
        if not ids:
            return []

        rows = self._read(
            "stock",
            "StockSet",
            filter_expr=_and(
                _or_filter("Material", ids), f"Plant eq '{quote(target_plant)}'"
            ),
            top=max(200, len(ids) * 10),
        )

        levels: dict[str, StockLevel] = {
            mid: StockLevel(material_id=mid, plant=target_plant) for mid in ids
        }
        for row in rows:
            material_id = str(row.get("Material", ""))
            level = levels.get(material_id)
            if level is None:
                continue
            level.unrestricted_qty += _num(row.get("UnrestrictedQuantity"))
            level.quality_inspection_qty += _num(row.get("QualityInspectionQuantity"))
            level.blocked_qty += _num(row.get("BlockedQuantity"))
            level.safety_stock = max(level.safety_stock, _num(row.get("SafetyStock")))
            location = str(row.get("StorageLocation") or "")
            if location:
                level.storage_location = location
            unit = str(row.get("BaseUnit") or "")
            if unit:
                level.unit = unit

        # Acik siparis ve rezervasyon ayri portlardan gelir; ikisi de toplu okunur.
        on_order = self._open_po_quantities(ids, target_plant)
        reserved = self._reservation_quantities(ids, target_plant)
        for material_id, level in levels.items():
            level.on_order_qty = on_order.get(material_id, 0.0)
            if material_id in reserved:
                level.reserved_qty = reserved[material_id]
            level.unrestricted_qty = round(level.unrestricted_qty, 3)
            level.quality_inspection_qty = round(level.quality_inspection_qty, 3)
            level.blocked_qty = round(level.blocked_qty, 3)
        return list(levels.values())

    def _open_po_quantities(self, material_ids: list[str], plant: str) -> dict[str, float]:
        """Acik siparis miktari = siparis - teslim edilen (EKBE toplami CDS'te).

        Teslim edilmis miktar mutlaka dusulur; aksi halde acik siparis sisirilir.
        """
        rows = self._read(
            "purchase_order",
            "PurchaseOrderItemSet",
            filter_expr=_and(
                _or_filter("Material", material_ids),
                f"Plant eq '{quote(plant)}'",
                "DeletionIndicator eq ''",
            ),
            select=(
                "Material",
                "Quantity",
                "DeliveredQuantity",
            ),
            top=500,
        )
        out: dict[str, float] = {}
        for row in rows:
            material_id = str(row.get("Material", ""))
            if not material_id:
                continue
            open_qty = _num(row.get("Quantity")) - _num(row.get("DeliveredQuantity"))
            out[material_id] = round(out.get(material_id, 0.0) + max(0.0, open_qty), 3)
        return out

    def _reservation_quantities(self, material_ids: list[str], plant: str) -> dict[str, float]:
        """MD04 talep elementlerinden rezervasyon toplami.

        Okunamazsa **bos sozluk** doner: `reserved_qty` 0.0'a zorlanmaz, cagiran
        "rezervasyon bilinmiyor" ile "rezervasyon yok"u ayirt edebilir.
        """
        try:
            rows = self._read(
                "mrp",
                "SupplyDemandSet",
                filter_expr=_and(
                    _or_filter("Material", material_ids), f"Plant eq '{quote(plant)}'"
                ),
                select=("Material", "MRPElement", "Quantity"),
                top=1000,
            )
        except SAPError as exc:
            log.info("Rezervasyon (MD04) okunamadi: %s", exc)
            return {}
        out: dict[str, float] = {}
        for row in rows:
            element = str(row.get("MRPElement") or "").upper()
            if element not in DEMAND_ELEMENTS:
                continue
            material_id = str(row.get("Material", ""))
            if not material_id:
                continue
            # MD04'te talep negatif isaretlidir; rezervasyon pozitif raporlanir.
            out[material_id] = round(out.get(material_id, 0.0) - _num(row.get("Quantity")), 3)
        return out

    def get_supply_demand(
        self,
        material_id: str,
        *,
        plant: str | None = None,
        horizon_days: int = 180,
    ) -> list[SupplyDemandItem]:
        """MD04 arz/talep listesi (MD_STOCK_REQUIREMENTS_LIST_API)."""
        target_plant = plant or self.settings.sap.plant
        horizon = date.today() + timedelta(days=horizon_days)
        rows = self._read(
            "mrp",
            "SupplyDemandSet",
            filter_expr=_and(
                f"Material eq '{quote(material_id)}'", f"Plant eq '{quote(target_plant)}'"
            ),
            top=500,
        )
        items: list[SupplyDemandItem] = []
        for row in rows:
            when = parse_odata_datetime(row.get("AvailabilityDate"))
            if when and when > horizon:
                continue
            element = str(row.get("MRPElement") or "").upper()
            description = str(row.get("ElementDescription") or "")
            items.append(
                SupplyDemandItem(
                    material_id=material_id,
                    plant=target_plant,
                    mrp_element=element,
                    element_id=str(row.get("MRPElementId") or ""),
                    availability_date=when,
                    quantity=_num(row.get("Quantity")),
                    unit=str(row.get("BaseUnit") or "ST"),
                    # Servis aciklama vermezse kod tablosundan tamamlanir;
                    # bilinmeyen kod oldugu gibi birakilir, uydurulmaz.
                    description=description or MRP_ELEMENT_LABELS.get(element, element),
                    wbs_element=str(row.get("WBSElement") or "") or None,
                )
            )
        items.sort(key=lambda i: (i.availability_date or date.today(), -i.quantity))
        return items

    # =======================================================================
    # ProcurementPort
    # =======================================================================
    def get_info_records(self, material_id: str, *, plant: str | None = None) -> list[InfoRecord]:
        """Bilgi kaydi (EINA/EINE) + kademeli fiyat (KONM), tek cagride.

        S/4 adapteri tedarikci adlarini ayrica $batch ile tamamlar. ECC'de
        `InfoRecordSet` CDS'i LFA1'i zaten join ettigi icin ikinci tur gerekmez.
        """
        cfg = self.settings.sap
        rows = self._read(
            "inforecord",
            "InfoRecordSet",
            filter_expr=_and(
                f"Material eq '{quote(material_id)}'",
                f"PurchasingOrganization eq '{quote(cfg.purch_org)}'",
                "DeletionIndicator eq ''",
            ),
            expand=("ToScales",),
            top=50,
        )
        records: list[InfoRecord] = []
        for row in rows:
            scales: dict[str, float] = {}
            for scale in expanded_rows(row, "ToScales"):
                qty = _opt_num(scale.get("ScaleQuantity"))
                price = _opt_num(scale.get("ScalePrice"))
                if qty is not None and price is not None:
                    scales[str(qty)] = price
            records.append(
                InfoRecord(
                    material_id=material_id,
                    vendor_id=str(row.get("Supplier", "")),
                    vendor_name=str(row.get("SupplierName") or ""),
                    net_price=_num(row.get("NetPrice")),
                    currency=str(row.get("Currency") or cfg.currency),
                    price_unit=_int(row.get("PriceUnit"), 1) or 1,
                    min_order_qty=_num(row.get("MinimumQuantity"), 1.0) or 1.0,
                    planned_delivery_days=_int(row.get("PlannedDeliveryDays"), 14),
                    incoterms=str(row.get("Incoterms") or "DAP"),
                    payment_terms=str(row.get("PaymentTerms") or "NT30"),
                    valid_to=parse_odata_datetime(row.get("ValidTo")),
                    scale_prices=scales,
                )
            )
        return records

    def get_vendor(self, vendor_id: str) -> Vendor | None:
        rows = self._read(
            "supplier",
            "SupplierSet",
            filter_expr=f"Supplier eq '{quote(vendor_id)}'",
            top=1,
        )
        if not rows:
            return None
        row = rows[0]
        vendor = Vendor(
            vendor_id=str(row.get("Supplier", vendor_id)),
            name=str(row.get("SupplierName") or ""),
            country=str(row.get("Country") or ""),
            city=str(row.get("City") or ""),
            blocked=_flag(row.get("PurchasingBlock")),
        )
        # Performans alanlari ana veride yoktur; degerlendirmeden gelirse
        # doldurulur, gelmezse 0 kalir ve skor tool'u bunu isaretler.
        score = self.get_supplier_score(vendor_id)
        if score is not None:
            vendor.on_time_delivery_pct = score.on_time_delivery_pct or 0.0
            vendor.quality_ppm = score.quality_ppm or 0
            vendor.price_competitiveness = score.price_score or 0.0
            vendor.responsiveness = score.service_score or 0.0
        return vendor

    def get_supplier_score(
        self, vendor_id: str, *, purchasing_org: str | None = None
    ) -> SupplierScore | None:
        """Klasik ECC tedarikci degerlendirmesi (ELBK/ELBP, ME6H).

        S/4'un operasyonel skor CDS'i ECC'de yoktur. `on_time_delivery_pct` ve
        `quality_ppm` standart alan olmadigi icin servis bunlari bos birakabilir;
        bos gelen her alan `estimated_fields` ile **acikca** isaretlenir. Sifir
        dondurup gercek veri gibi gostermek yanlis karara yol acardi.
        """
        service = f"{self._service('supplier_score')}/SupplierScoreSet"
        org = purchasing_org or self.settings.sap.purch_org
        try:
            rows = self._read(
                "supplier_score",
                "SupplierScoreSet",
                filter_expr=_and(
                    f"Supplier eq '{quote(vendor_id)}'",
                    f"PurchasingOrganization eq '{quote(org)}'",
                ),
                top=5,
            )
        except SAPError as exc:
            log.info("Tedarikci skoru okunamadi (%s): %s", vendor_id, exc)
            return SupplierScore(
                vendor_id=vendor_id,
                purchasing_org=org,
                source_api=service,
                estimated_fields=[
                    "overall_score",
                    "price_score",
                    "delivery_score",
                    "quantity_score",
                    "quality_score",
                    "on_time_delivery_pct",
                    "quality_ppm",
                ],
            )
        if not rows:
            return SupplierScore(
                vendor_id=vendor_id,
                purchasing_org=org,
                source_api=service,
                estimated_fields=["overall_score", "delivery_score", "quality_score"],
                evaluated_period="",
            )

        row = rows[0]
        score = SupplierScore(
            vendor_id=vendor_id,
            purchasing_org=org,
            overall_score=_opt_num(row.get("OverallScore")),
            price_score=_opt_num(row.get("PriceScore")),
            delivery_score=_opt_num(row.get("DeliveryScore")),
            quantity_score=_opt_num(row.get("QuantityScore")),
            quality_score=_opt_num(row.get("QualityScore")),
            service_score=_opt_num(row.get("ServiceScore")),
            on_time_delivery_pct=_opt_num(row.get("OnTimeDeliveryPct")),
            quality_ppm=(
                _int(row["QualityPPM"]) if row.get("QualityPPM") not in (None, "") else None
            ),
            evaluated_period=str(row.get("EvaluationPeriod") or ""),
            source_api=service,
        )
        # ECC'de standart olmayan alanlar bos ise bunu gizleme.
        missing = [
            name
            for name, value in (
                ("on_time_delivery_pct", score.on_time_delivery_pct),
                ("quality_ppm", score.quality_ppm),
                ("quantity_score", score.quantity_score),
            )
            if value is None
        ]
        score.estimated_fields = missing
        return score

    # --- PR: prepare / submit / read ---------------------------------------
    def prepare_purchase_requisition(
        self,
        items: list[PurchaseRequisitionItem],
        *,
        header_text: str = "",
        purchase_group: str | None = None,
    ) -> PurchaseRequisitionDraft:
        """Taslak uretir, fiyatlar ve dogrular. **Asla yazmaz.**"""
        if not items:
            raise SAPError("Satinalma talebi en az bir kalem icermeli.", code="EBAN_NO_ITEMS")

        cfg = self.settings.sap
        findings: list[ValidationFinding] = []
        priced: list[dict[str, Any]] = []
        odata_items: list[dict[str, Any]] = []
        diff: list[dict[str, Any]] = []
        total = 0.0

        for idx, item in enumerate(items, start=1):
            item_no = idx * 10
            item_plant = item.plant or cfg.plant
            master = self.get_material(item.material_id, plant=item_plant)
            if master is None:
                raise SAPError(
                    f"Malzeme {item.material_id} malzeme ana verisinde bulunamadi "
                    f"(tesis {item_plant}).",
                    code="MM_MATNR_NOT_FOUND",
                )
            records = self.get_info_records(item.material_id, plant=item_plant)

            chosen = None
            if item.preferred_vendor:
                chosen = next(
                    (r for r in records if r.vendor_id == item.preferred_vendor), None
                )
                if chosen is None:
                    findings.append(
                        ValidationFinding(
                            severity="warning",
                            field="preferred_vendor",
                            item_no=item_no,
                            message=(
                                f"Kalem {item_no}: {item.preferred_vendor} icin bilgi kaydi yok."
                            ),
                        )
                    )
            if chosen is None and records:
                chosen = min(records, key=lambda r: r.price_for_qty(item.quantity))

            unit_price, price_warning = effective_unit_price(item.net_price, chosen.price_for_qty(item.quantity) if chosen else master.moving_avg_price)
            if price_warning:
                findings.append(
                    ValidationFinding(
                        severity="warning", field="net_price", item_no=item_no,
                        message=f"Kalem {item_no}: {price_warning}",
                    )
                )
            if not unit_price:
                findings.append(
                    ValidationFinding(
                        severity="warning",
                        field="net_price",
                        item_no=item_no,
                        message=(
                            f"Kalem {item_no}: fiyat bulunamadi (bilgi kaydi ve MBEW bos). "
                            "Tahmini deger sifir; onaya sunmadan once fiyat girilmeli."
                        ),
                    )
                )
            line_total = round(float(unit_price) * item.quantity, 2)
            total += line_total

            moq = chosen.min_order_qty if chosen else master.min_order_qty
            if item.quantity < moq:
                findings.append(
                    ValidationFinding(
                        severity="warning",
                        field="quantity",
                        item_no=item_no,
                        message=(
                            f"Kalem {item_no}: miktar {item.quantity:g} < minimum siparis "
                            f"miktari {moq:g}."
                        ),
                    )
                )

            lead = chosen.planned_delivery_days if chosen else master.planned_delivery_days
            earliest = date.today() + timedelta(days=lead)
            delivery = item.delivery_date or earliest
            if item.delivery_date and item.delivery_date < earliest:
                findings.append(
                    ValidationFinding(
                        severity="warning",
                        field="delivery_date",
                        item_no=item_no,
                        message=(
                            f"Kalem {item_no}: istenen teslim {item.delivery_date}, en erken "
                            f"{earliest} ({lead} gun)."
                        ),
                    )
                )
            if not (item.wbs_element or item.cost_center):
                findings.append(
                    ValidationFinding(
                        severity="warning",
                        field="account_assignment",
                        item_no=item_no,
                        message=f"Kalem {item_no}: hesap atamasi (WBS/masraf merkezi) yok.",
                    )
                )

            # ECC BAPI_PR_CREATE alan adlari; hesap atamasi EBKN'ye gider.
            odata_items.append(
                {
                    "PurchaseRequisitionItem": f"{item_no:05d}",
                    "Material": item.material_id,
                    "Plant": item_plant,
                    "Quantity": str(item.quantity),
                    "BaseUnit": item.unit or master.base_unit,
                    "DeliveryDate": delivery.isoformat(),
                    "PurchasingGroup": purchase_group or cfg.purch_group,
                    "PurchasingOrganization": cfg.purch_org,
                    "CompanyCode": cfg.company_code,
                    "Price": str(round(float(unit_price), 2)),
                    "Currency": item.currency or cfg.currency,
                    "FixedSupplier": item.preferred_vendor or "",
                    "ItemText": item.item_text[:40],
                    "WBSElement": item.wbs_element or "",
                    "CostCenter": item.cost_center or "",
                    # Hesap atama kategorisi: P=proje, K=masraf merkezi, bos=stok
                    "AccountAssignmentCategory": (
                        "P" if item.wbs_element else ("K" if item.cost_center else "")
                    ),
                }
            )
            priced.append(
                {
                    "item_no": item_no,
                    "material_id": item.material_id,
                    "description": master.description,
                    "quantity": item.quantity,
                    "unit": item.unit or master.base_unit,
                    "unit_price": round(float(unit_price), 2),
                    "line_total": line_total,
                    "currency": item.currency or cfg.currency,
                    "vendor_id": chosen.vendor_id if chosen else item.preferred_vendor,
                    "vendor_name": chosen.vendor_name if chosen else None,
                    "lead_time_days": lead,
                    "earliest_delivery": earliest.isoformat(),
                    "requested_delivery": delivery.isoformat(),
                    "plant": item_plant,
                    "wbs_element": item.wbs_element,
                    "cost_center": item.cost_center,
                }
            )
            diff.append(
                {
                    "item_no": item_no,
                    "action": "create",
                    "material_id": item.material_id,
                    "quantity": item.quantity,
                    "unit_price": round(float(unit_price), 2),
                    "line_total": line_total,
                    "currency": item.currency or cfg.currency,
                    "vendor_id": chosen.vendor_id if chosen else None,
                    "delivery_date": delivery.isoformat(),
                    "wbs_element": item.wbs_element,
                }
            )

        total = round(total, 2)
        payload = {
            "DocumentType": self.document_type,
            "HeaderText": header_text[:40],
            "items": odata_items,
        }
        return PurchaseRequisitionDraft(
            draft_id=f"draft-{hashlib.sha256(str(payload).encode()).hexdigest()[:12]}",
            items=priced,
            header_text=header_text,
            purchase_group=purchase_group or cfg.purch_group,
            purchasing_org=cfg.purch_org,
            plant=cfg.plant,
            total_value=total,
            currency=cfg.currency,
            payload=payload,
            findings=findings,
            diff=diff,
            source_api=f"{self._service('purchase_requisition')}/PurchaseRequisitionSet",
            requires_human_approval=total > cfg.approval_threshold,
        )

    def submit_purchase_requisition(
        self,
        draft: PurchaseRequisitionDraft,
        *,
        external_reference: str,
        correlation_id: str = "",
    ) -> PurchaseRequisitionResult:
        """Onaylanmis taslagi ECC'ye yazar (BAPI_PR_CREATE, deep insert).

        Idempotency **SAP tarafinda** zorlanir: Z servisi ayni LUW icinde
        ZAGENT_IDEMPOTENCY'ye anahtari INSERT eder, cakisma olursa yeni belge
        yaratmadan mevcut PR numarasini dondurur. Bu yuzden burada tekrar POST
        etmeye karsi ek bir koruma kurmaya gerek yoktur.

        `etag` alani ECC'de HTTP ETag DEGILDIR: mutabakat anahtaridir. ECC'de
        optimistic concurrency yoktur; kilit ENQUEUE ile pessimistic saglanir.
        """
        if self.settings.sap.read_only:
            raise SAPError(
                "SAP read-only profili etkin; satinalma talebi olusturulamaz.",
                code="READ_ONLY_MODE",
            )
        if not draft.is_submittable:
            raise SAPError(
                "Taslakta engelleyici bulgular var; SAP'a gonderilmedi: "
                + "; ".join(f.message for f in draft.blocking_findings),
                code="EBAN_VALIDATION_FAILED",
            )

        token = reference_token(external_reference)
        body = {
            "DocumentType": draft.payload.get("DocumentType", self.document_type),
            "HeaderText": draft.header_text[:40],
            "IdempotencyKey": token,
            "ToItems": draft.payload.get("items", []),
        }
        created = self.v2.create(
            self._service("purchase_requisition"),
            "PurchaseRequisitionSet",
            body,
            correlation_id=correlation_id,
        )
        pr_id = str(created.get("PurchaseRequisition", "") or "")
        if not pr_id:
            raise SAPError(
                "PR olusturuldu ama belge numarasi donmedi; mutabakat gerekiyor.",
                code="EBAN_NO_DOCUMENT_NUMBER",
            )

        already = _flag(created.get("AlreadyExisted"))
        if already:
            log.info("SAP PR idempotency cakismasi: %s mevcut (ref %s)", pr_id, token)
            messages = [
                f"Bu idempotency anahtari ile daha once {pr_id} olusturulmus; "
                "yeni belge yaratilmadi."
            ]
        else:
            log.info("SAP PR olusturuldu: %s (ref %s)", pr_id, token)
            messages = [f"Satinalma talebi {pr_id} olusturuldu."]

        return PurchaseRequisitionResult(
            requisition_id=pr_id,
            created=not already,
            dry_run=False,
            requires_human_approval=False,
            total_value=draft.total_value,
            currency=draft.currency,
            items=draft.items,
            messages=messages,
            external_reference=token,
            # ECC'de ETag yok: mutabakat anahtari tasinir.
            etag=f"idempotency:{token}",
        )

    def read_purchase_requisition(self, requisition_id: str) -> dict[str, Any] | None:
        """Read-after-write dogrulamasi icin PR'i geri okur."""
        rows = self._read(
            "purchase_requisition",
            "PurchaseRequisitionSet",
            filter_expr=f"PurchaseRequisition eq '{quote(requisition_id)}'",
            expand=("ToItems",),
            top=1,
        )
        if not rows:
            return None
        row = rows[0]
        items = expanded_rows(row, "ToItems")
        total = sum(_num(i.get("Price")) * _num(i.get("Quantity")) for i in items)
        return {
            "PurchaseRequisition": str(row.get("PurchaseRequisition", requisition_id)),
            "HeaderText": str(row.get("HeaderText") or ""),
            "DocumentType": str(row.get("DocumentType") or ""),
            "IdempotencyKey": str(row.get("IdempotencyKey") or ""),
            "CreatedBy": str(row.get("CreatedBy") or ""),
            "item_count": len(items),
            "total_value": round(total, 2),
            "items": items,
            "source_api": f"{self._service('purchase_requisition')}/PurchaseRequisitionSet",
        }

    def find_purchase_requisition_by_reference(
        self, external_reference: str
    ) -> tuple[str, dict[str, Any]] | None:
        """Timeout sonrasi mutabakat: bu anahtarla PR olusmus mu?

        S/4 adapteri baslik metninde `contains()` ile arar (40 karakter siniri
        yuzunden hash gomer). ECC'de baslik metni STXH/STXL'de yasar ve
        filtrelenemez; bunun yerine ZAGENT_IDEMPOTENCY'de **tam esitlik**
        sorgusu yapilir. Substring taramasindan hem hizli hem saglamdir.
        """
        token = reference_token(external_reference)
        rows = self._read(
            "purchase_requisition",
            IDEMPOTENCY_ENTITY_SET,
            filter_expr=_and(
                f"IdempotencyKey eq '{quote(token)}'",
                f"ObjectType eq '{_IDEMPOTENCY_OBJECT_PR}'",
            ),
            top=1,
        )
        if not rows:
            return None
        pr_id = str(rows[0].get("ObjectId", ""))
        if not pr_id:
            return None
        record = self.read_purchase_requisition(pr_id)
        return (pr_id, record) if record else None

    # --- Satinalma siparisi -------------------------------------------------
    def get_purchase_orders(
        self,
        *,
        material_id: str | None = None,
        vendor_id: str | None = None,
        wbs_element: str | None = None,
        only_open: bool = False,
        limit: int = 50,
    ) -> list[PurchaseOrder]:
        """PO kalemleri + baslik + teslimat plani, tek cagride ($expand)."""
        clauses: list[str] = ["DeletionIndicator eq ''"]
        if material_id:
            clauses.append(f"Material eq '{quote(material_id)}'")
        if wbs_element:
            # WBS hiyerarsiktir ve tool sozlesmesi "eleman VEYA on eki" vaat
            # eder. Sunucuya genis (`startswith`) filtre gonderilir, ata/alt
            # eleman kurali asagida kesinlestirilir: bu yon guvenlidir - genis
            # filtre gecerli satir DUSURMEZ, yalniz fazlasini getirir.
            clauses.append(f"startswith(WBSElement,'{quote(wbs_element)}')")
        if vendor_id:
            # Tedarikci basliktadir; CDS bunu kaleme tasidigi icin filtre
            # sunucuda uygulanir (Python tarafinda ayiklama yapilmaz).
            clauses.append(f"Supplier eq '{quote(vendor_id)}'")

        rows = self._read(
            "purchase_order",
            "PurchaseOrderItemSet",
            filter_expr=_and(*clauses),
            expand=("ToHeader", "ToScheduleLines"),
            top=limit,
        )

        out: list[PurchaseOrder] = []
        for row in rows:
            # Sunucuya gonderilen genis `startswith` filtresinin yanlis
            # pozitiflerini burada eliyoruz: `R-2026-02` sorgusu
            # `R-2026-021-1`i KAPSAMAZ (ayri proje kodu, ata degil).
            if wbs_element and not wbs_matches(wbs_element, str(row.get("WBSElement") or "")):
                continue
            header = _first(expanded_rows(row, "ToHeader"))
            schedule = expanded_rows(row, "ToScheduleLines")
            ordered = _num(row.get("Quantity"))
            delivered = _num(row.get("DeliveredQuantity"))
            invoiced = _num(row.get("InvoicedQuantity"))

            if delivered <= 0:
                status = "open"
            elif delivered < ordered:
                status = "partially_delivered"
            elif invoiced >= ordered > 0:
                status = "invoiced"
            else:
                status = "delivered"

            if only_open and status in {"delivered", "invoiced", "closed"}:
                continue

            requested = _weighted_date(schedule, "DeliveryDate")
            # `or requested` KALDIRILDI (OData yolundaki ayni duzeltme). Teyit
            # yoksa talep tarihini teyit diye yazmak, gecikmeyi her zaman 0
            # gosteriyor ve olmayan bir teyide dayali uyari uretiyordu.
            confirmed = _weighted_date(schedule, "ConfirmedDate")

            out.append(
                PurchaseOrder(
                    po_id=str(row.get("PurchaseOrder", "")),
                    vendor_id=str(header.get("Supplier") or row.get("Supplier") or ""),
                    vendor_name=str(header.get("SupplierName") or ""),
                    created_on=parse_odata_datetime(header.get("CreationDate")),
                    currency=str(
                        row.get("Currency")
                        or header.get("DocumentCurrency")
                        or self.settings.sap.currency
                    ),
                    net_value=_num(row.get("NetValue")) or round(
                        _num(row.get("NetPrice")) * ordered, 2
                    ),
                    status=status,
                    material_id=str(row.get("Material") or ""),
                    description=str(row.get("ItemText") or ""),
                    quantity=ordered,
                    delivered_qty=delivered,
                    confirmed_delivery_date=confirmed,
                    requested_delivery_date=requested,
                    wbs_element=str(row.get("WBSElement") or "") or None,
                )
            )
        return out

    # =======================================================================
    # ProcureToPayPort (salt okunur)
    # =======================================================================
    def get_purchase_order_items(self, po_id: str) -> list[PurchaseOrderItem]:
        """PO kalemleri (EKPO) + kumulatif teslim/fatura miktarlari (EKBE).

        Teslim ve fatura toplamlari CDS icinde EKBE'den toplanir; kalem basina
        ayri gecmis cagrisi yapilmaz.
        """
        rows = self._read(
            "purchase_order",
            "PurchaseOrderItemSet",
            filter_expr=f"PurchaseOrder eq '{quote(po_id)}'",
            top=200,
        )
        return [
            PurchaseOrderItem(
                po_id=str(row.get("PurchaseOrder", po_id)),
                item_no=str(row.get("PurchaseOrderItem", "")),
                material_id=str(row.get("Material") or ""),
                description=str(row.get("ItemText") or ""),
                plant=str(row.get("Plant") or ""),
                quantity=_num(row.get("Quantity")),
                unit=str(row.get("BaseUnit") or "ST"),
                net_price=_num(row.get("NetPrice")),
                net_value=_num(row.get("NetValue")),
                currency=str(row.get("Currency") or self.settings.sap.currency),
                delivered_qty=_num(row.get("DeliveredQuantity")),
                invoiced_qty=_num(row.get("InvoicedQuantity")),
                goods_receipt_required=_flag(row.get("GoodsReceiptIndicator")),
                invoice_receipt_required=_flag(row.get("InvoiceReceiptIndicator")),
                deletion_indicator=_flag(row.get("DeletionIndicator")),
                wbs_element=str(row.get("WBSElement") or "") or None,
                account_assignment=str(row.get("AccountAssignmentCategory") or ""),
            )
            for row in rows
        ]

    def get_schedule_lines(self, po_id: str, *, item_no: str = "") -> list[ScheduleLine]:
        """Teslimat plani satirlari (EKET)."""
        clauses = [f"PurchaseOrder eq '{quote(po_id)}'"]
        if item_no:
            clauses.append(f"PurchaseOrderItem eq '{quote(item_no)}'")
        rows = self._read(
            "purchase_order", "ScheduleLineSet", filter_expr=_and(*clauses), top=200
        )
        return [
            ScheduleLine(
                po_id=str(row.get("PurchaseOrder", po_id)),
                item_no=str(row.get("PurchaseOrderItem", "")),
                schedule_line=str(row.get("ScheduleLine") or "0001"),
                requested_date=parse_odata_datetime(row.get("DeliveryDate")),
                confirmed_date=parse_odata_datetime(row.get("ConfirmedDate")),
                quantity=_num(row.get("ScheduleQuantity")),
                delivered_qty=_num(row.get("DeliveredQuantity")),
                unit=str(row.get("BaseUnit") or "ST"),
            )
            for row in rows
        ]

    def get_goods_receipts(
        self, *, po_id: str = "", material_id: str = "", limit: int = 50
    ) -> list[GoodsReceipt]:
        """Mal kabul belgeleri (MKPF/MSEG).

        Iptal hareketleri (102/122/162) ayri kayit olarak dondurulur ve
        `reversed` ile isaretlenir; netlestirme cagirana birakilir.
        """
        clauses: list[str] = []
        if po_id:
            clauses.append(f"PurchaseOrder eq '{quote(po_id)}'")
        if material_id:
            clauses.append(f"Material eq '{quote(material_id)}'")
        if not clauses:
            raise SAPError(
                "Mal kabul sorgusu icin po_id veya material_id gerekli.",
                code="GR_FILTER_REQUIRED",
            )
        rows = self._read(
            "po_history", "GoodsReceiptSet", filter_expr=_and(*clauses), top=limit
        )
        return [
            GoodsReceipt(
                material_document=str(row.get("MaterialDocument", "")),
                document_year=_int(row.get("MaterialDocumentYear")),
                item_no=str(row.get("MaterialDocumentItem") or "0001"),
                posting_date=parse_odata_datetime(row.get("PostingDate")),
                movement_type=str(row.get("MovementType") or "101"),
                material_id=str(row.get("Material") or ""),
                plant=str(row.get("Plant") or ""),
                quantity=_num(row.get("Quantity")),
                unit=str(row.get("BaseUnit") or "ST"),
                po_id=str(row.get("PurchaseOrder") or ""),
                po_item=str(row.get("PurchaseOrderItem") or ""),
                batch=str(row.get("Batch") or ""),
                reversed=str(row.get("MovementType") or "") in _REVERSAL_MOVEMENTS,
            )
            for row in rows
        ]

    def get_supplier_invoices(
        self,
        *,
        invoice_id: str = "",
        po_id: str = "",
        vendor_id: str = "",
        only_blocked: bool = False,
        limit: int = 50,
    ) -> list[SupplierInvoice]:
        """Tedarikci faturalari (RBKP) + kalem + blokaj nedenleri."""
        clauses: list[str] = []
        if invoice_id:
            clauses.append(f"SupplierInvoice eq '{quote(invoice_id)}'")
        if vendor_id:
            clauses.append(f"Supplier eq '{quote(vendor_id)}'")
        if po_id:
            # PO bagi kalem duzeyindedir (RSEG-EBELN); CDS bunu basliga tasir.
            clauses.append(f"PurchaseOrder eq '{quote(po_id)}'")
        if only_blocked:
            clauses.append("PaymentBlock ne ''")

        rows = self._read(
            "supplier_invoice",
            "SupplierInvoiceSet",
            filter_expr=_and(*clauses),
            expand=("ToItems", "ToBlocks"),
            top=limit,
        )
        service = f"{self._service('supplier_invoice')}/SupplierInvoiceSet"

        out: list[SupplierInvoice] = []
        for row in rows:
            fiscal_year = _int(row.get("FiscalYear"))
            invoice_no = str(row.get("SupplierInvoice", ""))
            items = expanded_rows(row, "ToItems")
            po_ids = sorted(
                {str(i.get("PurchaseOrder", "")) for i in items if i.get("PurchaseOrder")}
            )
            blocks = [
                self._map_invoice_block(b, invoice_no)
                for b in expanded_rows(row, "ToBlocks")
            ]
            payment_block = str(row.get("PaymentBlock") or "").strip()
            status = _invoice_status(row, payment_block)

            out.append(
                SupplierInvoice(
                    invoice_id=invoice_no,
                    fiscal_year=fiscal_year,
                    vendor_id=str(row.get("Supplier") or ""),
                    vendor_name=str(row.get("SupplierName") or ""),
                    company_code=str(row.get("CompanyCode") or ""),
                    invoice_date=parse_odata_datetime(row.get("DocumentDate")),
                    posting_date=parse_odata_datetime(row.get("PostingDate")),
                    due_date=parse_odata_datetime(row.get("DueDate")),
                    gross_amount=_num(row.get("GrossAmount")),
                    net_amount=_num(row.get("NetAmount")),
                    tax_amount=_num(row.get("TaxAmount")),
                    currency=str(row.get("Currency") or self.settings.sap.currency),
                    status=status,
                    payment_block=payment_block,
                    payment_terms=str(row.get("PaymentTerms") or ""),
                    paid_on=parse_odata_datetime(row.get("ClearingDate")),
                    accounting_document=str(row.get("AccountingDocument") or ""),
                    po_ids=po_ids,
                    blocks=blocks,
                    source_api=service,
                )
            )
        return out

    def _map_invoice_block(self, row: dict[str, Any], invoice_id: str) -> InvoiceBlock:
        expected = _opt_num(row.get("ExpectedValue"))
        actual = _opt_num(row.get("ActualValue"))
        variance_abs = None
        variance_pct = None
        if expected is not None and actual is not None:
            variance_abs = round(actual - expected, 3)
            if expected:
                variance_pct = round((actual / expected - 1) * 100, 2)
        # Bilinmeyen/eksik neden "price" ya da "manual" sayilmaz: ikisi de
        # kullaniciyi belirli bir islemin ustune yollayan bir IDDIADIR.
        # Okunamayan neden `unknown` kalir.
        reason = str(row.get("BlockReason") or "").lower()
        if reason not in {
            "price", "quantity", "date", "order_price_unit", "quality", "manual", "amount"
        }:
            reason = "unknown"
        return InvoiceBlock(
            invoice_id=invoice_id,
            item_no=str(row.get("InvoiceItem") or ""),
            block_reason=reason,  # type: ignore[arg-type]
            tolerance_key=str(row.get("ToleranceKey") or ""),
            expected_value=expected,
            actual_value=actual,
            variance_abs=variance_abs,
            variance_pct=variance_pct,
            tolerance_limit_abs=_opt_num(row.get("ToleranceLimitAbsolute")),
            tolerance_limit_pct=_opt_num(row.get("ToleranceLimitPercent")),
            currency=str(row.get("Currency") or ""),
            po_id=str(row.get("PurchaseOrder") or ""),
            po_item=str(row.get("PurchaseOrderItem") or ""),
        )


    # --- Belge akisi --------------------------------------------------------
    def get_document_flow(
        self,
        document_id: str,
        *,
        document_type: str = "auto",
        include_payments: bool = True,
    ) -> list[DocumentFlowNode]:
        """PR -> PO -> mal kabul -> fatura -> odeme zinciri.

        ECC'nin avantaji burada gorunur: EKBE (satinalma siparisi gecmisi) mal
        kabul ve fatura girisini kalem bazinda zaten tasir. Zincir tek bir
        gecmis okumasindan kurulur.

        **Hicbir bag tahminle kurulmaz.** Her dugum `linked_by` alaninda
        kendisini onceki belgeye baglayan gercek SAP alanini tasir. Bag
        gosterilemiyorsa dugum zincire eklenmez.
        """
        resolved_type = self._detect_document_type(document_id, document_type)
        if resolved_type == "unknown":
            raise SAPError(
                f"Belge {document_id} PR/PO/mal kabul/fatura olarak bulunamadi. "
                "document_type parametresiyle tur belirtin.",
                code="DOCFLOW_UNKNOWN_DOCUMENT",
            )

        links = self._purchase_orders_for(resolved_type, document_id)
        if not links:
            raise SAPError(
                f"{document_id} icin satinalma siparisi bagi bulunamadi; belge akisi "
                "kurulamaz (tahmini bag uretilmez).",
                code="DOCFLOW_NO_PO_LINK",
            )

        nodes: list[DocumentFlowNode] = []
        for po_id, linked_by, predecessor in links:
            nodes.extend(
                self._flow_for_po(
                    po_id,
                    entry_link=linked_by,
                    entry_predecessor=predecessor,
                    include_payments=include_payments,
                )
            )
        return nodes

    def _detect_document_type(self, document_id: str, document_type: str) -> str:
        """Belge turunu belirler.

        ECC'de numara araliklari musteriye gore ozellestirilir; bu yuzden
        numaradan tur **tahmin edilmez**. `auto` verildiginde her tur icin
        ucuz bir varlik sorgusu ($top=1, tek alan) yapilir.
        """
        known = {
            "purchase_requisition",
            "purchase_order",
            "goods_receipt",
            "supplier_invoice",
        }
        if document_type in known:
            return document_type
        if document_type not in {"auto", ""}:
            raise SAPError(
                f"Gecersiz document_type '{document_type}'. "
                f"Gecerli: auto, {', '.join(sorted(known))}.",
                code="DOCFLOW_BAD_TYPE",
            )

        probes: tuple[tuple[str, str, str, str], ...] = (
            ("purchase_order", "purchase_order", "PurchaseOrderItemSet", "PurchaseOrder"),
            (
                "purchase_requisition",
                "purchase_order",
                "PurchaseOrderItemSet",
                "PurchaseRequisition",
            ),
            ("supplier_invoice", "supplier_invoice", "SupplierInvoiceSet", "SupplierInvoice"),
            ("goods_receipt", "po_history", "GoodsReceiptSet", "MaterialDocument"),
        )
        for doc_type, alias, entity_set, field in probes:
            try:
                rows = self._read(
                    alias,
                    entity_set,
                    filter_expr=f"{field} eq '{quote(document_id)}'",
                    select=(field,),
                    top=1,
                )
            except SAPError as exc:
                log.info("Belge turu sondaji basarisiz (%s): %s", doc_type, exc)
                continue
            if rows:
                return doc_type
        return "unknown"

    def _purchase_orders_for(
        self, document_type: str, document_id: str
    ) -> list[tuple[str, str, str]]:
        """(po_id, linked_by, predecessor_id) uclulerini dondurur."""
        if document_type == "purchase_order":
            return [(document_id, "", "")]

        if document_type == "purchase_requisition":
            rows = self._read(
                "purchase_order",
                "PurchaseOrderItemSet",
                filter_expr=f"PurchaseRequisition eq '{quote(document_id)}'",
                select=("PurchaseOrder", "PurchaseRequisition"),
                top=100,
            )
            return [
                (str(r.get("PurchaseOrder", "")), "EKPO-BANFN", document_id)
                for r in rows
                if r.get("PurchaseOrder")
            ]

        if document_type == "goods_receipt":
            rows = self._read(
                "po_history",
                "GoodsReceiptSet",
                filter_expr=f"MaterialDocument eq '{quote(document_id)}'",
                select=("PurchaseOrder", "MaterialDocument"),
                top=100,
            )
            return [
                (str(r.get("PurchaseOrder", "")), "MSEG-EBELN", document_id)
                for r in rows
                if r.get("PurchaseOrder")
            ]

        if document_type == "supplier_invoice":
            rows = self._read(
                "supplier_invoice",
                "SupplierInvoiceItemSet",
                filter_expr=f"SupplierInvoice eq '{quote(document_id)}'",
                select=("PurchaseOrder", "SupplierInvoice"),
                top=100,
            )
            seen: dict[str, None] = {}
            for r in rows:
                po = str(r.get("PurchaseOrder", ""))
                if po:
                    seen.setdefault(po, None)
            return [(po, "RSEG-EBELN", document_id) for po in seen]

        return []

    def _flow_for_po(
        self,
        po_id: str,
        *,
        entry_link: str,
        entry_predecessor: str,
        include_payments: bool,
    ) -> list[DocumentFlowNode]:
        """Tek bir PO icin zinciri kurar: PR -> PO -> GR -> fatura -> odeme."""
        service = self._service("purchase_order")
        history_service = self._service("po_history")
        nodes: list[DocumentFlowNode] = []

        items = self._read(
            "purchase_order",
            "PurchaseOrderItemSet",
            filter_expr=f"PurchaseOrder eq '{quote(po_id)}'",
            expand=("ToHeader",),
            top=200,
        )
        if not items:
            return nodes

        header = _first(expanded_rows(items[0], "ToHeader"))
        currency = str(
            items[0].get("Currency") or header.get("DocumentCurrency") or self.settings.sap.currency
        )

        # 1) PR dugumu - yalniz EKPO-BANFN doluysa.
        requisitions = {
            str(i.get("PurchaseRequisition", "")) for i in items if i.get("PurchaseRequisition")
        }
        for pr_id in sorted(requisitions):
            nodes.append(
                DocumentFlowNode(
                    document_type="purchase_requisition",
                    document_id=pr_id,
                    status="converted",
                    linked_by="EKPO-BANFN",
                    predecessor_id="",
                    source_api=f"{service}/PurchaseOrderItemSet",
                    notes=["Bag EKPO-BANFN alanindan kuruldu."],
                )
            )

        # 2) PO dugumu.
        total_value = sum(_num(i.get("NetValue")) for i in items)
        delivered = sum(_num(i.get("DeliveredQuantity")) for i in items)
        ordered = sum(_num(i.get("Quantity")) for i in items)
        nodes.append(
            DocumentFlowNode(
                document_type="purchase_order",
                document_id=po_id,
                document_date=parse_odata_datetime(header.get("CreationDate")),
                status=(
                    "open" if delivered <= 0
                    else ("partially_delivered" if delivered < ordered else "delivered")
                ),
                quantity=round(ordered, 3),
                amount=round(total_value, 2),
                currency=currency,
                linked_by=entry_link or (
                    "EKPO-BANFN" if requisitions else ""
                ),
                predecessor_id=entry_predecessor or (sorted(requisitions)[0] if requisitions else ""),
                source_api=f"{service}/PurchaseOrderSet",
            )
        )

        # 3) EKBE gecmisi: mal kabul (BEWTP=E) ve fatura girisi (BEWTP=Q).
        history = self._read(
            "po_history",
            "PurchaseOrderHistorySet",
            filter_expr=f"PurchaseOrder eq '{quote(po_id)}'",
            top=300,
        )
        invoice_keys: dict[str, None] = {}
        for row in history:
            category = str(row.get("HistoryCategory") or "").upper()
            document = str(row.get("MaterialDocument") or "")
            if not document:
                continue
            posted = parse_odata_datetime(row.get("PostingDate"))
            movement = str(row.get("MovementType") or "")
            quantity = _num(row.get("Quantity"))

            if category == _HIST_GOODS_RECEIPT:
                nodes.append(
                    DocumentFlowNode(
                        document_type="goods_receipt",
                        document_id=document,
                        item_no=str(row.get("MaterialDocumentItem") or ""),
                        document_date=posted,
                        status="reversed" if movement in _REVERSAL_MOVEMENTS else "posted",
                        quantity=round(quantity, 3),
                        amount=_opt_num(row.get("Amount")),
                        currency=str(row.get("Currency") or currency),
                        linked_by="EKBE-BELNR (BEWTP=E)",
                        predecessor_id=po_id,
                        source_api=f"{history_service}/PurchaseOrderHistorySet",
                        notes=[f"Hareket tipi {movement}."] if movement else [],
                    )
                )
            elif category == _HIST_INVOICE_RECEIPT:
                invoice_keys.setdefault(document, None)
                nodes.append(
                    DocumentFlowNode(
                        document_type="supplier_invoice",
                        document_id=document,
                        item_no=str(row.get("MaterialDocumentItem") or ""),
                        document_date=posted,
                        status="posted",
                        quantity=round(quantity, 3),
                        amount=_opt_num(row.get("Amount")),
                        currency=str(row.get("Currency") or currency),
                        linked_by="EKBE-BELNR (BEWTP=Q)",
                        predecessor_id=po_id,
                        source_api=f"{history_service}/PurchaseOrderHistorySet",
                    )
                )

        # 4) Odeme dugumleri - yalniz mahsup tarihi (BSEG-AUGDT) varsa.
        if include_payments and invoice_keys:
            nodes.extend(self._payment_nodes(list(invoice_keys), currency))

        return nodes

    def _payment_nodes(self, invoice_ids: list[str], currency: str) -> list[DocumentFlowNode]:
        """Odeme dugumleri. Mahsup edilmemis fatura icin dugum URETILMEZ."""
        try:
            rows = self._read(
                "supplier_invoice",
                "SupplierInvoiceSet",
                filter_expr=_or_filter("SupplierInvoice", invoice_ids),
                select=(
                    "SupplierInvoice",
                    "ClearingDate",
                    "GrossAmount",
                    "Currency",
                    "AccountingDocument",
                ),
                top=100,
            )
        except SAPError as exc:
            log.info("Odeme bilgisi okunamadi: %s", exc)
            return []

        out: list[DocumentFlowNode] = []
        for row in rows:
            cleared = parse_odata_datetime(row.get("ClearingDate"))
            if cleared is None:
                continue
            invoice_id = str(row.get("SupplierInvoice", ""))
            out.append(
                DocumentFlowNode(
                    document_type="payment",
                    document_id=str(row.get("AccountingDocument") or invoice_id),
                    document_date=cleared,
                    status="paid",
                    amount=_opt_num(row.get("GrossAmount")),
                    currency=str(row.get("Currency") or currency),
                    linked_by="BSEG-AUGBL (mahsup belgesi)",
                    predecessor_id=invoice_id,
                    source_api=f"{self._service('supplier_invoice')}/SupplierInvoiceSet",
                )
            )
        return out

# ---------------------------------------------------------------------------
# Modul duzeyi yardimcilar
# ---------------------------------------------------------------------------
def _first(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return rows[0] if rows else {}


def _weighted_date(schedule: list[dict[str, Any]], field: str) -> date | None:
    """Coklu teslimat plani satiri icin miktar-agirlikli tarih.

    Tek satirdan tarih almak coklu teslimatta yaniltir; miktar agirligi
    terminin fiilen ne zaman tamamlandigini yansitir.
    """
    weighted: list[tuple[date, float]] = []
    for line in schedule:
        when = parse_odata_datetime(line.get(field))
        if when is None:
            continue
        weighted.append((when, max(_num(line.get("ScheduleQuantity")), 0.0)))
    if not weighted:
        return None
    total_qty = sum(q for _, q in weighted)
    if total_qty <= 0:
        return max(when for when, _ in weighted)
    ordinal = sum(when.toordinal() * q for when, q in weighted) / total_qty
    return date.fromordinal(round(ordinal))


def _parse_datetime(value: Any) -> datetime | None:
    """OData V2 /Date(...)/ veya ISO damgasini timezone'lu datetime'a cevirir."""
    if not value:
        return None
    text = str(value)
    if text.startswith("/Date("):
        digits = text[6:-2].split("+")[0].split("-")[0]
        try:
            return datetime.fromtimestamp(int(digits) / 1000, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _invoice_status(row: dict[str, Any], payment_block: str) -> str:
    """RBKP-RBSTAT + odeme blokaji + mahsup durumundan statu turetir."""
    raw = str(row.get("InvoiceStatus") or "").strip().upper()
    if raw in {"CANCELLED", "3"}:
        return "cancelled"
    if raw in {"PARKED", "A", "B"}:
        return "parked"
    if payment_block:
        return "blocked"
    if row.get("ClearingDate"):
        return "paid"
    return "posted"


def _workflow_status(raw: str) -> str:
    """SAP_WAPI is ogesi statusunu model enum'una esler."""
    mapping = {
        "COMPLETED": "completed",
        "CONFIRMED": "completed",
        "READY": "ready",
        "SELECTED": "ready",
        "STARTED": "in_progress",
        "COMMITTED": "in_progress",
        "WAITING": "waiting",
        "CANCELLED": "cancelled",
        "ERROR": "rejected",
    }
    return mapping.get(raw.strip().upper(), "in_progress")
