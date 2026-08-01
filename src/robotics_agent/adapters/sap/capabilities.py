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
        entity_sets=("A_Product", "A_ProductDescription", "A_ProductPlant"),
        doc_url="https://api.sap.com/api/API_PRODUCT_SRV",
        critical_properties={
            "A_Product": ("Product", "ProductType", "ProductGroup", "BaseUnit", "GrossWeight"),
            "A_ProductDescription": ("Product", "Language", "ProductDescription"),
            "A_ProductPlant": (
                "Product",
                "Plant",
                "ProcurementType",
                "PlndDelryDurnInDays",
                "MRPController",
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
            "A_ClfnCharacteristicForKeyDate",
        ),
        doc_url="https://api.sap.com/api/API_CLFN_PRODUCT_SRV",
        critical_properties={
            "A_ProductCharcValue": ("Product", "Characteristic", "CharcValue"),
            "A_ProductClass": ("Product", "ClassInternalID", "ClassTypeInternalID"),
        },
    ),
    "valuation": ServiceCapability(
        alias="valuation",
        service_path="/sap/opu/odata/sap/API_MATERIAL_VALUATION_SRV",
        odata_version="v2",
        purpose="Hareketli ortalama / standart fiyat (MBEW) - maliyet dogrulugu icin",
        entity_sets=("A_MaterialValuation",),
        doc_url="https://api.sap.com/api/API_MATERIAL_VALUATION_SRV",
        critical_properties={
            "A_MaterialValuation": (
                "Material",
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
        entity_sets=("SupplyDemandItems", "MRPMaterials"),
        doc_url="https://api.sap.com/api/API_MRP_MATERIALS_SRV_01",
        critical_properties={
            "SupplyDemandItems": (
                "Material",
                "MRPPlant",
                "MRPElement",
                "MRPElementOpenQuantity",
                "MRPAvailabilityDate",
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
        entity_sets=("A_Supplier",),
        doc_url="https://api.sap.com/api/API_BUSINESS_PARTNER",
        critical_properties={
            "A_Supplier": ("Supplier", "SupplierName", "Country", "PurchasingIsBlockedForSupplier")
        },
    ),
    "supplier_score": ServiceCapability(
        alias="supplier_score",
        service_path="/sap/opu/odata/sap/A_SUPPLIEROPLSCORESAV_CDS",
        odata_version="v2",
        purpose="Tedarikci operasyonel degerlendirme skorlari (fiyat/zaman/miktar/kalite)",
        entity_sets=("A_SupplierOplScoresAv",),
        doc_url="https://api.sap.com/api/A_SUPPLIEROPLSCORESAV_CDS",
        critical_properties={
            "A_SupplierOplScoresAv": (
                "Supplier",
                "PurchasingOrganization",
                "OverallScore",
                "PriceScore",
                "DeliveryScore",
                "QuantityScore",
                "QualityScore",
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
        entity_sets=("PurchaseOrder", "PurchaseOrderItem", "PurchaseOrderScheduleLine"),
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
                "WBSElement",
            ),
            "PurchaseOrderScheduleLine": (
                "PurchaseOrder",
                "PurchaseOrderItem",
                "ScheduleLineOrderQuantity",
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


@dataclass
class MetadataContract:
    """$metadata belgesinden cikarilan sozlesme."""

    entity_sets: dict[str, str] = field(default_factory=dict)  # set adi -> tip adi
    entity_types: dict[str, tuple[str, ...]] = field(default_factory=dict)  # tip -> alanlar
    actions: tuple[str, ...] = ()
    functions: tuple[str, ...] = ()
    version: str = ""

    def properties_of_set(self, entity_set: str) -> tuple[str, ...]:
        type_name = self.entity_sets.get(entity_set, "")
        if not type_name:
            return ()
        short = type_name.rsplit(".", 1)[-1]
        return self.entity_types.get(short, self.entity_types.get(type_name, ()))

    def has_set(self, entity_set: str) -> bool:
        return entity_set in self.entity_sets

    def missing_properties(self, entity_set: str, expected: Iterable[str]) -> tuple[str, ...]:
        available = set(self.properties_of_set(entity_set))
        if not available:
            return tuple(expected)
        return tuple(sorted(p for p in expected if p not in available))


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
            props = tuple(
                child.attrib.get("Name", "")
                for child in element
                if _local(child.tag) == "Property" and child.attrib.get("Name")
            )
            if name:
                contract.entity_types[name] = props
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
