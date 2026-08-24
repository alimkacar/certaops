"""Capability manifest ve $metadata tabanli kontrat dogrulama.

Manifest ve contract testleri su sorulari yanitlar:

  - Hangi SAP servisini, hangi surumle, hangi amacla kullaniyoruz?
  - Servis hedef sistemde aktif mi? Entity set'ler ve alanlar bekledigimiz gibi mi?
  - Released mi, deprecated mi, yoksa kontrollu bir custom API mi?

`parse_metadata` gercek ag olmadan da calisir; kaydedilmis EDMX fixture'lariyla
CI'da kontrat testi yapmayi mumkun kilar.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

log = logging.getLogger(__name__)

# Released public API her zaman tercih edilir.
STATUS_RELEASED = "released"
STATUS_CUSTOM = "custom"
STATUS_DEPRECATED = "deprecated"


@dataclass(frozen=True)
class ServiceCapability:
    alias: str
    service_path: str
    odata_version: str
    purpose: str
    entity_sets: tuple[str, ...]
    status: str = STATUS_RELEASED
    successor: str = ""
    doc_url: str = ""
    # Kontrat testinde varligi dogrulanan kritik alanlar: entity_set -> alanlar
    critical_properties: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "alias": self.alias,
            "service": self.service_path,
            "odata": self.odata_version,
            "purpose": self.purpose,
            "status": self.status,
            "entity_sets": list(self.entity_sets),
        }
        if self.successor:
            payload["successor"] = self.successor
        if self.doc_url:
            payload["doc_url"] = self.doc_url
        return payload


CAPABILITY_MANIFEST: dict[str, ServiceCapability] = {
    "product": ServiceCapability(
        alias="product",
        service_path="/sap/opu/odata/sap/API_PRODUCT_SRV",
        odata_version="v2",
        purpose="Malzeme ana verisi, tesis verisi ve aciklamalar",
        entity_sets=(
            "A_Product", "A_ProductDescription", "A_ProductPlant", "A_ProductPlantMRPArea",
        ),
        doc_url="https://api.sap.com/api/API_PRODUCT_SRV",
        critical_properties={
            "A_Product": ("Product", "ProductType", "ProductGroup", "BaseUnit", "GrossWeight"),
            "A_ProductDescription": ("Product", "Language", "ProductDescription"),
            "A_ProductPlant": (
                "Product",
                "Plant",
                "ProcurementType",
                "MRPResponsible",
            ),
            "A_ProductPlantMRPArea": (
                "Product",
                "Plant",
                "MRPArea",
                "PlannedDeliveryDurationInDays",
            ),
        },
    ),
    "classification": ServiceCapability(
        alias="classification",
        service_path="/sap/opu/odata/sap/API_CLFN_PRODUCT_SRV",
        odata_version="v2",
        purpose="Malzeme siniflandirma karakteristikleri (payload_kg, reach_mm ...)",
        entity_sets=(
            "A_ProductCharcValue",
            "A_ProductClass",
        ),
        doc_url="https://api.sap.com/api/API_CLFN_PRODUCT_SRV",
        critical_properties={
            "A_ProductCharcValue": ("Product", "CharcInternalID", "ClassType", "CharcValue"),
            "A_ProductClass": ("Product", "ClassInternalID", "ClassType"),
        },
    ),
    "valuation": ServiceCapability(
        alias="valuation",
        service_path="/sap/opu/odata/sap/API_PRODUCT_SRV",
        odata_version="v2",
        purpose="Hareketli ortalama / standart fiyat (MBEW) - maliyet dogrulugu icin",
        entity_sets=("A_ProductValuation",),
        doc_url="https://api.sap.com/api/API_PRODUCT_SRV",
        critical_properties={
            "A_ProductValuation": (
                "Product",
                "ValuationArea",
                "MovingAveragePrice",
                "StandardPrice",
                "Currency",
            )
        },
    ),
    "stock": ServiceCapability(
        alias="stock",
        service_path="/sap/opu/odata/sap/API_MATERIAL_STOCK_SRV",
        odata_version="v2",
        purpose="Stok seviyeleri (serbest, kalite, bloke)",
        entity_sets=("A_MatlStkInAcctMod",),
        doc_url="https://api.sap.com/api/API_MATERIAL_STOCK_SRV",
        critical_properties={
            "A_MatlStkInAcctMod": (
                "Material",
                "Plant",
                "StorageLocation",
                "InventoryStockType",
                "MatlWrhsStkQtyInMatlBaseUnit",
            )
        },
    ),
    "availability": ServiceCapability(
        alias="availability",
        service_path="/sap/opu/odata4/sap/api_productavailyinfo/srvd_a2x/sap/productavailabilityinfo/0001",
        odata_version="v4",
        purpose="Gercek ATP: tarih ve miktar bazli teyit (API_PRODUCT_AVAILY_INFO)",
        entity_sets=("ProductAvailabilityInformation", "AvailabilitySituation"),
        doc_url="https://api.sap.com/api/API_PRODUCT_AVAILY_INFO",
        critical_properties={
            "ProductAvailabilityInformation": (
                "Product",
                "Plant",
                "RequestedQuantity",
                "RequestedDeliveryDate",
                "ConfirmedQuantity",
                "ConfirmedDeliveryDate",
            )
        },
    ),
    "mrp": ServiceCapability(
        alias="mrp",
        service_path="/sap/opu/odata/sap/API_MRP_MATERIALS_SRV_01",
        odata_version="v2",
        purpose="Arz-talep elementleri ve shortage aciklamasi (SupplyDemandItems)",
        entity_sets=("SupplyDemandItems", "A_MRPMaterial"),
        doc_url="https://api.sap.com/api/API_MRP_MATERIALS_SRV_01",
        critical_properties={
            "SupplyDemandItems": (
                "Material",
                "MRPPlant",
                "MRPElement",
                "MRPElementOpenQuantity",
                "MRPElementAvailyOrRqmtDate",
            )
        },
    ),
    "inforecord": ServiceCapability(
        alias="inforecord",
        service_path="/sap/opu/odata/sap/API_INFORECORD_PROCESS_SRV",
        odata_version="v2",
        purpose="Satinalma bilgi kaydi: fiyat, teslim suresi, Incoterm",
        entity_sets=("A_PurchasingInfoRecord", "A_PurgInfoRecdOrgPlantData"),
        doc_url="https://api.sap.com/api/API_INFORECORD_PROCESS_SRV",
        critical_properties={
            "A_PurchasingInfoRecord": ("PurchasingInfoRecord", "Material", "Supplier", "IsDeleted"),
            "A_PurgInfoRecdOrgPlantData": (
                "PurchasingOrganization",
                "NetPriceAmount",
                "MaterialPlannedDeliveryDurn",
                "MinimumPurchaseOrderQuantity",
                "IncotermsClassification",
            ),
        },
    ),
    "supplier": ServiceCapability(
        alias="supplier",
        service_path="/sap/opu/odata/sap/API_BUSINESS_PARTNER",
        odata_version="v2",
        purpose="Tedarikci ana verisi",
        entity_sets=("A_Supplier", "A_BusinessPartnerAddress"),
        doc_url="https://api.sap.com/api/API_BUSINESS_PARTNER",
        critical_properties={
            "A_Supplier": ("Supplier", "SupplierName", "PurchasingIsBlocked"),
            "A_BusinessPartnerAddress": ("BusinessPartner", "Country", "CityName"),
        },
    ),
    "supplier_score": ServiceCapability(
        alias="supplier_score",
        service_path="/sap/opu/odata/sap/A_SUPPLIEROPLSCORESAV_CDS",
        odata_version="v2",
        purpose="Tedarikci operasyonel degerlendirme skorlari (fiyat/zaman/miktar/kalite)",
        entity_sets=("A_SupplierOplScoresAV", "A_SupplierOplScoresAVResults"),
        doc_url="https://api.sap.com/api/A_SUPPLIEROPLSCORESAV_CDS",
        critical_properties={
            "A_SupplierOplScoresAVResults": (
                "Supplier",
                "PurchasingOrganization",
                "SupplierOperationalScore",
                "PriceVarianceScore",
                "TimeVarianceScore",
                "QuantityVarianceScore",
                "InspectionLotQualityScore",
                "QualityNotificationScore",
            )
        },
    ),
    "purchase_requisition": ServiceCapability(
        alias="purchase_requisition",
        service_path="/sap/opu/odata4/sap/api_purchaserequisition_2/srvd_a2x/sap/purchaserequisition/0001",
        odata_version="v4",
        purpose="Satinalma talebi olusturma/okuma (ETag destekli V4)",
        entity_sets=("PurchaseRequisition", "PurchaseRequisitionItem"),
        doc_url="https://api.sap.com/api/API_PURCHASEREQUISITION_2",
        critical_properties={
            "PurchaseRequisition": ("PurchaseRequisition", "PurchaseRequisitionType"),
            "PurchaseRequisitionItem": (
                "PurchaseRequisition",
                "PurchaseRequisitionItem",
                "Material",
                "Plant",
                "RequestedQuantity",
                "DeliveryDate",
                "PurchaseRequisitionPrice",
            ),
        },
    ),
    "purchase_requisition_v2": ServiceCapability(
        alias="purchase_requisition_v2",
        service_path="/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV",
        odata_version="v2",
        purpose="Satinalma talebi (V4 yoksa fallback, deep insert)",
        entity_sets=("A_PurchaseRequisitionHeader", "A_PurchaseReqnItem"),
        status=STATUS_DEPRECATED,
        successor="purchase_requisition",
        doc_url="https://api.sap.com/api/API_PURCHASEREQ_PROCESS_SRV",
    ),
    "purchase_order": ServiceCapability(
        alias="purchase_order",
        service_path="/sap/opu/odata4/sap/api_purchaseorder_2/srvd_a2x/sap/purchaseorder/0001",
        odata_version="v4",
        purpose="Satinalma siparisi okuma/degistirme, schedule line ve teyit",
        entity_sets=(
            "PurchaseOrder",
            "PurchaseOrderItem",
            "PurchaseOrderScheduleLine",
            "PurchaseOrderAccountAssignment",
        ),
        doc_url="https://api.sap.com/api/API_PURCHASEORDER_2",
        critical_properties={
            "PurchaseOrder": ("PurchaseOrder", "Supplier", "CreationDate", "DocumentCurrency"),
            "PurchaseOrderItem": (
                "PurchaseOrder",
                "PurchaseOrderItem",
                "Material",
                "Plant",
                "OrderQuantity",
                "NetPriceAmount",
            ),
            "PurchaseOrderAccountAssignment": (
                "PurchaseOrder",
                "PurchaseOrderItem",
                "WBSElementExternalID",
            ),
            "PurchaseOrderScheduleLine": (
                "PurchaseOrder",
                "PurchaseOrderItem",
                "ScheduleLineOrderQuantity",
                "OpenPurchaseOrderQuantity",
                "ScheduleLineDeliveryDate",
                "PurchaseOrderQuantityUnit",
            ),
        },
    ),
    "purchase_order_v2": ServiceCapability(
        alias="purchase_order_v2",
        service_path="/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV",
        odata_version="v2",
        purpose="Satinalma siparisi (V4 yoksa fallback)",
        entity_sets=("A_PurchaseOrder", "A_PurchaseOrderItem", "A_PurOrdScheduleLine"),
        status=STATUS_DEPRECATED,
        successor="purchase_order",
        doc_url="https://api.sap.com/api/API_PURCHASEORDER_PROCESS_SRV",
    ),
    "material_document": ServiceCapability(
        alias="material_document",
        service_path="/sap/opu/odata/sap/API_MATERIAL_DOCUMENT_SRV",
        odata_version="v2",
        purpose="PO referansli mal kabul ve ters kayit hareketleri",
        entity_sets=("A_MaterialDocumentHeader", "A_MaterialDocumentItem"),
        doc_url="https://api.sap.com/api/API_MATERIAL_DOCUMENT_SRV",
        critical_properties={
            "A_MaterialDocumentHeader": (
                "MaterialDocumentYear",
                "MaterialDocument",
                "PostingDate",
            ),
            "A_MaterialDocumentItem": (
                "MaterialDocumentYear",
                "MaterialDocument",
                "MaterialDocumentItem",
                "GoodsMovementType",
                "Material",
                "Plant",
                "QuantityInEntryUnit",
                "EntryUnit",
                "PurchaseOrder",
                "PurchaseOrderItem",
                "GoodsMovementIsCancelled",
            ),
        },
    ),
    "supplier_invoice": ServiceCapability(
        alias="supplier_invoice",
        service_path="/sap/opu/odata/sap/API_SUPPLIERINVOICE_PROCESS_SRV",
        odata_version="v2",
        purpose="Tedarikci faturasi, PO referansi, blokaj ve odeme durumu",
        entity_sets=(
            "A_SupplierInvoice",
            "A_SuplrInvcItemPurOrdRef",
            "A_SupplierInvoiceTax",
        ),
        doc_url="https://api.sap.com/api/API_SUPPLIERINVOICE_PROCESS_SRV",
        critical_properties={
            "A_SupplierInvoice": (
                "SupplierInvoice",
                "FiscalYear",
                "CompanyCode",
                "DocumentDate",
                "PostingDate",
                "InvoicingParty",
                "DocumentCurrency",
                "InvoiceGrossAmount",
                "PaymentBlockingReason",
                "SupplierInvoiceStatus",
                "SupplierInvoicePaymentStatus",
            ),
            "A_SuplrInvcItemPurOrdRef": (
                "SupplierInvoice",
                "FiscalYear",
                "SupplierInvoiceItem",
                "PurchaseOrder",
                "PurchaseOrderItem",
                "QuantityInPurchaseOrderUnit",
                "SupplierInvoiceItemAmount",
            ),
            "A_SupplierInvoiceTax": (
                "SupplierInvoice",
                "FiscalYear",
                "TaxAmount",
            ),
        },
    ),
    "project_cost": ServiceCapability(
        alias="project_cost",
        service_path="/sap/opu/odata/sap/ZAPI_PROJECT_COST_SRV",
        odata_version="v2",
        purpose="WBS plan/fiili/taahhut - released servis yok, kontrollu custom API",
        entity_sets=("ProjectCostSet",),
        status=STATUS_CUSTOM,
        doc_url="https://help.sap.com/docs/ABAP_Cloud/abap-development-tools-user-guide/released-apis",
        critical_properties={
            "ProjectCostSet": (
                "WBSElement",
                "PlanCost",
                "ActualCost",
                "Commitment",
                "FiscalYear",
                "CompletionPercent",
            )
        },
    ),
}


# --- EDMX cozumleme ---------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class NavigationInfo:
    """Bir navigation property'nin sozlesmesi."""

    name: str
    target_type: str
    is_collection: bool = False


@dataclass
class MetadataContract:
    """$metadata belgesinden cikarilan sozlesme.

    Yalniz "alan var mi" sorusunu degil, **yazma govdesinin sekli ne olmali**
    sorusunu da cevaplar: navigation property'ler, anahtar alanlar ve zorunlu
    (`Nullable="false"`) alanlar da cikarilir.

    Bunun onemi: bir POST govdesi, gercek bir yazma denemesi YAPILMADAN
    sozlesmeye karsi denetlenebilir. Okuma yetkisi olan bir sistemde bile
    "bu govde yapisal olarak kabul edilir mi" sorusu cevaplanabilir.
    """

    entity_sets: dict[str, str] = field(default_factory=dict)  # set adi -> tip adi
    entity_types: dict[str, tuple[str, ...]] = field(default_factory=dict)  # tip -> alanlar
    navigations: dict[str, dict[str, NavigationInfo]] = field(default_factory=dict)
    keys: dict[str, tuple[str, ...]] = field(default_factory=dict)
    required: dict[str, tuple[str, ...]] = field(default_factory=dict)
    actions: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()
    version: str = ""

    # --- Tip cozumleme ------------------------------------------------------
    def type_of_set(self, entity_set: str) -> str:
        type_name = self.entity_sets.get(entity_set, "")
        return type_name.rsplit(".", 1)[-1] if type_name else ""

    def properties_of_set(self, entity_set: str) -> tuple[str, ...]:
        short = self.type_of_set(entity_set)
        if not short:
            return ()
        return self.entity_types.get(short, self.entity_types.get(
            self.entity_sets[entity_set], ()))

    def properties_of_type(self, type_name: str) -> tuple[str, ...]:
        return self.entity_types.get(type_name.rsplit(".", 1)[-1], ())

    def key_properties(self, entity_set: str) -> tuple[str, ...]:
        return self.keys.get(self.type_of_set(entity_set), ())

    def required_properties(self, entity_set: str) -> tuple[str, ...]:
        """Anahtar olmayan zorunlu alanlar.

        Anahtarlar cikarilir: create sirasinda belge numarasi SAP tarafindan
        uretilir, gonderilmesi beklenmez.
        """
        short = self.type_of_set(entity_set)
        keys = set(self.keys.get(short, ()))
        return tuple(p for p in self.required.get(short, ()) if p not in keys)

    def has_set(self, entity_set: str) -> bool:
        return entity_set in self.entity_sets

    def missing_properties(self, entity_set: str, expected: Iterable[str]) -> tuple[str, ...]:
        available = set(self.properties_of_set(entity_set))
        if not available:
            return tuple(expected)
        return tuple(sorted(p for p in expected if p not in available))

    # --- Navigation ---------------------------------------------------------
    def navigations_of_type(self, type_name: str) -> dict[str, NavigationInfo]:
        return self.navigations.get(type_name.rsplit(".", 1)[-1], {})

    def navigations_of_set(self, entity_set: str) -> dict[str, NavigationInfo]:
        short = self.type_of_set(entity_set)
        return self.navigations.get(short, {}) if short else {}

    def is_empty(self) -> bool:
        """Sozlesme okunabildi mi?

        Bos olmasi "alan yok" DEGIL, "kanit yok" demektir. Bu ayrimi kaybetmek,
        okunamayan bir $metadata yuzunden calisan bir kurulumu bozmaya yol acar.
        """
        return not self.entity_sets and not self.entity_types


def parse_metadata(edmx_xml: str) -> MetadataContract:
    """EDMX XML'i (V2 veya V4) sozlesme nesnesine cevirir."""
    contract = MetadataContract()
    if not edmx_xml or not edmx_xml.strip():
        return contract
    try:
        root = ElementTree.fromstring(edmx_xml)
    except ElementTree.ParseError as exc:
        log.warning("$metadata cozumlenemedi: %s", exc)
        return contract

    contract.version = root.attrib.get("Version", "")
    actions: list[str] = []
    functions: list[str] = []

    for element in root.iter():
        tag = _local(element.tag)
        if tag == "EntityType":
            name = element.attrib.get("Name", "")
            if not name:
                continue
            props: list[str] = []
            required: list[str] = []
            navigations: dict[str, NavigationInfo] = {}
            keys: list[str] = []
            for child in element:
                child_tag = _local(child.tag)
                child_name = child.attrib.get("Name", "")
                if child_tag == "Property" and child_name:
                    props.append(child_name)
                    # V4 varsayilani Nullable="true"; yalniz ACIKCA false
                    # bildirilen alan zorunlu sayilir.
                    if child.attrib.get("Nullable", "true").lower() == "false":
                        required.append(child_name)
                elif child_tag == "NavigationProperty" and child_name:
                    raw_type = child.attrib.get("Type", "")
                    is_collection = raw_type.startswith("Collection(")
                    target = raw_type[len("Collection("):-1] if is_collection else raw_type
                    navigations[child_name] = NavigationInfo(
                        name=child_name,
                        target_type=target.rsplit(".", 1)[-1],
                        is_collection=is_collection,
                    )
                elif child_tag == "Key":
                    keys.extend(
                        ref.attrib["Name"]
                        for ref in child
                        if _local(ref.tag) == "PropertyRef" and ref.attrib.get("Name")
                    )
            contract.entity_types[name] = tuple(props)
            if navigations:
                contract.navigations[name] = navigations
            if keys:
                contract.keys[name] = tuple(keys)
            if required:
                contract.required[name] = tuple(required)
        elif tag == "EntitySet":
            name = element.attrib.get("Name", "")
            type_name = element.attrib.get("EntityType", "")
            if name:
                contract.entity_sets[name] = type_name
        elif tag == "Action":
            if element.attrib.get("Name"):
                actions.append(element.attrib["Name"])
        elif tag in {"Function", "FunctionImport"}:
            if element.attrib.get("Name"):
                functions.append(element.attrib["Name"])

    contract.actions = tuple(sorted(set(actions)))
    contract.functions = tuple(sorted(set(functions)))
    return contract


# --- Yetenek dogrulama ------------------------------------------------------
@dataclass
class CapabilityCheck:
    alias: str
    available: bool
    status: str
    odata_version: str
    detected_version: str = ""
    missing_entity_sets: tuple[str, ...] = ()
    missing_properties: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    error: str = ""
    latency_ms: float | None = None

    @property
    def contract_ok(self) -> bool:
        return self.available and not self.missing_entity_sets and not self.missing_properties

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "alias": self.alias,
            "available": self.available,
            "contract_ok": self.contract_ok,
            "status": self.status,
            "expected_odata": self.odata_version,
        }
        if self.detected_version:
            payload["metadata_version"] = self.detected_version
        if self.missing_entity_sets:
            payload["missing_entity_sets"] = list(self.missing_entity_sets)
        if self.missing_properties:
            payload["missing_properties"] = {k: list(v) for k, v in self.missing_properties.items()}
        if self.error:
            payload["error"] = self.error
        if self.latency_ms is not None:
            payload["latency_ms"] = round(self.latency_ms, 1)
        return payload


def verify_contract(
    capability: ServiceCapability, contract: MetadataContract
) -> CapabilityCheck:
    """Manifest beklentilerini gercek $metadata'ya karsi dogrular."""
    missing_sets = tuple(s for s in capability.entity_sets if not contract.has_set(s))
    missing_props: dict[str, tuple[str, ...]] = {}
    for entity_set, expected in (capability.critical_properties or {}).items():
        if entity_set in missing_sets:
            continue
        gaps = contract.missing_properties(entity_set, expected)
        if gaps:
            missing_props[entity_set] = gaps
    return CapabilityCheck(
        alias=capability.alias,
        available=bool(contract.entity_sets),
        status=capability.status,
        odata_version=capability.odata_version,
        detected_version=contract.version,
        missing_entity_sets=missing_sets,
        missing_properties=missing_props,
    )


# --- Yazma govdesinin sozlesmeye karsi denetimi ------------------------------
@dataclass
class WriteShapeIssue:
    """Bir yazma govdesindeki tek yapisal sorun."""

    #: unknown_field | missing_required | unknown_navigation | wrong_cardinality
    kind: str
    path: str
    message: str
    severity: str = "error"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass
class WriteShapeReport:
    """Bir POST govdesinin `$metadata`ya karsi denetim sonucu.

    **Ne kanitlar:** govdenin YAPISI hedef sistemin sozlesmesine uyuyor mu -
    alan adlari dogru mu, ic ice yapi (deep insert) dogru navigation
    uzerinden mi kuruluyor, zorunlu alan atlanmis mi.

    **Ne kanitlamaz:** SAP'in calisma zamani is dogrulamalari. Bir govde
    yapisal olarak kusursuz olup yine de "Malzeme X icin tesis Y'de gecerli
    hesap atamasi girin" ile reddedilebilir. Bunu yalniz gercek bir yazma
    gosterir.

    Yani bu rapor **gerekli ama yeterli olmayan** kosulu dogrular. Degeri
    sudur: en sik ve en sessiz hata sinifi (yanlis alan adi / yanlis ic ice
    yapi) yazma yetkisi olmadan yakalanir.
    """

    entity_set: str
    issues: list[WriteShapeIssue] = field(default_factory=list)
    checked_fields: int = 0
    contract_available: bool = True

    @property
    def ok(self) -> bool:
        return not [i for i in self.issues if i.severity == "error"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_set": self.entity_set,
            "structurally_valid": self.ok,
            "contract_available": self.contract_available,
            "checked_fields": self.checked_fields,
            "issues": [i.to_dict() for i in self.issues],
        }


def verify_write_shape(
    contract: MetadataContract,
    entity_set: str,
    payload: Mapping[str, Any],
    *,
    path: str = "",
) -> WriteShapeReport:
    """Bir yazma govdesini `$metadata` sozlesmesine karsi ozyinelemeli denetler.

    Deep insert govdeleri de kapsanir: `_PurchaseRequisitionItem` gibi bir
    anahtar navigation property olarak taninirsa, icindeki her kayit hedef
    tipin alanlarina karsi ayrica denetlenir.
    """
    report = WriteShapeReport(entity_set=entity_set)
    if contract.is_empty() or not contract.has_set(entity_set):
        report.contract_available = False
        return report

    root = path or entity_set
    _verify_level(
        contract,
        type_name=contract.type_of_set(entity_set),
        payload=payload,
        path=root,
        report=report,
        required=contract.required_properties(entity_set),
    )
    return report


def _verify_level(
    contract: MetadataContract,
    *,
    type_name: str,
    payload: Mapping[str, Any],
    path: str,
    report: WriteShapeReport,
    required: Iterable[str] = (),
) -> None:
    properties = set(contract.properties_of_type(type_name))
    navigations = contract.navigations_of_type(type_name)
    if not properties and not navigations:
        # Tip cozumlenemedi: sessizce "hepsi dogru" demek yaniltici olur.
        report.issues.append(
            WriteShapeIssue(
                kind="unknown_type",
                path=path,
                message=f"'{type_name}' tipi $metadata icinde bulunamadi.",
                severity="warning",
            )
        )
        return

    for key, value in payload.items():
        report.checked_fields += 1
        child_path = f"{path}.{key}"
        if key in properties:
            continue
        navigation = navigations.get(key)
        if navigation is None:
            report.issues.append(
                WriteShapeIssue(
                    kind="unknown_field",
                    path=child_path,
                    message=(
                        f"'{key}' bu tipte ne alan ne navigation property. "
                        "SAP bu govdeyi reddeder ya da alani sessizce yok sayar."
                    ),
                )
            )
            continue
        # Navigation: kardinalite ve ic yapi denetlenir.
        children = value if isinstance(value, list) else [value]
        if navigation.is_collection and not isinstance(value, list):
            report.issues.append(
                WriteShapeIssue(
                    kind="wrong_cardinality",
                    path=child_path,
                    message=f"'{key}' bir koleksiyon; liste gonderilmeli.",
                )
            )
        if not navigation.is_collection and isinstance(value, list):
            report.issues.append(
                WriteShapeIssue(
                    kind="wrong_cardinality",
                    path=child_path,
                    message=f"'{key}' tekil; liste degil nesne gonderilmeli.",
                )
            )
        for index, child in enumerate(children):
            if not isinstance(child, Mapping):
                continue
            _verify_level(
                contract,
                type_name=navigation.target_type,
                payload=child,
                path=f"{child_path}[{index}]" if navigation.is_collection else child_path,
                report=report,
            )

    for name in required:
        if name not in payload:
            report.issues.append(
                WriteShapeIssue(
                    kind="missing_required",
                    path=f"{path}.{name}",
                    message=f"'{name}' $metadata'da zorunlu (Nullable=false) ama govdede yok.",
                )
            )


def account_assignment_shape(
    contract: MetadataContract, item_entity_set: str
) -> str:
    """Hesap atamasi bu serviste NEREYE yazilir?

    Uc olasilik vardir ve hangisi oldugu tahminle degil sozlesmeyle belirlenir:

      ``child``  Ayri bir alt entity (`_PurchaseReqnAcctAssgmt` /
                 `to_PurchaseReqnAcctAssgmt`). Released S/4HANA PR API'sinde
                 beklenen sekil budur.
      ``inline`` WBS/masraf merkezi dogrudan kalemin uzerinde.
      ``unknown`` Sozlesme okunamadi; guvenli varsayilan uygulanir.

    Bu ayrimin bedeli yuksektir: yanlis secim ya 400 doner ya da - daha
    kotusu - hesap atamasi OLMAYAN bir belge acar. Ikincisi sessizdir ve
    proje maliyeti yanlis yere duser.
    """
    if contract.is_empty() or not contract.has_set(item_entity_set):
        return "unknown"
    navigations = contract.navigations_of_set(item_entity_set)
    for name in navigations:
        if "acctassgmt" in name.lower() or "accountassignment" in name.lower():
            return "child"
    properties = set(contract.properties_of_set(item_entity_set))
    if {"WBSElement", "CostCenter"} & properties:
        return "inline"
    return "unknown"


def manifest_summary(aliases: Iterable[str] | None = None) -> list[dict[str, Any]]:
    keys = list(aliases) if aliases else list(CAPABILITY_MANIFEST)
    return [
        CAPABILITY_MANIFEST[key].to_dict() for key in keys if key in CAPABILITY_MANIFEST
    ]


def preferred_alias(*candidates: str) -> str:
    """Tercih sirasi: released V4 -> released V2 -> custom.

    Adapter tercih siralamasini kodda tek yerde tutar.
    """
    ranked = sorted(
        (CAPABILITY_MANIFEST[c] for c in candidates if c in CAPABILITY_MANIFEST),
        key=lambda cap: (
            {STATUS_RELEASED: 0, STATUS_CUSTOM: 1, STATUS_DEPRECATED: 2}[cap.status],
            {"v4": 0, "v2": 1}.get(cap.odata_version, 2),
        ),
    )
    return ranked[0].alias if ranked else ""
