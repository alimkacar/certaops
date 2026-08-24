"""Gercek SAP S/4HANA backend'i (OData V4 tercihli, V2 fallback).

Backend su veri dogrulugu invariantlarini uygular:

  A. Malzeme aramasi artik **aciklamada** da arar ve siniflandirma
     karakteristiklerini gercekten okur (API_CLFN_PRODUCT_SRV).
  B. `check_atp` gercek ATP servisini kullanir; stok fotografi ATP yerine
     gecirilmez. MRP arz/talep ayri bir port metodudur.
  C. PO okumasi V4 uzerinden `$expand` ile yapilir: baslik basina ek GET yok
     (N+1 kalkti), schedule line duzeyinde talep/teyit tarihi miktar-agirlikli
     degerlendirilir.
  D. Tedarikci skorlari operasyonel degerlendirme CDS'inden okunur; okunamazsa
     alanlar `estimated_fields` ile isaretlenir, sessizce uydurulmaz.

Servis yollari ve beklenen alanlar `adapters.sap.capabilities` icindeki manifestte
tanimlidir. Hedef sistemde kontrat farkliysa `sap_discover_capabilities` bunu
raporlar; kod sessiz bos veri dondurmez.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..adapters.sap import (
    CAPABILITY_MANIFEST,
    ODataHttpCore,
    ODataV2Client,
    ODataV4Client,
    SAPError,
    SAPNotSupported,
    account_assignment_shape,
    breaker_for,
    build_http_client,
    escape_key,
    expanded_rows,
    parse_metadata,
    parse_odata_datetime,
    quote,
    resolve_connection,
    to_odata_datetime,
    verify_contract,
    verify_write_shape,
)
from ..core.tenant_profile import DEFAULT_DOCUMENT_TYPE
from .base import SAPBackend, effective_unit_price
from .models import (
    AtpResult,
    AtpScheduleLine,
    DocumentFlowNode,
    GoodsReceipt,
    InfoRecord,
    InvoiceBlock,
    Material,
    MaterialClassification,
    ProjectCost,
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
# PR basligina yazilan idempotency referansi. Baslik 40 karakter sinirli oldugu
# icin anahtarin sha256'sinin ilk 16 hanesi kullanilir.
_REFERENCE_PREFIX = "REF#"

# --- Alan minimizasyonu ------------------------------------------------------
# `$select` bir performans ayari degil, **veri minimizasyonu kontrolu**dur.
# `$select` verilmezse SAP entity'nin TUM alanlarini doner: `A_Supplier`
# cagrisi vergi numarasi, banka hesabi ve adres bilgisini de tasir. Bu veri
# hicbir tool tarafindan istenmemis olsa bile once surece, sonra DLP'ye,
# oradan da loglara ve cache'e girme riski tasir. En ucuz koruma, hic
# okumamaktir.
#
# Kural: her okuma yalniz kendi is mantiginin kullandigi alanlari ister.
# Yeni bir alan gerektiginde buraya acikca eklenir.
SELECT_FIELDS: dict[str, str] = {
    "product": (
        "Product,ProductType,ProductGroup,BaseUnit,GrossWeight,"
        "to_Description/Product,to_Description/Language,to_Description/ProductDescription,"
        "to_Plant/Product,to_Plant/Plant,to_Plant/ProcurementType,"
        "to_Plant/MinimumLotSizeQuantity,to_Plant/MRPResponsible,to_Plant/ABCIndicator,"
        "to_Plant/to_PlantMRPArea/Product,to_Plant/to_PlantMRPArea/Plant,"
        "to_Plant/to_PlantMRPArea/MRPArea,"
        "to_Plant/to_PlantMRPArea/MRPResponsible,"
        "to_Plant/to_PlantMRPArea/PlannedDeliveryDurationInDays,"
        "to_Plant/to_PlantMRPArea/IsPlannedDeliveryTime"
    ),
    "valuation": (
        "Product,ValuationArea,MovingAveragePrice,StandardPrice,Currency,PriceUnitQty"
    ),
    "stock": (
        "Material,Plant,StorageLocation,InventoryStockType,MatlWrhsStkQtyInMatlBaseUnit"
    ),
    "inforecord": (
        "PurchasingInfoRecord,Material,Supplier,IsDeleted,"
        "to_PurgInfoRecdOrgPlantData/PurchasingOrganization,"
        "to_PurgInfoRecdOrgPlantData/NetPriceAmount,"
        "to_PurgInfoRecdOrgPlantData/Currency,"
        "to_PurgInfoRecdOrgPlantData/MaterialPriceUnitQty,"
        "to_PurgInfoRecdOrgPlantData/MinimumPurchaseOrderQuantity,"
        "to_PurgInfoRecdOrgPlantData/MaterialPlannedDeliveryDurn,"
        "to_PurgInfoRecdOrgPlantData/IncotermsClassification,"
        # API Business Hub metadata'sinda gorunmesine ragmen PaymentTerms'i
        # $select'e eklemek sandbox'ta sonucu sessizce bosaltiyor. Satin alma
        # karsilastirmasi bu alana bagli degil; model asagida NT30 varsayimini
        # acikca kullaniyor.
        "to_PurgInfoRecdOrgPlantData/PriceValidityEndDate"
    ),
    # A_Supplier'da vergi/banka/adres alanlari BILEREK yok: satinalma karari
    # icin gerekli degiller ve D2/D3 sinifindalar.
    "supplier": "Supplier,SupplierName,SupplierFullName,PurchasingIsBlocked,"
    "SupplierProcurementBlock",
    "supplier_address": "BusinessPartner,Country,CityName",
    "supplier_score": (
        "Supplier,PurchasingOrganization,SupplierOperationalScore,PriceVarianceScore,"
        "TimeVarianceScore,QuantityVarianceScore,InspectionLotQualityScore,"
        "QualityNotificationScore"
    ),
    "classification": "Product,CharcInternalID,CharcValue,CharcFromNumericValue,"
    "CharcFromNumericValueUnit,CharcToNumericValueUnit,ClassType",
    "po_item_p2p": (
        "PurchaseOrder,PurchaseOrderItem,PurchaseOrderItemText,Material,Plant,"
        "OrderQuantity,PurchaseOrderQuantityUnit,DocumentCurrency,NetPriceAmount,"
        "NetPriceQuantity,GoodsReceiptIsExpected,InvoiceIsExpected,"
        "PurchasingDocumentDeletionCode,AccountAssignmentCategory,"
        "PurchaseRequisition,PurchaseRequisitionItem,"
        "to_AccountAssignment/PurchaseOrder,to_AccountAssignment/PurchaseOrderItem,"
        "to_AccountAssignment/WBSElementExternalID"
    ),
    "po_schedule_p2p": (
        "PurchasingDocument,PurchasingDocumentItem,ScheduleLine,"
        "ScheduleLineDeliveryDate,PurchaseOrderQuantityUnit,"
        "ScheduleLineOrderQuantity,ScheduleLineCommittedQuantity,"
        "SchedLineStscDeliveryDate"
    ),
    "material_document_p2p": (
        "MaterialDocumentYear,MaterialDocument,MaterialDocumentItem,Material,Plant,"
        "Batch,GoodsMovementType,PurchaseOrder,PurchaseOrderItem,QuantityInEntryUnit,"
        "EntryUnit,GoodsMovementIsCancelled,ReversedMaterialDocument,"
        "to_MaterialDocumentHeader/MaterialDocumentYear,"
        "to_MaterialDocumentHeader/MaterialDocument,"
        "to_MaterialDocumentHeader/PostingDate"
    ),
    "supplier_invoice_p2p": (
        "SupplierInvoice,FiscalYear,CompanyCode,DocumentDate,PostingDate,"
        "InvoicingParty,DocumentCurrency,InvoiceGrossAmount,PaymentTerms,"
        "DueCalculationBaseDate,NetPaymentDays,PaymentBlockingReason,"
        "SupplierInvoiceStatus,SupplierInvoicePaymentStatus,"
        "SupplierInvoiceApprovalStatus,ReverseDocument,IsReversal,IsReversed,"
        "to_SuplrInvcItemPurOrdRef/SupplierInvoice,"
        "to_SuplrInvcItemPurOrdRef/FiscalYear,"
        "to_SuplrInvcItemPurOrdRef/SupplierInvoiceItem,"
        "to_SuplrInvcItemPurOrdRef/PurchaseOrder,"
        "to_SuplrInvcItemPurOrdRef/PurchaseOrderItem,"
        "to_SuplrInvcItemPurOrdRef/QuantityInPurchaseOrderUnit,"
        "to_SuplrInvcItemPurOrdRef/SupplierInvoiceItemAmount,"
        "to_SupplierInvoiceTax/SupplierInvoice,"
        "to_SupplierInvoiceTax/FiscalYear,to_SupplierInvoiceTax/TaxAmount"
    ),
}

# SAP hesap atamasi kategorileri (EBAN-KNTTP).
ACCT_ASSIGN_PROJECT = "P"
ACCT_ASSIGN_COST_CENTER = "K"
#: Hesap atamasi olmayan (stoga giren) kalem.
ACCT_ASSIGN_STOCK = ""

#: Released V4 servisi bulunmayan sistemlerde kullanilacak V2 karsiligi.
#: Manifestte zaten tanimliydi ama hicbir kod bu esleme uzerinden gitmiyordu.
_V2_FALLBACK: dict[str, str] = {
    "purchase_requisition": "purchase_requisition_v2",
    "purchase_order": "purchase_order_v2",
}


def service_path(alias: str) -> str:
    capability = CAPABILITY_MANIFEST.get(alias)
    if capability is None:
        raise SAPError(f"Bilinmeyen servis alias'i: {alias}", code="UNKNOWN_SERVICE")
    return capability.service_path


def reference_token(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
    return f"{_REFERENCE_PREFIX}{digest}"


def _decimal(value: Any, places: int = 3) -> float:
    """OData V4 `Edm.Decimal` icin JSON **sayisi** uretir.

    Onceki surum miktar ve fiyati string olarak gonderiyordu (`"3.0"`).
    OData V4 JSON'da `Edm.Decimal` varsayilan olarak sayidir; string gosterim
    yalniz `Content-Type` icinde `IEEE754Compatible=true` varken gecerlidir.
    Bazi RAP servisleri stringi tolere eder, bazilari 400 doner - tolere
    edilmesine bel baglanmaz.
    """
    try:
        return round(float(value or 0), places)
    except (TypeError, ValueError):
        return 0.0


def _number_literal(value: Any) -> str:
    """`$filter` icine gomulecek sayisal literal.

    Sayisal alanlar tirnak icinde tasinmadigi icin `quote()` onlari korumaz:
    model `quantity="1 or 1 eq 1"` gonderirse ifade filtreyi degistirir.
    Tip zorlama bu yolu tumden kapatir - sayiya cevrilemeyen deger reddedilir.
    """
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SAPError(
            f"Sayisal filtre degeri gecersiz: {value!r}", code="INVALID_FILTER_VALUE"
        ) from exc
    return str(int(number)) if number.is_integer() else repr(number)


def _substring_filter(field_name: str, tokens: Sequence[str]) -> str:
    """Harf durumuna dayanikli `substringof` filtresi.

    SAP Gateway'de `substringof` CHAR alanlarda buyuk/kucuk harfe duyarlidir.
    Token'in kendisi ve buyuk harfli hali OR'lanarak desteklenmeyebilen
    `toupper(...)` fonksiyonuna gerek kalmaz.
    """
    clauses: list[str] = []
    for token in tokens:
        variants = list(dict.fromkeys(v for v in (token, token.upper()) if v))
        clauses.extend(f"substringof('{quote(v)}',{field_name})" for v in variants)
    return " or ".join(clauses)


def _in_filter(field_name: str, values: Sequence[str]) -> str:
    """`Field eq 'a' or Field eq 'b'` - V2/V4 ortak, `in` operatorune gerek yok.

    OData V2 `in` operatorunu desteklemez; ECC ve eski S/4 servislerinde de
    calisan tek bicim budur.
    """
    unique = [v for v in dict.fromkeys(values) if v]
    if not unique:
        return ""
    clause = " or ".join(f"{field_name} eq '{quote(v)}'" for v in unique)
    return f"({clause})" if len(unique) > 1 else clause


class ODataSAPBackend(SAPBackend):
    """S/4HANA OData istemcisi (V4 tercihli)."""

    name = "odata"

    def __init__(self, settings) -> None:
        self.settings = settings
        cfg = settings.sap
        problems = cfg.validate()
        if problems:
            raise SAPError("SAP konfigurasyonu eksik: " + "; ".join(problems), code="CONFIG")

        #: Su anki cagriyi tetikleyen insanin kimligi. `execute_tool` her
        #: tool oncesi doldurur, sonrasinda temizler. SAP tarafindaki izleri
        #: insana baglamak icindir; yetkilendirme degildir.
        self._acting_subject = ""
        #: Aktif tenant profili. `execute_tool` her tool oncesi doldurur.
        self._profile: Any = None
        self.connection = resolve_connection(cfg)
        for warning in self.connection.warnings:
            log.warning("SAP baglanti uyarisi: %s", warning)

        http_client = build_http_client(self.connection, cfg)
        allowed = settings.security.allowed_sap_hosts
        # Tek SAP sistemi = tek devre kesici. V2 ve V4 cekirdekleri ayni ornegi
        # paylasir; ayri olsalardi ardisik hata esigi fiilen iki katina cikardi.
        self.breaker = breaker_for(cfg)
        self._core_v4 = ODataHttpCore(
            client=http_client,
            odata_version="v4",
            sap_client=cfg.client,
            accept_language=cfg.description_language,
            allowed_hosts=allowed,
            token_provider=self.connection.token_provider,
            breaker=self.breaker,
            identity_provider=lambda: self._acting_subject,
        )
        # V2 ve V4 ayni HTTP baglantisini paylasir; yalniz $format/`d` farki degisir.
        self._core_v2 = ODataHttpCore(
            client=http_client,
            odata_version="v2",
            sap_client=cfg.client,
            accept_language=cfg.description_language,
            allowed_hosts=allowed,
            token_provider=self.connection.token_provider,
            breaker=self.breaker,
            identity_provider=lambda: self._acting_subject,
        )
        self.v4 = ODataV4Client(self._core_v4, page_size=cfg.page_size, max_pages=cfg.max_pages)
        self.v2 = ODataV2Client(self._core_v2, page_size=cfg.page_size, max_pages=cfg.max_pages)
        self._metadata_cache: dict[str, Any] = {}
        self._alias_cache: dict[str, str] = {}

        # Kimlik suresi dolarsa (destination/connectivity token'lari sureli)
        # cekirdek istemciyi kendi yeniler; 401/407 kalici hata sayilmaz.
        def _reconnect() -> Any:
            previous = self._core_v4.client
            self.connection = resolve_connection(cfg)
            fresh = build_http_client(self.connection, cfg)
            # V2 ve V4 cekirdekleri ayni istemciyi paylasir: ikisi de tasinir,
            # eski istemci burada (paylasimi bilen tek yerde) kapatilir.
            for core in (self._core_v4, self._core_v2):
                core.client = fresh
                core.token_provider = self.connection.token_provider
                core._csrf_token = ""
            if previous is not fresh:
                # Kapatma hatasi akisi kesmemeli: yeni istemci zaten hazir.
                with contextlib.suppress(Exception):
                    previous.close()
            return fresh

        self._core_v4.reconnect = _reconnect
        self._core_v2.reconnect = _reconnect

    def close(self) -> None:
        self._core_v4.close()

    # --- Yetenek kesfi ------------------------------------------------------
    def metadata_contract(self, alias: str, *, correlation_id: str = ""):
        """Servisin $metadata sozlesmesini onbellekli okur."""
        if alias in self._metadata_cache:
            return self._metadata_cache[alias]
        capability = CAPABILITY_MANIFEST[alias]
        client = self.v4 if capability.odata_version == "v4" else self.v2
        raw = client.metadata(capability.service_path, correlation_id=correlation_id)
        contract = parse_metadata(raw)
        self._metadata_cache[alias] = contract
        return contract

    # --- Servis surumu secimi (gercek V4 -> V2 fallback) --------------------
    def _alias_for(self, primary: str) -> str:
        """Bu sistemde hangi servis surumu kullanilacak?

        `SAP_ODATA_VERSION` uzun sure yalniz dogrulanip **hic kullanilmiyordu**:
        `.env` "V4 varsa V4, yoksa V2'ye duser" diyordu ama kodda fallback yoktu
        ve `purchase_requisition_v2` / `purchase_order_v2` manifest kayitlari
        olu koddu. Hedef sistemde released V4 servisi yoksa (2021 oncesi
        on-prem S/4HANA'da yok) her PR/PO cagrisi 404 donerdi.

        Karar bir kez verilir ve onbelleklenir; `$metadata` sondasi tur basina
        tekrarlanmaz.
        """
        fallback = _V2_FALLBACK.get(primary)
        cached = self._alias_cache.get(primary)
        if cached is not None:
            return cached

        preference = self.settings.sap.odata_version
        resolved = primary
        if fallback is None:
            pass
        elif preference == "v2":
            resolved = fallback
        elif preference == "v4":
            resolved = primary
        else:
            # auto: V2'ye DUSMEK icin pozitif kanit gerekir. Deprecated bir
            # servise sessizce gecmek, dogru calisan bir V4 kurulumunu geri
            # gotururdu; bu yuzden "sozlesme okunamadi" fallback sebebi
            # SAYILMAZ, yalniz uyari uretir.
            try:
                contract = self.metadata_contract(primary)
            except SAPError as exc:
                # Servis SICF'te aktif degil / yetki yok: V4 yolu kapali.
                log.info(
                    "%s V4 servisi okunamadi (%s); V2 fallback deneniyor: %s",
                    primary,
                    exc.code,
                    CAPABILITY_MANIFEST[fallback].service_path,
                )
                resolved = fallback
            else:
                expected = CAPABILITY_MANIFEST[primary].entity_sets
                if not contract.entity_sets:
                    log.warning(
                        "%s icin $metadata cozumlenemedi; V4 tercihi korunuyor. "
                        "Hedef sistemi sap_discover_capabilities ile dogrulayin.",
                        primary,
                    )
                    resolved = primary
                elif all(contract.has_set(s) for s in expected):
                    resolved = primary
                else:
                    resolved = fallback
        if resolved != primary:
            log.warning(
                "%s icin V2 fallback kullaniliyor (%s). Bu servis deprecated; "
                "hedef sistemde released V4 servisini aktive etmek onerilir.",
                primary,
                CAPABILITY_MANIFEST[resolved].service_path,
            )
        self._alias_cache[primary] = resolved
        return resolved

    def _alias_path(self, primary: str) -> str:
        return CAPABILITY_MANIFEST[self._alias_for(primary)].service_path

    def _alias_version(self, primary: str) -> str:
        return CAPABILITY_MANIFEST[self._alias_for(primary)].odata_version

    def resolved_services(self) -> dict[str, dict[str, str]]:
        """Hangi is icin hangi servis surumunun secildigi (teshis ciktisi)."""
        out: dict[str, dict[str, str]] = {}
        for primary in _V2_FALLBACK:
            alias = self._alias_for(primary)
            capability = CAPABILITY_MANIFEST[alias]
            out[primary] = {
                "alias": alias,
                "service": capability.service_path,
                "odata": capability.odata_version,
                "status": capability.status,
            }
        return out

    @property
    def sap_call_count(self) -> int:
        return self._core_v4.call_count + self._core_v2.call_count

    def set_acting_subject(self, subject: str) -> None:
        self._acting_subject = str(subject or "")

    def set_active_profile(self, profile: Any) -> None:
        self._profile = profile

    @property
    def document_type(self) -> str:
        """Satinalma talebi belge tipi.

        Sirkete gore degisir (NB / ZNB / Z01). Profil yoksa SAP standardi.
        """
        return getattr(self._profile, "document_type", None) or DEFAULT_DOCUMENT_TYPE

    def probe_capabilities(self, aliases: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Manifestteki servisleri hedef sistemde dogrular."""
        keys = list(aliases) if aliases else list(CAPABILITY_MANIFEST)
        out: list[dict[str, Any]] = []
        for alias in keys:
            capability = CAPABILITY_MANIFEST.get(alias)
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
                        "error": str(exc),
                    }
                )
                continue
            payload = check.to_dict()
            payload["latency_ms"] = round(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000, 1
            )
            out.append(payload)
        return out

    def capabilities(self) -> dict[str, Any]:
        payload = super().capabilities()
        payload["connection"] = self.connection.describe()
        payload["odata_preference"] = self.settings.sap.odata_version
        return payload

    # --- Malzeme ------------------------------------------------------------
    def search_materials(
        self,
        query: str = "",
        *,
        material_group: str | None = None,
        plant: str | None = None,
        attribute_filters: dict[str, tuple[float, float]] | None = None,
        limit: int = 20,
    ) -> list[Material]:
        service = service_path("product")
        product_ids: list[str] = []

        if query:
            tokens = [t for t in query.split() if t]
            # 1) Aciklamada arama
            described = self._search_descriptions(tokens, limit=limit * 4)
            # 2) Malzeme numarasinda arama
            by_id = self.v2.read(
                service,
                "A_Product",
                params={
                    "$filter": _substring_filter("Product", tokens),
                    "$select": "Product",
                    "$top": limit * 2,
                },
            )
            seen: set[str] = set()
            for candidate in described + [r.get("Product", "") for r in by_id]:
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    product_ids.append(candidate)

            # Eslesme yokken filtresiz devam etmek SAP'in ilk N malzemesini
            # arama sonucu gibi sunar. Sessiz yanlis cevap yerine bos don.
            if not product_ids:
                return []

        filters: list[str] = []
        if material_group:
            filters.append(f"ProductGroup eq '{quote(material_group)}'")
        if product_ids:
            id_filter = " or ".join(f"Product eq '{quote(p)}'" for p in product_ids[: limit * 4])
            filters.append(f"({id_filter})")

        rows = self.v2.read(
            service,
            "A_Product",
            params={
                "$filter": " and ".join(filters) if filters else None,
                "$top": max(limit, len(product_ids) or limit),
                "$expand": "to_Description,to_Plant,to_Plant/to_PlantMRPArea",
                "$select": SELECT_FIELDS["product"],
            },
        )
        materials = [self._map_product(r, plant) for r in rows]

        # Siniflandirma yalniz gerektiginde okunur: her aramada karakteristik
        # cekmek gereksiz cagri ve gecikme uretir.
        if attribute_filters:
            filtered: list[Material] = []
            for material in materials:
                classification = self.get_material_classification(material.material_id)
                if classification is None:
                    continue
                material.attributes = dict(classification.characteristics)
                if self._matches(classification, attribute_filters):
                    filtered.append(material)
            materials = filtered

        return materials[:limit]

    def _search_descriptions(self, tokens: list[str], *, limit: int) -> list[str]:
        service = service_path("product")
        if not tokens:
            return []
        rows = self.v2.read(
            service,
            "A_ProductDescription",
            params={
                "$filter": _substring_filter("ProductDescription", tokens),
                "$select": "Product,ProductDescription",
                "$top": limit,
            },
        )
        return [r.get("Product", "") for r in rows if r.get("Product")]

    @staticmethod
    def _matches(
        classification: MaterialClassification, filters: dict[str, tuple[float, float]]
    ) -> bool:
        for key, (low, high) in filters.items():
            value = classification.numeric(key)
            if value is None or not (low <= value <= high):
                return False
        return True

    def _map_product(self, row: dict, plant: str | None) -> Material:
        descriptions = expanded_rows(row, "to_Description")
        preferred = self.settings.sap.description_language
        description = next(
            (
                d.get("ProductDescription", "")
                for d in descriptions
                if str(d.get("Language", "")).upper() == preferred
            ),
            descriptions[0].get("ProductDescription", "") if descriptions else "",
        )
        plants = expanded_rows(row, "to_Plant")
        target_plant = plant or self.settings.sap.plant
        # Baska bir tesisin MRP/MOQ verisini istenen tesisin verisiymis gibi
        # kullanmayiz. Tesis belirtilmemisse ve ayarda da yoksa ilk satir
        # kullanilabilir; aksi halde yalniz tam eslesme kabul edilir.
        plant_row = (
            next((p for p in plants if p.get("Plant") == target_plant), {})
            if target_plant else (plants[0] if plants else {})
        )
        mrp_areas = expanded_rows(plant_row, "to_PlantMRPArea")
        mrp_row = next(
            (r for r in mrp_areas if r.get("MRPArea") == plant_row.get("Plant")),
            next(
                (r for r in mrp_areas if r.get("IsPlannedDeliveryTime")),
                mrp_areas[0] if mrp_areas else {},
            ),
        )

        mtart = row.get("ProductType", "ROH")
        material_id = row.get("Product", "")
        return Material(
            material_id=material_id,
            description=description,
            material_type=mtart if mtart in _MATERIAL_TYPE_FALLBACK else "ROH",
            material_group=row.get("ProductGroup", ""),
            base_unit=row.get("BaseUnit", "ST"),
            gross_weight_kg=float(row["GrossWeight"]) if row.get("GrossWeight") else None,
            procurement_type=plant_row.get("ProcurementType") or "F",
            planned_delivery_days=int(
                mrp_row.get("PlannedDeliveryDurationInDays")
                or plant_row.get("PlndDelryDurnInDays")
                or 0
            ),
            moving_avg_price=0.0,  # get_valuation ile ayrica okunur
            currency=self.settings.sap.currency,
            min_order_qty=float(plant_row.get("MinimumLotSizeQuantity") or 1),
            mrp_controller=(
                plant_row.get("MRPResponsible")
                or mrp_row.get("MRPResponsible")
                or plant_row.get("MRPController", "")
            ),
            abc_indicator=plant_row.get("ABCIndicator", ""),
            plant=target_plant,
            attributes={},
        )

    def get_material(self, material_id: str, *, plant: str | None = None) -> Material | None:
        rows = self.v2.read(
            service_path("product"),
            f"A_Product({escape_key(material_id)})",
            params={
                "$expand": "to_Description,to_Plant,to_Plant/to_PlantMRPArea",
                "$select": SELECT_FIELDS["product"],
            },
        )
        if not rows:
            return None
        material = self._map_product(rows[0], plant)
        valuation = self.get_valuation(material_id, plant=plant)
        if valuation:
            material.moving_avg_price = float(valuation.get("moving_avg_price") or 0.0)
            material.currency = valuation.get("currency") or material.currency
            material.price_unit = int(valuation.get("price_unit") or 1)
        return material

    def get_materials(
        self, material_ids: Sequence[str], *, plant: str | None = None
    ) -> dict[str, Material]:
        """Birden cok malzemeyi **iki** cagride okur (ana veri + degerleme).

        `get_material` tek kayit icin 2 cagri yapar; N kalemli bir talep
        hazirlarken bu 2N cagri demekti. Toplu okuma cagri sayisini kalem
        sayisindan bagimsiz hale getirir.
        """
        ids = [m for m in dict.fromkeys(material_ids) if m]
        if not ids:
            return {}
        id_filter = _in_filter("Product", ids)
        rows = self.v2.read(
            service_path("product"),
            "A_Product",
            params={
                "$filter": id_filter,
                "$expand": "to_Description,to_Plant,to_Plant/to_PlantMRPArea",
                "$select": SELECT_FIELDS["product"],
                "$top": len(ids),
            },
        )
        materials = {
            str(row.get("Product", "")): self._map_product(row, plant)
            for row in rows
            if row.get("Product")
        }
        for material_id, valuation in self.get_valuations(ids, plant=plant).items():
            material = materials.get(material_id)
            if material is None:
                continue
            material.moving_avg_price = float(valuation.get("moving_avg_price") or 0.0)
            material.currency = valuation.get("currency") or material.currency
            material.price_unit = int(valuation.get("price_unit") or 1)
        return materials

    def get_valuations(
        self, material_ids: Sequence[str], *, plant: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Birden cok malzemenin degerlemesini tek cagride okur."""
        capability = CAPABILITY_MANIFEST.get("valuation")
        ids = [m for m in dict.fromkeys(material_ids) if m]
        if capability is None or not ids:
            return {}
        valuation_area = plant or self.settings.sap.plant
        id_filter = _in_filter("Product", ids)
        try:
            rows = self.v2.read(
                capability.service_path,
                "A_ProductValuation",
                params={
                    "$filter": (
                        f"{id_filter} and ValuationArea eq '{quote(valuation_area)}'"
                    ),
                    "$select": SELECT_FIELDS["valuation"],
                    "$top": max(len(ids) * 2, 10),
                },
            )
        except SAPError as exc:
            log.info("Degerleme servisi okunamadi (%d malzeme): %s", len(ids), exc)
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            material_id = str(row.get("Product", ""))
            if not material_id or material_id in out:
                continue
            out[material_id] = {
                "moving_avg_price": float(row.get("MovingAveragePrice") or 0.0),
                "standard_price": float(row.get("StandardPrice") or 0.0),
                "currency": row.get("Currency") or self.settings.sap.currency,
                "price_unit": int(row.get("PriceUnitQty") or 1),
                "source_api": capability.service_path,
            }
        return out

    def get_valuation(self, material_id: str, *, plant: str | None = None) -> dict[str, Any] | None:
        """Hareketli ortalama fiyat (MBEW). Servis yoksa None doner."""
        capability = CAPABILITY_MANIFEST.get("valuation")
        if capability is None:
            return None
        return self.get_valuations([material_id], plant=plant).get(material_id)

    def get_material_classification(
        self, material_id: str, *, class_type: str = "001"
    ) -> MaterialClassification | None:
        capability = CAPABILITY_MANIFEST["classification"]
        rows = self.v2.read(
            capability.service_path,
            "A_ProductCharcValue",
            params={
                "$filter": (
                    f"Product eq '{quote(material_id)}' and "
                    f"ClassType eq '{quote(class_type)}'"
                ),
                "$select": SELECT_FIELDS["classification"],
                "$top": 200,
            },
        )
        if not rows:
            # Sinif atanmamis olabilir; bos siniflandirma acikca dondurulur ki
            # cagiran "okundu ama bos" ile "okunamadi"yi ayirt edebilsin.
            return MaterialClassification(
                material_id=material_id,
                class_type=class_type,
                characteristics={},
                units={},
                source=capability.service_path,
            )

        characteristics: dict[str, Any] = {}
        units: dict[str, str] = {}
        for row in rows:
            name = row.get("Characteristic") or row.get("CharcInternalID") or ""
            if not name:
                continue
            raw_value = (
                row.get("CharcValue")
                or row.get("CharcFromNumericValue")
                or row.get("CharcValuePositionNumber")
            )
            unit = (
                row.get("CharcFromNumericValueUnit")
                or row.get("CharcToNumericValueUnit")
                or row.get("CharcValueUnit")
                or row.get("Unit")
                or ""
            )
            key = str(name).strip().lower()
            characteristics[key] = _coerce_number(raw_value)
            if unit:
                units[key] = str(unit)

        class_rows = self.v2.read(
            capability.service_path,
            "A_ProductClass",
            params={
                "$filter": f"Product eq '{quote(material_id)}'",
                "$select": "Product,ClassInternalID,ClassType",
                "$top": 5,
            },
        )
        class_name = class_rows[0].get("ClassInternalID", "") if class_rows else ""

        return MaterialClassification(
            material_id=material_id,
            class_type=class_type,
            class_name=class_name,
            characteristics=characteristics,
            units=units,
            source=capability.service_path,
        )

    # --- Stok ---------------------------------------------------------------
    def get_stock(self, material_ids: list[str], *, plant: str | None = None) -> list[StockLevel]:
        """Stok fotografi. Malzeme sayisindan bagimsiz sabit cagri sayisi.

        Onceki surum malzeme basina 3 cagri yapiyordu (stok + acik siparis +
        rezervasyon), yani 10 malzeme = 30 round-trip. Stok ve acik siparis
        artik tek `$filter` ile toplu okunur.
        """
        target_plant = plant or self.settings.sap.plant
        ids = [m for m in dict.fromkeys(material_ids) if m]
        if not ids:
            return []
        service = service_path("stock")
        rows = self.v2.read(
            service,
            "A_MatlStkInAcctMod",
            params={
                "$filter": (
                    f"{_in_filter('Material', ids)} and Plant eq '{quote(target_plant)}'"
                ),
                "$select": SELECT_FIELDS["stock"],
                "$top": max(100, len(ids) * 20),
            },
        )
        levels = {mid: StockLevel(material_id=mid, plant=target_plant) for mid in ids}
        for row in rows:
            level = levels.get(str(row.get("Material", "")))
            if level is None:
                continue
            qty = float(row.get("MatlWrhsStkQtyInMatlBaseUnit") or 0)
            stock_type = row.get("InventoryStockType", "01")
            if stock_type == "01":
                level.unrestricted_qty += qty
            elif stock_type == "02":
                level.quality_inspection_qty += qty
            elif stock_type == "03":
                level.blocked_qty += qty
            level.storage_location = row.get("StorageLocation") or level.storage_location

        for material_id, open_qty in self._open_po_quantities(ids, target_plant).items():
            if material_id in levels:
                levels[material_id].on_order_qty = open_qty
        # Rezervasyon MRP tarafindan gelir; butun malzemeler tek
        # SupplyDemandItems cagrisi ile okunur.
        for material_id, reserved in self._reservation_quantities(ids, target_plant).items():
            if material_id in levels:
                levels[material_id].reserved_qty = reserved
        return [levels[mid] for mid in ids]

    def _open_po_quantity(self, material_id: str, plant: str) -> float:
        return self._open_po_quantities([material_id], plant).get(material_id, 0.0)

    def _open_po_quantities(
        self, material_ids: Sequence[str], plant: str
    ) -> dict[str, float]:
        """Acik siparis miktari: siparis miktari eksi teslim edilen.

        Teslim edilmis miktar acik siparis hesabindan mutlaka dusulur.
        """
        ids = [m for m in dict.fromkeys(material_ids) if m]
        if not ids:
            return {}
        service = self._alias_path("purchase_order")
        totals: dict[str, float] = dict.fromkeys(ids, 0.0)
        # Tek malzeme istendiginde `$filter` zaten ayirt ediciyi garanti eder;
        # satirda `Material` alani gelmese de sonuc dogru malzemeye yazilir.
        only = ids[0] if len(ids) == 1 else ""

        def _bucket(row: dict[str, Any]) -> str | None:
            key = str(row.get("Material", "")) or only
            return key if key in totals else None

        if self._alias_version("purchase_order") == "v2":
            rows = self.v2.read(
                service,
                "A_PurchaseOrderItem",
                params={
                    "$filter": (
                        f"{_in_filter('Material', ids)} and Plant eq '{quote(plant)}' "
                        "and IsCompletelyDelivered eq false"
                    ),
                    "$select": "Material,OrderQuantity,to_ScheduleLine/ScheduleLineDeliveredQty",
                    "$expand": "to_ScheduleLine",
                    "$top": 200,
                },
            )
            for row in rows:
                material_id = _bucket(row)
                if material_id is None:
                    continue
                ordered = float(row.get("OrderQuantity") or 0)
                delivered = sum(
                    float(line.get("ScheduleLineDeliveredQty") or 0)
                    for line in expanded_rows(row, "to_ScheduleLine")
                )
                totals[material_id] += max(0.0, ordered - delivered)
            return {k: round(v, 3) for k, v in totals.items()}

        page = self.v4.read_collection(
            service,
            "PurchaseOrderItem",
            filter_expr=(
                f"{_in_filter('Material', ids)} and Plant eq '{quote(plant)}' "
                "and IsCompletelyDelivered eq false"
            ),
            select=("Material", "PurchaseOrder", "PurchaseOrderItem", "OrderQuantity"),
            expand=("_PurchaseOrderScheduleLineTP",),
            top=200,
        )
        for row in page.rows:
            material_id = _bucket(row)
            if material_id is None:
                continue
            ordered = float(row.get("OrderQuantity") or 0)
            schedule = _nav_rows(row, "_PurchaseOrderScheduleLineTP")
            if any("OpenPurchaseOrderQuantity" in line for line in schedule):
                open_qty = sum(
                    float(line.get("OpenPurchaseOrderQuantity") or 0) for line in schedule
                )
            else:
                delivered = sum(
                    float(line.get("ScheduleLineDeliveredQuantity") or 0)
                    for line in schedule
                )
                open_qty = max(0.0, ordered - delivered)
            totals[material_id] += max(0.0, open_qty)
        return {k: round(v, 3) for k, v in totals.items()}

    #: Rezervasyon sayilan MRP element tipleri.
    _RESERVATION_ELEMENTS = frozenset({"VC", "MS", "AR"})

    def _reservation_quantity(self, material_id: str, plant: str) -> float | None:
        """MRP arz/talep elementlerinden rezervasyon toplamini cikarir."""
        return self._reservation_quantities([material_id], plant).get(material_id)

    def _reservation_quantities(
        self, material_ids: Sequence[str], plant: str
    ) -> dict[str, float]:
        """Rezervasyon toplamlarini tek MRP cagrisinda okur."""
        ids = [m for m in dict.fromkeys(material_ids) if m]
        if not ids:
            return {}
        capability = CAPABILITY_MANIFEST["mrp"]
        try:
            rows = self.v2.read(
                capability.service_path,
                "SupplyDemandItems",
                params={
                    "$filter": (
                        f"{_in_filter('Material', ids)} and "
                        f"MRPPlant eq '{quote(plant)}'"
                    ),
                    "$top": max(500, len(ids) * 200),
                },
            )
        except SAPError as exc:
            log.info("MRP rezervasyon okumasi basarisiz (%d malzeme): %s", len(ids), exc)
            return {}

        only = ids[0] if len(ids) == 1 else ""
        totals: dict[str, float] = {}
        for row in rows:
            if str(row.get("MRPElement", "")) not in self._RESERVATION_ELEMENTS:
                continue
            material_id = str(row.get("Material", "")) or only
            if material_id not in ids:
                continue
            quantity = float(
                row.get("MRPElementOpenQuantity") or row.get("MRPElementQuantity") or 0
            )
            totals[material_id] = totals.get(material_id, 0.0) - quantity
        return {mid: round(totals.get(mid, 0.0), 3) for mid in ids}

    # --- ATP ----------------------------------------------------------------
    def check_atp(
        self,
        material_id: str,
        *,
        quantity: float,
        requested_date: date | None = None,
        plant: str | None = None,
    ) -> AtpResult:
        capability = CAPABILITY_MANIFEST["availability"]
        target_plant = plant or self.settings.sap.plant
        need_by = requested_date or date.today()

        # ATP servisi parametreli bir okuma olarak calisir: istenen miktar/tarih
        # filtrede verilir, servis teyit satirlarini dondurur.
        filter_expr = (
            f"Product eq '{quote(material_id)}' and Plant eq '{quote(target_plant)}' "
            f"and RequestedQuantity eq {_number_literal(quantity)} "
            f"and RequestedDeliveryDate eq {need_by.isoformat()}"
        )
        collection = self.v4.read_collection(
            capability.service_path,
            "ProductAvailabilityInformation",
            filter_expr=filter_expr,
            top=50,
        )
        data_rows = collection.rows
        if not data_rows:
            raise SAPNotSupported(
                "atp_check",
                backend=self.name,
                hint=(
                    f"{capability.service_path} beklenen alanlari dondurmedi. "
                    "sap_discover_capabilities ile kontrati dogrulayin "
                    f"(beklenen: {', '.join(capability.critical_properties.get('ProductAvailabilityInformation', ()))})."
                ),
            )

        schedule: list[AtpScheduleLine] = []
        confirmed_total = 0.0
        for row in data_rows:
            confirmed_qty = float(row.get("ConfirmedQuantity") or 0)
            confirmed_date = parse_odata_datetime(row.get("ConfirmedDeliveryDate"))
            if confirmed_qty <= 0 or confirmed_date is None:
                continue
            schedule.append(
                AtpScheduleLine(
                    confirmed_date=confirmed_date,
                    confirmed_qty=round(confirmed_qty, 3),
                    supply_element=str(row.get("AvailabilityCheckType", "ATP")),
                )
            )
            confirmed_total += confirmed_qty

        schedule.sort(key=lambda line: line.confirmed_date)
        confirmed_by_need = round(
            sum(line.confirmed_qty for line in schedule if line.confirmed_date <= need_by), 3
        )
        return AtpResult(
            material_id=material_id,
            plant=target_plant,
            requested_qty=float(quantity),
            requested_date=requested_date,
            confirmed_qty=confirmed_by_need,
            full_confirmation_date=schedule[-1].confirmed_date if schedule else None,
            schedule_lines=schedule,
            checked_at=datetime.now(timezone.utc),
            source_api=capability.service_path,
            calendar_considered=True,
            messages=["Teyit SAP ATP kontrol kuralina gore uretildi."],
        )

    # --- MRP ----------------------------------------------------------------
    def get_supply_demand(
        self,
        material_id: str,
        *,
        plant: str | None = None,
        horizon_days: int = 180,
    ) -> list[SupplyDemandItem]:
        capability = CAPABILITY_MANIFEST["mrp"]
        target_plant = plant or self.settings.sap.plant
        horizon = date.today() + timedelta(days=horizon_days)
        try:
            rows = self.v2.read(
                capability.service_path,
                "SupplyDemandItems",
                params={
                    "$filter": (
                        f"Material eq '{quote(material_id)}' and "
                        f"MRPPlant eq '{quote(target_plant)}'"
                    ),
                    "$top": 500,
                },
            )
        except SAPError as exc:
            # Bu Gateway kodu servis/yetki hatasi degil, secilen malzeme/tesis
            # icin MRP listesi bulunmadigini bildirir. Bos is sonucu olarak
            # korunur; diger 4xx/5xx hatalari yutulmaz.
            if exc.code == "PP_MRP_RSC/010" or "no material found" in str(exc).lower():
                return []
            raise
        items: list[SupplyDemandItem] = []
        for row in rows:
            when = parse_odata_datetime(
                row.get("MRPElementAvailyOrRqmtDate") or row.get("MRPAvailabilityDate")
            )
            if when and when > horizon:
                continue
            quantity = float(
                row.get("MRPElementOpenQuantity") or row.get("MRPElementQuantity") or 0
            )
            items.append(
                SupplyDemandItem(
                    material_id=material_id,
                    plant=target_plant,
                    mrp_element=str(row.get("MRPElement", "")),
                    element_id=str(row.get("MRPElementOpenItem") or row.get("MRPElementItem") or ""),
                    availability_date=when,
                    quantity=quantity,
                    unit=str(row.get("MaterialBaseUnit") or "ST"),
                    description=str(row.get("MRPElementText") or ""),
                    wbs_element=row.get("WBSElement") or None,
                )
            )
        items.sort(key=lambda i: (i.availability_date or date.today(), -i.quantity))
        return items

    # --- Satinalma ----------------------------------------------------------
    def get_info_records(self, material_id: str, *, plant: str | None = None) -> list[InfoRecord]:
        return self.get_info_records_bulk([material_id], plant=plant).get(material_id, [])

    def get_info_records_bulk(
        self, material_ids: Sequence[str], *, plant: str | None = None
    ) -> dict[str, list[InfoRecord]]:
        """Birden cok malzemenin bilgi kaydini **iki** cagride okur.

        Kalem basina ayri okuma yerine tek `$filter`; tedarikci adlari da tek
        ek cagride tamamlanir. Boylece N kalemli bir PR hazirligi kalem
        sayisindan bagimsiz sabit sayida SAP cagrisi yapar.
        """
        ids = [m for m in dict.fromkeys(material_ids) if m]
        if not ids:
            return {}
        service = service_path("inforecord")
        id_filter = _in_filter("Material", ids)
        heads = self.v2.read(
            service,
            "A_PurchasingInfoRecord",
            params={
                "$filter": f"{id_filter} and IsDeleted eq false",
                "$expand": "to_PurgInfoRecdOrgPlantData",
                "$select": SELECT_FIELDS["inforecord"],
                "$top": max(50, len(ids) * 10),
            },
        )
        out: dict[str, list[InfoRecord]] = {mid: [] for mid in ids}
        flat: list[InfoRecord] = []
        for head in heads:
            material_id = str(head.get("Material", ""))
            if material_id not in out:
                continue
            for org in expanded_rows(head, "to_PurgInfoRecdOrgPlantData"):
                if org.get("PurchasingOrganization") != self.settings.sap.purch_org:
                    continue
                record = InfoRecord(
                    material_id=material_id,
                    vendor_id=head.get("Supplier", ""),
                    vendor_name="",
                    net_price=float(org.get("NetPriceAmount") or 0),
                    currency=org.get("Currency") or self.settings.sap.currency,
                    price_unit=int(org.get("MaterialPriceUnitQty") or 1),
                    min_order_qty=float(org.get("MinimumPurchaseOrderQuantity") or 1),
                    planned_delivery_days=int(org.get("MaterialPlannedDeliveryDurn") or 14),
                    incoterms=org.get("IncotermsClassification") or "DAP",
                    payment_terms=org.get("PaymentTerms") or "NT30",
                    valid_to=parse_odata_datetime(org.get("PriceValidityEndDate")),
                )
                out[material_id].append(record)
                flat.append(record)
        # Tedarikci adlarini tek round-trip'te tamamla.
        self._fill_vendor_names(flat)
        return out

    def _fill_vendor_names(self, records: list[InfoRecord]) -> None:
        unique_ids = sorted({r.vendor_id for r in records if r.vendor_id})
        if not unique_ids:
            return
        names = self._vendor_names(unique_ids)
        for record in records:
            record.vendor_name = names.get(record.vendor_id, record.vendor_name)

    def _vendor_names(self, vendor_ids: list[str]) -> dict[str, str]:
        """Tedarikci adlarini **tek V2 cagrisinda** okur.

        Onceki surum `self.v4.batch()` kullaniyordu: govde
        `{"requests":[...]}` seklinde JSON, yani OData **V4** `$batch`
        bicimi. Hedef servis (`API_BUSINESS_PARTNER`) V2'dir ve V2 `$batch`
        `multipart/mixed` ister; gercek Gateway bu istegi 400/415 ile
        reddederdi. Cagri `try/except` icinde oldugu icin hata yutulup bos
        sozluk donuyor, tedarikci adlari sessizce bos kaliyordu - projenin
        "sessizce bos donme" yasagina aykiri.

        Coklu ID'yi tek `$filter` ile okumak hem V2 uyumlu hem de batch
        kadar ucuz (tek round-trip).
        """
        service = service_path("supplier")
        id_filter = _in_filter("Supplier", vendor_ids)
        if not id_filter:
            return {}
        try:
            rows = self.v2.read(
                service,
                "A_Supplier",
                params={
                    "$filter": id_filter,
                    "$select": SELECT_FIELDS["supplier"],
                    "$top": max(len(vendor_ids), 10),
                },
            )
        except SAPError as exc:
            log.warning(
                "Tedarikci adlari okunamadi (%d tedarikci): %s. Adlar bos kalacak.",
                len(vendor_ids),
                exc,
            )
            return {}
        return {
            str(row.get("Supplier", "")): (
                row.get("SupplierName") or row.get("SupplierFullName", "")
            )
            for row in rows
            if row.get("Supplier")
        }

    def get_vendor(self, vendor_id: str) -> Vendor | None:
        return self.get_vendors([vendor_id]).get(vendor_id)

    def get_vendor_master(self, vendor_id: str) -> Vendor | None:
        return self._get_vendors([vendor_id], include_score=False).get(vendor_id)

    def get_vendors(self, vendor_ids: Sequence[str]) -> dict[str, Vendor]:
        return self._get_vendors(vendor_ids, include_score=True)

    def _get_vendors(
        self, vendor_ids: Sequence[str], *, include_score: bool
    ) -> dict[str, Vendor]:
        """Tedarikci ana verisi, adresi ve skorunu sabit sayida cagrida okur."""
        ids = [vendor_id for vendor_id in dict.fromkeys(vendor_ids) if vendor_id]
        if not ids:
            return {}

        # `$select` olmadan A_Supplier vergi numarasi, banka ve adres alanlarini
        # da dondururdu; satinalma karari icin gerekli degiller.
        rows = self.v2.read(
            service_path("supplier"),
            "A_Supplier",
            params={
                "$filter": _in_filter("Supplier", ids),
                "$select": SELECT_FIELDS["supplier"],
                "$top": max(50, len(ids) * 2),
            },
        )
        by_id = {
            str(row.get("Supplier")): row
            for row in rows
            if str(row.get("Supplier") or "") in ids
        }

        addresses: dict[str, dict[str, Any]] = {}
        try:
            address_rows = self.v2.read(
                service_path("supplier"),
                "A_BusinessPartnerAddress",
                params={
                    "$filter": _in_filter("BusinessPartner", ids),
                    "$select": SELECT_FIELDS["supplier_address"],
                    "$top": max(50, len(ids) * 2),
                },
            )
            for address in address_rows:
                partner = str(address.get("BusinessPartner") or "")
                if partner in ids and partner not in addresses:
                    addresses[partner] = address
        except SAPError as exc:
            # Adres yardimci veridir; tedarikci ana kaydini kullanilamaz hale
            # getirmez. Veri yoklugu bos alanla acikca gorunur.
            log.info("Tedarikci ulke/sehir bilgisi toplu okunamadi: %s", exc)

        # Performans alanlari standart tedarikci API'sinde yok; degerlendirme
        # CDS'inden gelirse doldurulur, gelmezse 0 kalir ve tool bunu isaretler.
        scores = self._get_supplier_scores(ids) if include_score else {}
        vendors: dict[str, Vendor] = {}
        for vendor_id in ids:
            row = by_id.get(vendor_id)
            if row is None:
                continue
            address = addresses.get(vendor_id, {})
            vendor = Vendor(
                vendor_id=row.get("Supplier", vendor_id),
                name=row.get("SupplierName") or row.get("SupplierFullName", ""),
                country=address.get("Country", ""),
                city=address.get("CityName", ""),
                blocked=bool(
                    row.get("PurchasingIsBlocked") or row.get("SupplierProcurementBlock")
                ),
            )
            score = scores.get(vendor_id)
            if score is not None:
                vendor.on_time_delivery_pct = score.on_time_delivery_pct or 0.0
                vendor.quality_ppm = score.quality_ppm or 0
                vendor.price_competitiveness = score.price_score or 0.0
                vendor.responsiveness = score.service_score or 0.0
            vendors[vendor_id] = vendor
        return vendors

    def get_supplier_score(
        self, vendor_id: str, *, purchasing_org: str | None = None
    ) -> SupplierScore | None:
        return self._get_supplier_scores(
            [vendor_id], purchasing_org=purchasing_org
        ).get(vendor_id)

    def _get_supplier_scores(
        self,
        vendor_ids: Sequence[str],
        *,
        purchasing_org: str | None = None,
    ) -> dict[str, SupplierScore]:
        ids = [vendor_id for vendor_id in dict.fromkeys(vendor_ids) if vendor_id]
        if not ids:
            return {}
        capability = CAPABILITY_MANIFEST["supplier_score"]
        org = purchasing_org or self.settings.sap.purch_org
        currency = self.settings.sap.currency
        period = "YEARTODATE"
        entity = (
            "A_SupplierOplScoresAV("
            f"P_DisplayCurrency='{quote(currency)}',P_DateFunction='{period}')/Results"
        )
        try:
            rows = self.v2.read(
                capability.service_path,
                entity,
                params={
                    "$filter": (
                        f"{_in_filter('Supplier', ids)} and "
                        f"PurchasingOrganization eq '{quote(org)}'"
                    ),
                    "$select": SELECT_FIELDS["supplier_score"],
                    "$top": max(200, len(ids) * 20),
                },
            )
        except SAPError as exc:
            log.info("Tedarikci skorlari toplu okunamadi (%s): %s", ids, exc)
            return {
                vendor_id: SupplierScore(
                    vendor_id=vendor_id,
                    purchasing_org=org,
                    source_api=capability.service_path,
                    estimated_fields=[
                        "overall_score",
                        "price_score",
                        "delivery_score",
                        "quantity_score",
                        "quality_score",
                    ],
                )
                for vendor_id in ids
            }

        grouped = {
            vendor_id: [
                row for row in rows if str(row.get("Supplier") or "") == vendor_id
            ]
            for vendor_id in ids
        }
        return {
            vendor_id: self._supplier_score_from_rows(
                vendor_id, org=org, rows=grouped[vendor_id], source_api=capability.service_path
            )
            for vendor_id in ids
        }

    @staticmethod
    def _supplier_score_from_rows(
        vendor_id: str,
        *,
        org: str,
        rows: list[dict[str, Any]],
        source_api: str,
    ) -> SupplierScore:
        if not rows:
            return SupplierScore(
                vendor_id=vendor_id,
                purchasing_org=org,
                source_api=source_api,
                estimated_fields=["overall_score", "delivery_score", "quality_score"],
            )

        def average(*fields: str) -> float | None:
            values = [
                value
                for row in rows
                for field in fields
                if (value := _opt_float(row.get(field))) is not None
            ]
            return round(sum(values) / len(values), 2) if values else None

        overall = average("SupplierOperationalScore")
        price = average("PriceVarianceScore")
        delivery = average("TimeVarianceScore")
        quantity = average("QuantityVarianceScore")
        quality = average("InspectionLotQualityScore", "QualityNotificationScore")
        estimated = [
            name
            for name, value in (
                ("overall_score", overall),
                ("price_score", price),
                ("delivery_score", delivery),
                ("quantity_score", quantity),
                ("quality_score", quality),
            )
            if value is None
        ]
        return SupplierScore(
            vendor_id=vendor_id,
            purchasing_org=org,
            overall_score=overall,
            price_score=price,
            delivery_score=delivery,
            quantity_score=quantity,
            quality_score=quality,
            on_time_delivery_pct=delivery,
            evaluated_period="YEARTODATE",
            source_api=source_api,
            estimated_fields=estimated,
        )

    # --- PR: prepare / submit / read ---------------------------------------
    def prepare_purchase_requisition(
        self,
        items: list[PurchaseRequisitionItem],
        *,
        header_text: str = "",
        purchase_group: str | None = None,
    ) -> PurchaseRequisitionDraft:
        if not items:
            raise SAPError("Satinalma talebi en az bir kalem icermeli.", code="EBAN_NO_ITEMS")

        cfg = self.settings.sap
        findings: list[ValidationFinding] = []
        priced: list[dict[str, Any]] = []
        odata_items: list[dict[str, Any]] = []
        diff: list[dict[str, Any]] = []
        total = 0.0

        # Kalem basina ayri okuma yerine toplu okuma: 10 kalemli bir talep
        # 30+ SAP cagrisi yerine 3 cagri ile hazirlanir.
        wanted_ids = [item.material_id for item in items]
        masters = self.get_materials(wanted_ids, plant=items[0].plant or cfg.plant)
        info_by_material = self.get_info_records_bulk(wanted_ids)

        for idx, item in enumerate(items, start=1):
            item_no = idx * 10
            master = masters.get(item.material_id)
            if master is None:
                raise SAPError(
                    f"Malzeme {item.material_id} malzeme ana verisinde bulunamadi.",
                    code="MM_MATNR_NOT_FOUND",
                )
            records = info_by_material.get(item.material_id, [])
            chosen = None
            if item.preferred_vendor:
                chosen = next((r for r in records if r.vendor_id == item.preferred_vendor), None)
                if chosen is None:
                    findings.append(
                        ValidationFinding(
                            severity="warning", field="preferred_vendor", item_no=item_no,
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
                        severity="warning", field="net_price", item_no=item_no,
                        message=(
                            f"Kalem {item_no}: fiyat bulunamadi (bilgi kaydi ve degerleme bos). "
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
                        severity="warning", field="quantity", item_no=item_no,
                        message=(
                            f"Kalem {item_no}: miktar {item.quantity:g} < minimum siparis miktari "
                            f"{moq:g}."
                        ),
                    )
                )

            lead = chosen.planned_delivery_days if chosen else master.planned_delivery_days
            earliest = date.today() + timedelta(days=lead)
            delivery = item.delivery_date or earliest
            if item.delivery_date and item.delivery_date < earliest:
                findings.append(
                    ValidationFinding(
                        severity="warning", field="delivery_date", item_no=item_no,
                        message=(
                            f"Kalem {item_no}: istenen teslim {item.delivery_date}, en erken "
                            f"{earliest} ({lead} gun)."
                        ),
                    )
                )
            if not (item.wbs_element or item.cost_center):
                findings.append(
                    ValidationFinding(
                        severity="warning", field="account_assignment", item_no=item_no,
                        message=f"Kalem {item_no}: hesap atamasi (WBS/masraf merkezi) yok.",
                    )
                )

            odata_items.append(
                self._pr_item_payload(
                    item,
                    item_no=item_no,
                    master=master,
                    unit_price=unit_price,
                    delivery=delivery,
                    purchase_group=purchase_group,
                )
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
                    "plant": item.plant or cfg.plant,
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
        capability = CAPABILITY_MANIFEST["purchase_requisition"]
        payload = {
            "PurchaseRequisitionType": self.document_type,
            "PurchaseRequisitionHeaderText": header_text[:40],
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
            source_api=capability.service_path,
            requires_human_approval=total > cfg.approval_threshold,
        )

    def _pr_item_payload(
        self,
        item: PurchaseRequisitionItem,
        *,
        item_no: int,
        master: Material,
        unit_price: Any,
        delivery: date,
        purchase_group: str | None,
    ) -> dict[str, Any]:
        """Bir PR kaleminin OData govdesi.

        Iki duzeltme burada yasiyor:

        1. **Hesap atamasi kalemin uzerinde degildir.** Released PR API'sinde
           kalem yalniz `AccountAssignmentCategory` (EBAN-KNTTP) tasir; WBS
           elemani ve masraf merkezi ayri bir alt entity'dedir
           (`_PurchaseReqnAcctAssgmt`). Eski surum `WBSElement`/`CostCenter`
           alanlarini dogrudan kaleme yaziyordu: gercek sistemde ya 400 doner
           ya da alan sessizce yok sayilip **hesap atamasi olmayan** bir talep
           olusur. Ikincisi daha kotudur; belge acilir ama proje maliyeti
           yanlis yere duser.

        2. **Bos alan gonderilmez.** `"FixedSupplier": ""` gibi degerler
           Gateway dogrulamalarinda "deger verildi ama gecersiz" olarak
           yorumlanabilir. Alan yoksa hic gonderilmez.
        """
        cfg = self.settings.sap
        payload: dict[str, Any] = {
            "PurchaseRequisitionItem": f"{item_no:05d}",
            "Material": item.material_id,
            "Plant": item.plant or cfg.plant,
            # Edm.Decimal -> JSON sayisi (string degil).
            "RequestedQuantity": _decimal(item.quantity),
            "BaseUnit": item.unit or master.base_unit,
            "DeliveryDate": delivery.isoformat(),
            "PurchasingGroup": purchase_group or cfg.purch_group,
            "PurchasingOrganization": cfg.purch_org,
            "CompanyCode": cfg.company_code,
            "PurchaseRequisitionPrice": _decimal(unit_price, 2),
            "PurReqnItemCurrency": item.currency or cfg.currency,
        }
        if item.preferred_vendor:
            payload["FixedSupplier"] = item.preferred_vendor
        if item.item_text:
            payload["PurchaseRequisitionItemText"] = item.item_text[:40]

        # --- Hesap atamasi --------------------------------------------------
        # Sekil TAHMIN EDILMEZ, hedef sistemin $metadata'sindan okunur. Iki
        # gecerli sekil vardir ve hangisinin dogru oldugu servis surumune ve
        # sisteme gore degisir; yanlis secim ya 400 doner ya da hesap atamasi
        # OLMAYAN bir belge acar - ikincisi sessizdir ve daha pahalidir.
        if not (item.wbs_element or item.cost_center):
            return payload

        if item.wbs_element:
            category, field_name, value = (
                ACCT_ASSIGN_PROJECT, "WBSElement", item.wbs_element
            )
        else:
            category, field_name, value = (
                ACCT_ASSIGN_COST_CENTER, "CostCenter", item.cost_center
            )

        shape, contract = self._account_assignment_shape()
        # Kategori alani sozlesmede YOK oldugu KANITLANMADIKCA gonderilir.
        # Bos bir sozlesme "alan yok" degil "kanit yok" demektir; kanitsiz
        # alan dusurmek, hesap atamasi olmayan bir belge acmaya yol acardi.
        known = contract is not None and not contract.is_empty()
        if not known or "AccountAssignmentCategory" in contract.properties_of_set(
            self._pr_item_entity_set()
        ):
            payload["AccountAssignmentCategory"] = category

        if shape == "inline":
            payload[field_name] = value
        else:
            # `child` ve `unknown`: released API'de beklenen sekil alt entity.
            nav = "to_PurchaseReqnAcctAssgmt" if self._alias_version(
                "purchase_requisition"
            ) == "v2" else "_PurchaseReqnAcctAssgmt"
            payload[nav] = [
                {field_name: value, "PurchaseRequisitionAcctAssgmt": "01"}
            ]
        return payload

    def _pr_item_entity_set(self) -> str:
        return (
            "A_PurchaseReqnItem"
            if self._alias_version("purchase_requisition") == "v2"
            else "PurchaseRequisitionItem"
        )

    def _account_assignment_shape(self) -> tuple[str, Any]:
        """Hesap atamasi bu sistemde nereye yazilir? (sekil, sozlesme).

        Sozlesme okunamazsa ('unknown', None) doner ve released API sekli
        (alt entity) varsayilir - ama bu varsayim loglanir ve taslakta
        uyari olarak gorunur, sessizce yapilmaz.
        """
        cached = getattr(self, "_acct_shape_cache", None)
        if cached is not None:
            return cached
        alias = self._alias_for("purchase_requisition")
        item_set = self._pr_item_entity_set()
        try:
            contract = self.metadata_contract(alias)
        except SAPError as exc:
            log.warning(
                "PR sozlesmesi okunamadi (%s); hesap atamasi icin released API "
                "sekli (alt entity) varsayiliyor. Yazmadan once "
                "sap_discover_capabilities ile dogrulayin.",
                exc.code,
            )
            result: tuple[str, Any] = ("unknown", None)
        else:
            shape = account_assignment_shape(contract, item_set)
            if shape == "unknown":
                log.warning(
                    "PR kalem sozlesmesinde hesap atamasi sekli belirlenemedi "
                    "(%s); alt entity varsayiliyor.", item_set
                )
            else:
                log.info("PR hesap atamasi sekli: %s (%s)", shape, item_set)
            result = (shape, contract)
        self._acct_shape_cache = result
        return result

    @staticmethod
    def _to_v2_item(item: dict[str, Any]) -> dict[str, Any]:
        """V4 kalem govdesini V2 (`API_PURCHASEREQ_PROCESS_SRV`) bicimine cevirir.

        Farklar: navigation adi `to_...` on ekli, sayisal alanlar string,
        tarihler `/Date(ms)/` literali.
        """
        out = {k: v for k, v in item.items() if not k.startswith("_")}
        for key in ("RequestedQuantity", "PurchaseRequisitionPrice"):
            if key in out:
                out[key] = str(out[key])
        delivery = out.get("DeliveryDate")
        if isinstance(delivery, str) and len(delivery) == 10:
            out["DeliveryDate"] = to_odata_datetime(date.fromisoformat(delivery))
        assignments = item.get("_PurchaseReqnAcctAssgmt")
        if assignments:
            out["to_PurchaseReqnAcctAssgmt"] = list(assignments)
        return out

    def submit_purchase_requisition(
        self,
        draft: PurchaseRequisitionDraft,
        *,
        external_reference: str,
        correlation_id: str = "",
    ) -> PurchaseRequisitionResult:
        if not draft.is_submittable:
            raise SAPError(
                "Taslakta engelleyici bulgular var; SAP'a gonderilmedi: "
                + "; ".join(f.message for f in draft.blocking_findings),
                code="EBAN_VALIDATION_FAILED",
            )

        alias = self._alias_for("purchase_requisition")
        capability = CAPABILITY_MANIFEST[alias]
        token = reference_token(external_reference)
        self._assert_write_shape(alias, capability, draft)
        # Referans basliga yazilir: timeout sonrasi mutabakat bu deger uzerinden
        # yapilir. 40 karakter siniri nedeniyle hash kullanilir.
        header_text = f"{token} {draft.header_text}"[:40]
        items = draft.payload.get("items", [])

        if capability.odata_version == "v2":
            body = {
                "PurchaseRequisitionType": draft.payload.get(
                    "PurchaseRequisitionType", self.document_type
                ),
                "PurchaseRequisitionHeaderText": header_text,
                "to_PurchaseReqnItem": [self._to_v2_item(i) for i in items],
            }
            created = self.v2.create(
                capability.service_path,
                "A_PurchaseRequisitionHeader",
                body,
                correlation_id=correlation_id,
            )
            etag = ""
        else:
            body = {
                "PurchaseRequisitionType": draft.payload.get(
                    "PurchaseRequisitionType", self.document_type
                ),
                "PurchaseRequisitionHeaderText": header_text,
                "_PurchaseRequisitionItem": items,
            }
            created, etag = self.v4.create(
                capability.service_path,
                "PurchaseRequisition",
                body,
                correlation_id=correlation_id,
            )
        pr_id = str(created.get("PurchaseRequisition", "") or "")
        log.info(
            "SAP PR olusturuldu: %s (ref %s, %s)", pr_id, token, capability.odata_version
        )
        return PurchaseRequisitionResult(
            requisition_id=pr_id or None,
            created=bool(pr_id),
            dry_run=False,
            requires_human_approval=False,
            total_value=draft.total_value,
            currency=draft.currency,
            items=draft.items,
            messages=[f"Satinalma talebi {pr_id} olusturuldu."],
            external_reference=token,
            etag=etag,
        )

    def _assert_write_shape(self, alias: str, capability, draft) -> None:
        """POST'tan ONCE govdeyi $metadata sozlesmesine karsi denetler.

        Bu bir okuma ile yapilan yazma dogrulamasidir: yanlis alan adi veya
        yanlis ic ice yapi, SAP'a hic gitmeden yakalanir. Boylece hata,
        anlasilmaz bir Gateway 400'u yerine hangi alanin neden yanlis
        oldugunu soyleyen bir mesaj olur.

        **Sinir:** yalniz YAPIYI dogrular. SAP'in calisma zamani is
        dogrulamalari (butce, kaynak tayini, release stratejisi) buradan
        gorunmez; onlari yalniz gercek yazma gosterir.
        """
        try:
            contract = self.metadata_contract(alias)
        except SAPError:
            return  # Sozlesme yoksa on denetim atlanir; yazma yine denenir.
        if contract.is_empty():
            return
        if capability.odata_version == "v2":
            entity_set, items_key = "A_PurchaseRequisitionHeader", "to_PurchaseReqnItem"
            items = [self._to_v2_item(i) for i in draft.payload.get("items", [])]
        else:
            entity_set, items_key = "PurchaseRequisition", "_PurchaseRequisitionItem"
            items = draft.payload.get("items", [])
        body = {
            "PurchaseRequisitionType": draft.payload.get(
                    "PurchaseRequisitionType", self.document_type
                ),
            "PurchaseRequisitionHeaderText": draft.header_text[:40],
            items_key: items,
        }
        report = verify_write_shape(contract, entity_set, body)
        if report.ok:
            return
        problems = "; ".join(
            f"{i.path}: {i.message}" for i in report.issues if i.severity == "error"
        )
        raise SAPError(
            "Satinalma talebi govdesi hedef sistemin sozlesmesine uymuyor; "
            f"SAP'a gonderilmedi. {problems}",
            code="WRITE_SHAPE_MISMATCH",
            detail=capability.service_path,
        )

    def read_purchase_requisition(self, requisition_id: str) -> dict[str, Any] | None:
        alias = self._alias_for("purchase_requisition")
        capability = CAPABILITY_MANIFEST[alias]
        if capability.odata_version == "v2":
            rows = self.v2.read(
                capability.service_path,
                f"A_PurchaseRequisitionHeader({escape_key(requisition_id)})",
                params={"$expand": "to_PurchaseReqnItem"},
            )
            if not rows:
                return None
            row = rows[0]
            items = expanded_rows(row, "to_PurchaseReqnItem")
            etag = ""
        else:
            row, etag = self.v4.read_entity(
                capability.service_path,
                f"PurchaseRequisition({escape_key(requisition_id)})",
                params={"$expand": "_PurchaseRequisitionItem"},
            )
            if row is None:
                return None
            items = _nav_rows(row, "_PurchaseRequisitionItem")
        total = sum(
            float(i.get("PurchaseRequisitionPrice") or 0) * float(i.get("RequestedQuantity") or 0)
            for i in items
        )
        return {
            "PurchaseRequisition": row.get("PurchaseRequisition", requisition_id),
            "PurchaseRequisitionHeaderText": row.get("PurchaseRequisitionHeaderText", ""),
            "item_count": len(items),
            "total_value": round(total, 2),
            "etag": etag,
            "items": items,
        }

    def find_purchase_requisition_by_reference(
        self, external_reference: str
    ) -> tuple[str, dict[str, Any]] | None:
        alias = self._alias_for("purchase_requisition")
        capability = CAPABILITY_MANIFEST[alias]
        token = (
            external_reference
            if external_reference.startswith(_REFERENCE_PREFIX)
            else reference_token(external_reference)
        )
        if capability.odata_version == "v2":
            # V2 `contains` desteklemez; `substringof(deger, alan)` kullanilir.
            rows = self.v2.read(
                capability.service_path,
                "A_PurchaseRequisitionHeader",
                params={
                    "$filter": (
                        f"substringof('{quote(token)}',PurchaseRequisitionHeaderText)"
                    ),
                    "$select": "PurchaseRequisition,PurchaseRequisitionHeaderText",
                    "$top": 5,
                },
            )
        else:
            page = self.v4.read_collection(
                capability.service_path,
                "PurchaseRequisition",
                filter_expr=f"contains(PurchaseRequisitionHeaderText,'{quote(token)}')",
                select=("PurchaseRequisition", "PurchaseRequisitionHeaderText"),
                top=5,
            )
            rows = page.rows
        if not rows:
            return None
        pr_id = str(rows[0].get("PurchaseRequisition", ""))
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
        alias = self._alias_for("purchase_order")
        capability = CAPABILITY_MANIFEST[alias]
        filters: list[str] = []
        if material_id:
            filters.append(f"Material eq '{quote(material_id)}'")
        if only_open:
            filters.append("IsCompletelyDelivered eq false")

        if capability.odata_version == "v2":
            if wbs_element:
                filters.append(f"WBSElement eq '{quote(wbs_element)}'")
            rows = self.v2.read(
                capability.service_path,
                "A_PurchaseOrderItem",
                params={
                    "$filter": " and ".join(filters) if filters else None,
                    "$expand": "to_PurchaseOrder,to_ScheduleLine",
                    "$top": limit,
                },
            )
            return self._map_v2_purchase_orders(rows, vendor_id=vendor_id)

        if wbs_element:
            filters.append(
                "_PurOrdAccountAssignment/any(a:a/WBSElementExternalID eq "
                f"'{quote(wbs_element)}')"
            )

        # Baslik ve hesap atamasi $expand ile geliyor: baslik/kalem basina ek
        # GET yok (N+1 kalkti).
        page = self.v4.read_collection(
            capability.service_path,
            "PurchaseOrderItem",
            filter_expr=" and ".join(filters),
            select=(
                "PurchaseOrder", "PurchaseOrderItem", "Material", "OrderQuantity",
                "NetPriceAmount", "DocumentCurrency", "PurchaseOrderItemText",
                "IsCompletelyDelivered",
            ),
            expand=(
                "_PurchaseOrder", "_PurchaseOrderScheduleLineTP", "_PurOrdAccountAssignment",
            ),
            top=limit,
        )

        out: list[PurchaseOrder] = []
        for row in page.rows:
            header = _nav_single(row, "_PurchaseOrder")
            supplier = header.get("Supplier", "")
            if vendor_id and supplier != vendor_id:
                continue

            schedule = _nav_rows(row, "_PurchaseOrderScheduleLineTP")
            assignments = _nav_rows(row, "_PurOrdAccountAssignment")
            assigned_wbs = next(
                (
                    str(a.get("WBSElementExternalID") or a.get("WBSElementInternalID") or "")
                    for a in assignments
                    if a.get("WBSElementExternalID") or a.get("WBSElementInternalID")
                ),
                "",
            )
            ordered = float(row.get("OrderQuantity") or 0)
            if any("OpenPurchaseOrderQuantity" in line for line in schedule):
                open_qty = sum(
                    float(line.get("OpenPurchaseOrderQuantity") or 0) for line in schedule
                )
                delivered = max(0.0, ordered - open_qty)
            else:
                delivered = sum(
                    float(s.get("ScheduleLineDeliveredQuantity") or 0) for s in schedule
                )
            requested = _weighted_date(schedule, "ScheduleLineDeliveryDate")
            confirmed = (
                _weighted_date(schedule, "SchedLineStscDeliveryDate")
                or _weighted_date(schedule, "PurchaseOrderConfirmedDeliveryDate")
                or requested
            )

            if delivered <= 0:
                status = "open"
            elif delivered < ordered:
                status = "partially_delivered"
            else:
                status = "delivered"

            out.append(
                PurchaseOrder(
                    po_id=str(row.get("PurchaseOrder", "")),
                    vendor_id=supplier,
                    vendor_name=header.get("SupplierName", ""),
                    created_on=parse_odata_datetime(header.get("CreationDate")),
                    currency=row.get("DocumentCurrency") or self.settings.sap.currency,
                    net_value=float(row.get("NetPriceAmount") or 0) * ordered,
                    status=status,
                    material_id=row.get("Material", ""),
                    description=row.get("PurchaseOrderItemText", ""),
                    quantity=ordered,
                    delivered_qty=delivered,
                    confirmed_delivery_date=confirmed,
                    requested_delivery_date=requested,
                    wbs_element=assigned_wbs or None,
                )
            )
        return out

    def _map_v2_purchase_orders(
        self, rows: list[dict[str, Any]], *, vendor_id: str | None
    ) -> list[PurchaseOrder]:
        """V2 (`API_PURCHASEORDER_PROCESS_SRV`) satirlarini domain modeline cevirir."""
        out: list[PurchaseOrder] = []
        for row in rows:
            header = (expanded_rows(row, "to_PurchaseOrder") or [{}])[0]
            supplier = header.get("Supplier", "")
            if vendor_id and supplier != vendor_id:
                continue
            schedule = expanded_rows(row, "to_ScheduleLine")
            ordered = float(row.get("OrderQuantity") or 0)
            delivered = sum(
                float(s.get("ScheduleLineDeliveredQty") or 0) for s in schedule
            )
            requested = _weighted_date(schedule, "ScheduleLineDeliveryDate")
            confirmed = _weighted_date(schedule, "PurchaseOrderConfirmedDelivDate") or requested
            if delivered <= 0:
                status = "open"
            elif delivered < ordered:
                status = "partially_delivered"
            else:
                status = "delivered"
            out.append(
                PurchaseOrder(
                    po_id=str(row.get("PurchaseOrder", "")),
                    vendor_id=supplier,
                    vendor_name=header.get("SupplierName", ""),
                    created_on=parse_odata_datetime(header.get("CreationDate")),
                    currency=row.get("DocumentCurrency") or self.settings.sap.currency,
                    net_value=float(row.get("NetPriceAmount") or 0) * ordered,
                    status=status,
                    material_id=row.get("Material", ""),
                    description=row.get("PurchaseOrderItemText", ""),
                    quantity=ordered,
                    delivered_qty=delivered,
                    confirmed_delivery_date=confirmed,
                    requested_delivery_date=requested,
                    wbs_element=row.get("WBSElement") or None,
                )
            )
        return out

    # --- Procure-to-pay gorunurlugu ---------------------------------------
    def get_purchase_order_items(self, po_id: str) -> list[PurchaseOrderItem]:
        """PO kalemlerini released V2 API'den okur.

        Teslim ve fatura miktarlari bu entity'de yer almaz. `sap_purchase_order_360`
        bunlari ayni kosuda malzeme belgesi ve fatura referanslarindan netlestirir;
        burada `IsCompletelyDelivered` gibi bir bayraktan miktar UYDURULMAZ.
        """
        rows = self.v2.read(
            service_path("purchase_order_v2"),
            "A_PurchaseOrderItem",
            params={
                "$filter": f"PurchaseOrder eq '{quote(po_id)}'",
                "$expand": "to_AccountAssignment",
                "$select": SELECT_FIELDS["po_item_p2p"],
                "$top": 500,
            },
        )
        out: list[PurchaseOrderItem] = []
        for row in rows:
            quantity = float(row.get("OrderQuantity") or 0)
            price_qty = float(row.get("NetPriceQuantity") or 1) or 1.0
            unit_price = float(row.get("NetPriceAmount") or 0) / price_qty
            assignments = expanded_rows(row, "to_AccountAssignment")
            wbs = next(
                (
                    str(a.get("WBSElementExternalID") or "")
                    for a in assignments
                    if a.get("WBSElementExternalID")
                ),
                "",
            )
            out.append(
                PurchaseOrderItem(
                    po_id=str(row.get("PurchaseOrder") or po_id),
                    item_no=str(row.get("PurchaseOrderItem") or ""),
                    material_id=str(row.get("Material") or ""),
                    description=str(row.get("PurchaseOrderItemText") or ""),
                    plant=str(row.get("Plant") or ""),
                    quantity=quantity,
                    unit=str(row.get("PurchaseOrderQuantityUnit") or "ST"),
                    net_price=round(unit_price, 6),
                    net_value=round(unit_price * quantity, 2),
                    currency=str(row.get("DocumentCurrency") or self.settings.sap.currency),
                    delivered_qty=0.0,
                    invoiced_qty=0.0,
                    goods_receipt_required=_sap_bool(
                        row.get("GoodsReceiptIsExpected"), default=True
                    ),
                    invoice_receipt_required=_sap_bool(
                        row.get("InvoiceIsExpected"), default=True
                    ),
                    deletion_indicator=bool(
                        str(row.get("PurchasingDocumentDeletionCode") or "").strip()
                    ),
                    wbs_element=wbs or None,
                    account_assignment=str(row.get("AccountAssignmentCategory") or ""),
                )
            )
        return out

    def get_schedule_lines(self, po_id: str, *, item_no: str = "") -> list[ScheduleLine]:
        clauses = [f"PurchasingDocument eq '{quote(po_id)}'"]
        if item_no:
            clauses.append(f"PurchasingDocumentItem eq '{quote(item_no)}'")
        rows = self.v2.read(
            service_path("purchase_order_v2"),
            "A_PurchaseOrderScheduleLine",
            params={
                "$filter": " and ".join(clauses),
                "$select": SELECT_FIELDS["po_schedule_p2p"],
                "$top": 500,
            },
        )
        # Released entity supplier-confirmed date tasimaz. SchedLineStscDeliveryDate
        # istatistik tarihidir; teyit tarihi gibi sunulmaz.
        return [
            ScheduleLine(
                po_id=str(row.get("PurchasingDocument") or po_id),
                item_no=str(row.get("PurchasingDocumentItem") or ""),
                schedule_line=str(row.get("ScheduleLine") or "0001"),
                requested_date=parse_odata_datetime(row.get("ScheduleLineDeliveryDate")),
                confirmed_date=None,
                quantity=float(row.get("ScheduleLineOrderQuantity") or 0),
                delivered_qty=0.0,
                unit=str(row.get("PurchaseOrderQuantityUnit") or "ST"),
            )
            for row in rows
        ]

    def get_goods_receipts(
        self, *, po_id: str = "", material_id: str = "", limit: int = 50
    ) -> list[GoodsReceipt]:
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
        rows = self.v2.read(
            service_path("material_document"),
            "A_MaterialDocumentItem",
            params={
                "$filter": " and ".join(clauses),
                "$expand": "to_MaterialDocumentHeader",
                "$select": SELECT_FIELDS["material_document_p2p"],
                "$top": min(max(1, limit), 500),
            },
        )
        out: list[GoodsReceipt] = []
        for row in rows:
            header = (expanded_rows(row, "to_MaterialDocumentHeader") or [{}])[0]
            movement = str(row.get("GoodsMovementType") or "")
            cancelled = _sap_bool(row.get("GoodsMovementIsCancelled"))
            out.append(
                GoodsReceipt(
                    material_document=str(row.get("MaterialDocument") or ""),
                    document_year=_safe_int(row.get("MaterialDocumentYear")),
                    item_no=str(row.get("MaterialDocumentItem") or "0001"),
                    posting_date=parse_odata_datetime(
                        header.get("PostingDate") or row.get("PostingDate")
                    ),
                    movement_type=movement or "101",
                    material_id=str(row.get("Material") or ""),
                    plant=str(row.get("Plant") or ""),
                    quantity=abs(float(row.get("QuantityInEntryUnit") or 0)),
                    unit=str(row.get("EntryUnit") or "ST"),
                    po_id=str(row.get("PurchaseOrder") or ""),
                    po_item=str(row.get("PurchaseOrderItem") or ""),
                    batch=str(row.get("Batch") or ""),
                    reversed=cancelled or movement in {"102", "122", "162"},
                )
            )
        return out

    def get_supplier_invoices(
        self,
        *,
        invoice_id: str = "",
        po_id: str = "",
        vendor_id: str = "",
        only_blocked: bool = False,
        limit: int = 50,
    ) -> list[SupplierInvoice]:
        """Fatura basligi ile PO referanslarini iki toplu sorguda birlestirir.

        PO referansi baslikta degil `A_SuplrInvcItemPurOrdRef` entity'sindedir.
        Bu nedenle `po_id` verildiginde once ilgili kalem anahtarlari, sonra tum
        basliklar tek OR filtresiyle okunur; kalem basina N+1 yapilmaz.
        """
        service = service_path("supplier_invoice")
        preselected_items: list[dict[str, Any]] = []
        header_keys: list[tuple[str, str]] = []
        if po_id:
            preselected_items = self.v2.read(
                service,
                "A_SuplrInvcItemPurOrdRef",
                params={
                    "$filter": f"PurchaseOrder eq '{quote(po_id)}'",
                    "$select": (
                        "SupplierInvoice,FiscalYear,SupplierInvoiceItem,PurchaseOrder,"
                        "PurchaseOrderItem,QuantityInPurchaseOrderUnit,DocumentCurrency,"
                        "SupplierInvoiceItemAmount"
                    ),
                    "$top": min(max(20, limit * 10), 500),
                },
            )
            header_keys = list(
                dict.fromkeys(
                    (
                        str(row.get("SupplierInvoice") or ""),
                        str(row.get("FiscalYear") or ""),
                    )
                    for row in preselected_items
                    if row.get("SupplierInvoice")
                )
            )
            if not header_keys:
                return []

        filters: list[str] = []
        if header_keys:
            key_filter = " or ".join(
                "(SupplierInvoice eq '"
                + quote(inv)
                + "' and FiscalYear eq '"
                + quote(year)
                + "')"
                for inv, year in header_keys
            )
            filters.append(f"({key_filter})")
        elif invoice_id:
            filters.append(f"SupplierInvoice eq '{quote(invoice_id)}'")
        if vendor_id:
            filters.append(f"InvoicingParty eq '{quote(vendor_id)}'")
        if only_blocked:
            filters.append("PaymentBlockingReason ne ''")

        rows = self.v2.read(
            service,
            "A_SupplierInvoice",
            params={
                "$filter": " and ".join(filters) if filters else None,
                "$expand": "to_SuplrInvcItemPurOrdRef,to_SupplierInvoiceTax",
                "$select": SELECT_FIELDS["supplier_invoice_p2p"],
                "$top": min(max(1, limit), 200),
            },
        )

        item_by_header: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in preselected_items:
            key = (str(item.get("SupplierInvoice") or ""), str(item.get("FiscalYear") or ""))
            item_by_header.setdefault(key, []).append(item)

        out: list[SupplierInvoice] = []
        for row in rows:
            inv_id = str(row.get("SupplierInvoice") or "")
            fiscal_text = str(row.get("FiscalYear") or "")
            items = item_by_header.get((inv_id, fiscal_text)) or expanded_rows(
                row, "to_SuplrInvcItemPurOrdRef"
            )
            taxes = expanded_rows(row, "to_SupplierInvoiceTax")
            po_ids = sorted(
                {str(item.get("PurchaseOrder")) for item in items if item.get("PurchaseOrder")}
            )
            quantities: dict[str, float] = {}
            for item in items:
                item_po = str(item.get("PurchaseOrder") or "")
                item_no = str(item.get("PurchaseOrderItem") or "")
                if not (item_po and item_no):
                    continue
                key = f"{item_po}/{item_no}"
                quantities[key] = round(
                    quantities.get(key, 0.0)
                    + abs(float(item.get("QuantityInPurchaseOrderUnit") or 0)),
                    3,
                )

            gross = float(row.get("InvoiceGrossAmount") or 0)
            tax = round(sum(float(t.get("TaxAmount") or 0) for t in taxes), 2)
            payment_block = str(row.get("PaymentBlockingReason") or "").strip()
            status = _supplier_invoice_status(row, payment_block)
            due_base = parse_odata_datetime(row.get("DueCalculationBaseDate"))
            net_days = _safe_int(row.get("NetPaymentDays"))
            due_date = due_base + timedelta(days=net_days) if due_base else None
            blocks = []
            if payment_block:
                blocks.append(
                    InvoiceBlock(
                        invoice_id=inv_id,
                        block_reason="manual",
                        tolerance_key=payment_block,
                        currency=str(row.get("DocumentCurrency") or ""),
                        description=(
                            "Released Supplier Invoice API yalniz blokaj anahtarini yayinlar; "
                            "OMR6 tolerans degerleri bu kaynaktan okunamadi."
                        ),
                    )
                )
            out.append(
                SupplierInvoice(
                    invoice_id=inv_id,
                    fiscal_year=_safe_int(fiscal_text),
                    vendor_id=str(row.get("InvoicingParty") or ""),
                    company_code=str(row.get("CompanyCode") or ""),
                    invoice_date=parse_odata_datetime(row.get("DocumentDate")),
                    posting_date=parse_odata_datetime(row.get("PostingDate")),
                    due_date=due_date,
                    gross_amount=gross,
                    net_amount=round(gross - tax, 2),
                    tax_amount=tax,
                    currency=str(row.get("DocumentCurrency") or self.settings.sap.currency),
                    status=status,
                    payment_block=payment_block,
                    payment_terms=str(row.get("PaymentTerms") or ""),
                    paid_on=None,
                    accounting_document="",
                    po_ids=po_ids,
                    po_item_quantities=quantities,
                    blocks=blocks,
                    source_api=f"{service}/A_SupplierInvoice",
                )
            )

        names = self._vendor_names([inv.vendor_id for inv in out if inv.vendor_id])
        for invoice in out:
            invoice.vendor_name = names.get(invoice.vendor_id, "")
        return out

    def get_document_flow(
        self,
        document_id: str,
        *,
        document_type: str = "auto",
        include_payments: bool = True,
    ) -> list[DocumentFlowNode]:
        target = document_id.strip()
        if not target:
            raise SAPError("Belge numarasi bos olamaz.", code="DOC_ID_REQUIRED")

        po_ids: set[str] = set()
        known_items: dict[str, list[PurchaseOrderItem]] = {}
        resolved = document_type
        if resolved in {"auto", "purchase_order"}:
            initial_items = self.get_purchase_order_items(target)
            if initial_items:
                po_ids.add(target)
                known_items[target] = initial_items
                resolved = "purchase_order"
        if not po_ids and resolved in {"auto", "purchase_requisition"}:
            rows = self.v2.read(
                service_path("purchase_order_v2"),
                "A_PurchaseOrderItem",
                params={
                    "$filter": f"PurchaseRequisition eq '{quote(target)}'",
                    "$select": "PurchaseOrder,PurchaseRequisition",
                    "$top": 200,
                },
            )
            po_ids.update(str(r.get("PurchaseOrder")) for r in rows if r.get("PurchaseOrder"))
            if po_ids:
                resolved = "purchase_requisition"
        if not po_ids and resolved in {"auto", "goods_receipt"}:
            rows = self.v2.read(
                service_path("material_document"),
                "A_MaterialDocumentItem",
                params={
                    "$filter": f"MaterialDocument eq '{quote(target)}'",
                    "$select": "MaterialDocument,PurchaseOrder",
                    "$top": 200,
                },
            )
            po_ids.update(str(r.get("PurchaseOrder")) for r in rows if r.get("PurchaseOrder"))
            if po_ids:
                resolved = "goods_receipt"
        if not po_ids and resolved in {"auto", "supplier_invoice"}:
            invoices = self.get_supplier_invoices(invoice_id=target, limit=20)
            po_ids.update(po for invoice in invoices for po in invoice.po_ids)
            if po_ids:
                resolved = "supplier_invoice"
        if not po_ids:
            return []

        nodes: list[DocumentFlowNode] = []
        for po_id in sorted(po_ids):
            items = known_items.get(po_id) or self.get_purchase_order_items(po_id)
            receipts = self.get_goods_receipts(po_id=po_id, limit=200)
            invoices = self.get_supplier_invoices(po_id=po_id, limit=200)
            requisitions = sorted(
                {
                    (str(item.get("PurchaseRequisition")), str(item.get("PurchaseRequisitionItem") or ""))
                    for item in self.v2.read(
                        service_path("purchase_order_v2"),
                        "A_PurchaseOrderItem",
                        params={
                            "$filter": f"PurchaseOrder eq '{quote(po_id)}'",
                            "$select": "PurchaseOrderItem,PurchaseRequisition,PurchaseRequisitionItem",
                            "$top": 500,
                        },
                    )
                    if item.get("PurchaseRequisition")
                }
            )
            for pr_id, pr_item in requisitions:
                nodes.append(
                    DocumentFlowNode(
                        document_type="purchase_requisition",
                        document_id=pr_id,
                        item_no=pr_item,
                        linked_by="EKPO-BANFN",
                        source_api=service_path("purchase_order_v2"),
                    )
                )
            po_predecessor = requisitions[0][0] if requisitions else ""
            nodes.append(
                DocumentFlowNode(
                    document_type="purchase_order",
                    document_id=po_id,
                    amount=round(sum(i.net_value for i in items), 2),
                    currency=items[0].currency if items else self.settings.sap.currency,
                    linked_by="EKPO-BANFN" if po_predecessor else "",
                    predecessor_id=po_predecessor,
                    source_api=service_path("purchase_order_v2"),
                )
            )
            for gr in receipts:
                nodes.append(
                    DocumentFlowNode(
                        document_type="goods_receipt",
                        document_id=gr.material_document,
                        item_no=gr.item_no,
                        document_date=gr.posting_date,
                        status="reversed" if gr.reversed else "posted",
                        quantity=gr.quantity,
                        unit=gr.unit,
                        linked_by="MSEG-EBELN",
                        predecessor_id=po_id,
                        source_api=service_path("material_document"),
                    )
                )
            for invoice in invoices:
                notes = []
                if include_payments and invoice.status == "paid":
                    notes.append(
                        "Fatura odendi durumunda; released API odeme belgesi numarasini yayinlamadi."
                    )
                nodes.append(
                    DocumentFlowNode(
                        document_type="supplier_invoice",
                        document_id=invoice.invoice_id,
                        document_date=invoice.posting_date,
                        status=invoice.status,
                        amount=invoice.gross_amount,
                        currency=invoice.currency,
                        linked_by="RSEG-EBELN",
                        predecessor_id=po_id,
                        source_api=service_path("supplier_invoice"),
                        notes=notes,
                    )
                )
        return nodes

    # --- Kontrolling --------------------------------------------------------
    def get_project_costs(
        self, *, wbs_element: str | None = None, fiscal_year: int | None = None
    ) -> list[ProjectCost]:
        capability = CAPABILITY_MANIFEST["project_cost"]
        filters: list[str] = []
        if wbs_element:
            filters.append(f"startswith(WBSElement,'{quote(wbs_element)}')")
        if fiscal_year:
            filters.append(f"FiscalYear eq '{_number_literal(fiscal_year)}'")
        try:
            rows = self.v2.read(
                capability.service_path,
                "ProjectCostSet",
                params={"$filter": " and ".join(filters) if filters else None, "$top": 200},
            )
        except SAPError as exc:
            raise SAPNotSupported(
                "project_costs",
                backend=self.name,
                hint=(
                    "S/4HANA'da released bir proje maliyet OData servisi yoktur. Clean Core "
                    "uyumlu bir released CDS/RAP Tier 2 API yayinlanmali (beklenen yol: "
                    f"{capability.service_path}/ProjectCostSet). SAP hatasi: {exc}"
                ),
            ) from exc

        if not rows:
            # `ODataV2Client.read` 404'u yutup bos liste dondurur. Bu, VAR OLAN
            # bir servisin bos sonucu icin dogru davranistir; ama servis hic
            # yayinlanmamissa ayni bos liste "bu projenin maliyeti yok" gibi
            # okunur. Ikisi ayni sey degildir: biri veri yoklugu, digeri
            # yetenek yoklugudur. Ayirmak icin sozlesme dogrulanir.
            try:
                contract = self.metadata_contract("project_cost")
            except SAPError as exc:
                raise SAPNotSupported(
                    "project_costs",
                    backend=self.name,
                    hint=(
                        f"{capability.service_path} okunamadi; servis SICF'te aktif "
                        "olmayabilir. S/4HANA'da released bir proje maliyet OData "
                        "servisi yoktur; Clean Core uyumlu bir Tier 2 API yayinlanmali. "
                        f"SAP hatasi: {exc}"
                    ),
                ) from exc
            if not contract.has_set("ProjectCostSet"):
                raise SAPNotSupported(
                    "project_costs",
                    backend=self.name,
                    hint=(
                        f"{capability.service_path} yayinda ama ProjectCostSet entity "
                        "set'i yok. Kontrati sap_discover_capabilities ile dogrulayin."
                    ),
                )

        return [
            ProjectCost(
                wbs_element=r.get("WBSElement", ""),
                description=r.get("WBSElementDescription", ""),
                plan_cost=float(r.get("PlanCost") or 0),
                actual_cost=float(r.get("ActualCost") or 0),
                commitment=float(r.get("Commitment") or 0),
                currency=r.get("Currency") or self.settings.sap.currency,
                fiscal_year=int(r.get("FiscalYear") or 0),
                completion_pct=float(r.get("CompletionPercent") or 0),
            )
            for r in rows
        ]

    def ping(self) -> dict[str, str]:
        try:
            self.v2.read(
                service_path("product"), "A_Product", params={"$top": 1, "$select": "Product"}
            )
        except SAPError as exc:
            return {"backend": "odata", "status": "error", "detail": str(exc)}
        return {
            "backend": "odata",
            "status": "ok",
            "host": self.connection.base_url,
            "client": self.settings.sap.client,
            "auth": self.connection.describe()["auth"],
        }


# --- Yardimcilar ------------------------------------------------------------
def _nav_rows(row: dict[str, Any], nav: str) -> list[dict[str, Any]]:
    """V4 navigation property'sinden alt kayitlari alir (V2 sarmalayicisina da dayanikli)."""
    value = row.get(nav)
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    if isinstance(value, dict):
        results = value.get("results")
        if isinstance(results, list):
            return [v for v in results if isinstance(v, dict)]
        return [value] if value else []
    return []


def _nav_single(row: dict[str, Any], nav: str) -> dict[str, Any]:
    rows = _nav_rows(row, nav)
    return rows[0] if rows else {}


def _weighted_date(schedule: list[dict[str, Any]], field: str) -> date | None:
    """Coklu schedule line icin miktar-agirlikli teslim tarihi.

    Tek satirdan tarih almak coklu teslimatta yanlis sonuc verir. Miktar
    agirligi, terminin fiilen ne zaman tamamlandigini yansitir.
    """
    weighted: list[tuple[date, float]] = []
    for line in schedule:
        when = parse_odata_datetime(line.get(field))
        if when is None:
            continue
        qty = float(
            line.get("ScheduleLineOrderQuantity") or line.get("ScheduleLineOpenQuantity") or 0
        )
        weighted.append((when, max(qty, 0.0)))
    if not weighted:
        return None
    total_qty = sum(q for _, q in weighted)
    if total_qty <= 0:
        return max(when for when, _ in weighted)
    ordinal = sum(when.toordinal() * q for when, q in weighted) / total_qty
    return date.fromordinal(round(ordinal))


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


def _opt_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _sap_bool(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "x", "yes", "evet"}


def _supplier_invoice_status(
    row: dict[str, Any], payment_block: str
) -> str:
    """Released API durum alanlarini muhafazakar domain durumuna cevirir.

    Kod degerleri surume gore degisebildigi icin bilinmeyen tek harf/rakamlar
    yorumlanmaz. Yalniz acik semantik metinler ve gercek blokaj/reversal
    alanlari karar verir; boylece `2` gibi bir kod yanlislikla "paid" olmaz.
    """
    if _sap_bool(row.get("IsReversal")) or _sap_bool(row.get("IsReversed")):
        return "cancelled"
    if payment_block:
        return "blocked"
    blob = " ".join(
        str(row.get(name) or "").strip().upper()
        for name in (
            "SupplierInvoiceStatus",
            "SupplierInvoicePaymentStatus",
            "SupplierInvoiceApprovalStatus",
        )
    )
    if any(token in blob for token in ("PAID", "CLEARED", "PAYMENT COMPLETED")):
        return "paid"
    if any(token in blob for token in ("PARKED", "HELD", "SAVED AS COMPLETE")):
        return "parked"
    if any(token in blob for token in ("CANCELLED", "CANCELED", "REVERSED")):
        return "cancelled"
    if any(token in blob for token in ("BLOCKED", "PAYMENT BLOCK")):
        return "blocked"
    return "posted"
