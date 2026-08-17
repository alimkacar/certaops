"""SAP backend sozlesmesi - domain portlarina ayrilmis hali.

`SAPBackend` tek ve buyuyen bir ABC olmak yerine domain portlarina ayrilir.
Boylece bir backend yalniz destekledigi portlari gercekten uygular;
desteklemedigini sessizce bos donmek yerine `SAPNotSupported` ile bildirir.

Portlar:
    ProductPort         malzeme ana verisi ve siniflandirma
    PlanningPort        stok, gercek ATP, MRP arz/talep
    ProcurementPort     bilgi kaydi, tedarikci, skor, PR prepare/submit/read, PO
    ProjectFinancePort  WBS plan/fiili/taahhut
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import date
from typing import Any

# SAPError ve SAPNotSupported'in tek tanimi adapter katmanindadir; buradan
# yeniden ihrac edilir ki mevcut `from .base import SAPError` cagrilari calissin.
from ..adapters.sap.errors import SAPError, SAPFault, SAPNotSupported
from .models import (
    AtpResult,
    DocumentFlowNode,
    GoodsReceipt,
    InfoRecord,
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
    Vendor,
    WorkflowStep,
)

__all__ = [
    "PlanningPort",
    "ProcureToPayPort",
    "ProcurementPort",
    "ProductPort",
    "ProjectFinancePort",
    "SAPBackend",
    "SAPError",
    "SAPFault",
    "SAPNotSupported",
]


def effective_unit_price(
    declared: float | None,
    source_price: float,
) -> tuple[float, str]:
    """Onay esigi hesabinda kullanilacak birim fiyat.

    **Guvenlik kurali:** modelin bildirdigi fiyat degeri yalnizca YUKARI
    calisir. Risk skorlamasindaki `max(bildirilen, runtime)` ilkesiyle aynidir.

    Neden gerekli: `approval_policy="threshold"` onay ihtiyacini hazirlanan
    taslagin tutarindan turetir. Model bildirdigi `net_price` ile bu tutari
    serbestce dusurebilseydi, 59.000 EUR'luk bir talep `net_price=1` ile 50
    EUR'a inip 25.000 EUR onay esigini hic onaysiz gecerdi. Bildirilen fiyat
    SAP'in bilgi kaydindan/degerlemesinden DUSUKSE yok sayilir ve uyari uretilir.

    Mesru kullanim korunur: alicinin bildigi ve SAP'tekinden YUKSEK olan
    pazarlik fiyati aynen gecerlidir; yalnizca esigi asagi cekme yolu kapalidir.

    Donus: (kullanilacak_fiyat, uyari_metni). Uyari bos ise ozel bir durum yok.
    """
    source = float(source_price or 0.0)
    if declared is None:
        return source, ""
    declared_value = float(declared)
    if declared_value >= source:
        return declared_value, ""
    return source, (
        f"Bildirilen birim fiyat {declared_value:,.2f} SAP kaynagindaki "
        f"{source:,.2f} degerinin altinda. Onay esigi hesabinda SAP fiyati "
        "kullanildi; bildirilen fiyat tutari asagi cekemez."
    )


class ProductPort(ABC):
    """Malzeme ana verisi ve siniflandirma."""

    name: str = "abstract"

    @property
    def sap_call_count(self) -> int:
        """Bu backend uzerinden SAP'a giden gercek istek sayisi.

        Varsayilan 0'dir: simulator gibi HTTP yapmayan backend'ler icin dogru
        cevap budur. HTTP yapan backend bunu ezer.
        """
        return 0

    @abstractmethod
    def search_materials(
        self,
        query: str = "",
        *,
        material_group: str | None = None,
        plant: str | None = None,
        attribute_filters: dict[str, tuple[float, float]] | None = None,
        limit: int = 20,
    ) -> list[Material]:
        """Serbest metin + malzeme grubu + teknik karakteristik araligina gore arama.

        Uygulama notu: serbest metin **aciklamada** da aranmalidir. Yalniz malzeme
        numarasinda arama yapmak gercek sistemde sonucu bosaltir.
        """

    @abstractmethod
    def get_material(self, material_id: str, *, plant: str | None = None) -> Material | None: ...

    def get_material_classification(
        self, material_id: str, *, class_type: str = "001"
    ) -> MaterialClassification | None:
        """Siniflandirma karakteristikleri. Desteklenmiyorsa acik hata verir."""
        raise SAPNotSupported(
            "material_classification",
            backend=self.name,
            hint="Released classification API/CDS baglanmali (API_CLFN_PRODUCT_SRV).",
        )

    def get_valuation(self, material_id: str, *, plant: str | None = None) -> dict[str, Any] | None:
        """Hareketli ortalama / standart fiyat (MBEW).

        Ayri bir metot cunku fiyat verisi maliyet tahmininin dogrulugunu belirler
        ve okunamadiginda bunun sessizce 0.0'a dusmemesi gerekir.
        """
        raise SAPNotSupported(
            "valuation",
            backend=self.name,
            hint="API_MATERIAL_VALUATION_SRV veya esdeger released CDS baglanmali.",
        )


class PlanningPort(ABC):
    """Stok fotografi, gercek ATP ve MRP arz/talep."""

    name: str = "abstract"

    @abstractmethod
    def get_stock(self, material_ids: list[str], *, plant: str | None = None) -> list[StockLevel]:
        """Stok fotografi. ATP teyidi degildir."""

    def check_atp(
        self,
        material_id: str,
        *,
        quantity: float,
        requested_date: date | None = None,
        plant: str | None = None,
    ) -> AtpResult:
        """Tarih bazli gercek ATP teyidi (API_PRODUCT_AVAILY_INFO)."""
        raise SAPNotSupported(
            "atp_check",
            backend=self.name,
            hint="API_PRODUCT_AVAILY_INFO servisi aktive edilmeli.",
        )

    def get_supply_demand(
        self,
        material_id: str,
        *,
        plant: str | None = None,
        horizon_days: int = 180,
    ) -> list[SupplyDemandItem]:
        """MRP arz/talep elementleri (SupplyDemandItems)."""
        raise SAPNotSupported(
            "mrp_supply_demand",
            backend=self.name,
            hint="API_MRP_MATERIALS_SRV_01 servisi aktive edilmeli.",
        )


class ProcurementPort(ABC):
    """Satinalma: kaynak, tedarikci, talep ve siparis."""

    name: str = "abstract"

    @abstractmethod
    def get_info_records(self, material_id: str, *, plant: str | None = None) -> list[InfoRecord]:
        """Malzeme icin gecerli tedarikci fiyat/teslim kosullari."""

    @abstractmethod
    def get_vendor(self, vendor_id: str) -> Vendor | None: ...

    def get_supplier_score(
        self, vendor_id: str, *, purchasing_org: str | None = None
    ) -> SupplierScore | None:
        """Operasyonel degerlendirme skorlari (A_SUPPLIEROPLSCORESAV_CDS)."""
        raise SAPNotSupported(
            "supplier_score",
            backend=self.name,
            hint="Supplier evaluation CDS view'i (A_SUPPLIEROPLSCORESAV_CDS) baglanmali.",
        )

    # --- PR: prepare / submit / read ---------------------------------------
    @abstractmethod
    def prepare_purchase_requisition(
        self,
        items: list[PurchaseRequisitionItem],
        *,
        header_text: str = "",
        purchase_group: str | None = None,
    ) -> PurchaseRequisitionDraft:
        """Taslak uretir, fiyatlar ve dogrular. **Asla yazmaz.**"""

    @abstractmethod
    def submit_purchase_requisition(
        self,
        draft: PurchaseRequisitionDraft,
        *,
        external_reference: str,
        correlation_id: str = "",
    ) -> PurchaseRequisitionResult:
        """Onaylanmis taslagi SAP'a yazar.

        `external_reference` idempotency anahtarinin SAP tarafinda saklandigi
        degerdir; timeout sonrasi mutabakat bu referansla yapilir.
        """

    @abstractmethod
    def read_purchase_requisition(self, requisition_id: str) -> dict[str, Any] | None:
        """Read-after-write dogrulamasi icin PR'i geri okur."""

    def find_purchase_requisition_by_reference(
        self, external_reference: str
    ) -> tuple[str, dict[str, Any]] | None:
        """Timeout sonrasi: bu referansla olusmus PR var mi?

        Bu okuma, bilinmeyen yazma sonucunu tekrar POST etmeden uzlastirir.
        """
        raise SAPNotSupported(
            "pr_reference_lookup",
            backend=self.name,
            hint="PR okuma servisinde referans alanina gore filtre gerekiyor.",
        )

    @abstractmethod
    def get_purchase_orders(
        self,
        *,
        material_id: str | None = None,
        vendor_id: str | None = None,
        wbs_element: str | None = None,
        only_open: bool = False,
        limit: int = 50,
    ) -> list[PurchaseOrder]: ...


class ProjectFinancePort(ABC):
    """Proje/WBS finansal gorunumu."""

    name: str = "abstract"

    @abstractmethod
    def get_project_costs(
        self, *, wbs_element: str | None = None, fiscal_year: int | None = None
    ) -> list[ProjectCost]: ...


class ProcureToPayPort:
    """PR -> PO -> mal kabul -> fatura -> odeme gorunurlugu.

    Bu port bilerek **salt okunurdur**. PO/GR yazma yetenekleri bu gorunurluk
    portuna eklenmez; ayri sozlesme ve tool'lar gerektirir.

    Diger portlardan farki: hicbir metodu `@abstractmethod` degildir, cunku
    P2P yeteneklerinin tamami **opsiyoneldir**. Bir S/4HANA tenant'inda
    document flow servisi acikken workflow API'si kapali olabilir. Uygulanmayan
    metot sessizce bos donmez, `SAPNotSupported` firlatir; `capabilities()`
    bunu okuyup modele "bu yol yok" bilgisini verir.

    Tasarim kurali: her metot, dondurdugu bagi hangi SAP alanindan kurdugunu
    bildirir. "Muhtemelen bu PO bu PR'dan gelmistir" turu cikarim yapilmaz.
    """

    name: str = "abstract"

    def get_document_flow(
        self,
        document_id: str,
        *,
        document_type: str = "auto",
        include_payments: bool = True,
    ) -> list[DocumentFlowNode]:
        """Bir is nesnesinden baslayarak tum belge zincirini dondurur."""
        raise SAPNotSupported(
            "document_flow",
            backend=self.name,
            hint=(
                "PR/PO/malzeme belgesi/fatura okuma servisleri ve bunlarin referans "
                "alanlari (EKPO-BANFN, MSEG-EBELN, RSEG-EBELN) aktive edilmeli."
            ),
        )

    def get_purchase_order_items(self, po_id: str) -> list[PurchaseOrderItem]:
        """PO kalemleri (EKPO) + kumulatif teslim/fatura miktarlari."""
        raise SAPNotSupported(
            "purchase_order_items",
            backend=self.name,
            hint="API_PURCHASEORDER_PROCESS_SRV/PurchaseOrderItem baglanmali.",
        )

    def get_schedule_lines(self, po_id: str, *, item_no: str = "") -> list[ScheduleLine]:
        """Teslimat plani satirlari (EKET)."""
        raise SAPNotSupported(
            "schedule_lines",
            backend=self.name,
            hint="API_PURCHASEORDER_PROCESS_SRV/PurchaseOrderScheduleLine baglanmali.",
        )

    def get_goods_receipts(
        self, *, po_id: str = "", material_id: str = "", limit: int = 50
    ) -> list[GoodsReceipt]:
        """Mal kabul belgeleri (MKPF/MSEG)."""
        raise SAPNotSupported(
            "goods_receipts",
            backend=self.name,
            hint="API_MATERIAL_DOCUMENT_SRV baglanmali.",
        )

    def get_supplier_invoices(
        self,
        *,
        invoice_id: str = "",
        po_id: str = "",
        vendor_id: str = "",
        only_blocked: bool = False,
        limit: int = 50,
    ) -> list[SupplierInvoice]:
        """Tedarikci faturalari (RBKP) ve odeme/blokaj durumu."""
        raise SAPNotSupported(
            "supplier_invoices",
            backend=self.name,
            hint="API_SUPPLIERINVOICE_PROCESS_SRV baglanmali.",
        )

    def get_workflow_status(
        self, *, object_type: str, object_id: str
    ) -> list[WorkflowStep]:
        """Onay is akisinin hangi adimda ve kimde bekledigi."""
        raise SAPNotSupported(
            "workflow_status",
            backend=self.name,
            hint=(
                "SAP Workflow (SWI) veya Build Process Automation task API'si "
                "baglanmali; yerel onay kaydi is akisi durumu degildir."
            ),
        )


class SAPBackend(
    ProductPort, PlanningPort, ProcurementPort, ProjectFinancePort, ProcureToPayPort
):
    """S/4HANA is nesnelerine erisim arayuzu (tum portlarin birlesimi)."""

    name: str = "abstract"

    def capabilities(self) -> dict[str, Any]:
        """Bu backend'in gercekten destekledigi yetenekler.

        `sap_discover_capabilities` bunu kullanir; boylece model desteklenmeyen
        bir yolu denemek yerine dogrudan dogru araci secer.
        """
        supported: dict[str, bool] = {}
        for capability, method in (
            ("material_search", "search_materials"),
            ("material_classification", "get_material_classification"),
            ("valuation", "get_valuation"),
            ("stock", "get_stock"),
            ("atp_check", "check_atp"),
            ("mrp_supply_demand", "get_supply_demand"),
            ("info_records", "get_info_records"),
            ("supplier_score", "get_supplier_score"),
            ("pr_prepare", "prepare_purchase_requisition"),
            ("pr_submit", "submit_purchase_requisition"),
            ("pr_reference_lookup", "find_purchase_requisition_by_reference"),
            ("purchase_orders", "get_purchase_orders"),
            ("project_costs", "get_project_costs"),
            # Procure-to-pay gorunurlugu
            ("document_flow", "get_document_flow"),
            ("purchase_order_items", "get_purchase_order_items"),
            ("schedule_lines", "get_schedule_lines"),
            ("goods_receipts", "get_goods_receipts"),
            ("supplier_invoices", "get_supplier_invoices"),
            ("workflow_status", "get_workflow_status"),
        ):
            own = getattr(type(self), method, None)
            base_impl = None
            for port in (
                ProductPort,
                PlanningPort,
                ProcurementPort,
                ProjectFinancePort,
                ProcureToPayPort,
            ):
                candidate = getattr(port, method, None)
                if candidate is not None:
                    base_impl = candidate
                    break
            # Port'taki varsayilan (SAPNotSupported firlatan) uygulama hala
            # geciyorsa yetenek gercekten yok demektir.
            supported[capability] = own is not None and own is not base_impl
        return {"backend": self.name, "supported": supported}

    # --- Servis manifesti ---------------------------------------------------
    # Manifest **backend'e aittir**: S/4HANA released API'leri ile ECC Z-Gateway
    # servisleri ayni sozluk degildir. `sap_discover_capabilities` bu iki metot
    # uzerinden konusur ve tek bir manifeste sabitlenmez.
    def service_manifest(self, aliases: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Bu backend'in kullandigi servislerin ozeti."""
        from ..adapters.sap import manifest_summary

        return manifest_summary(list(aliases) if aliases else None)

    def manifest_aliases(self) -> frozenset[str]:
        """Bu backend'in tanidigi servis alias'lari."""
        from ..adapters.sap import CAPABILITY_MANIFEST

        return frozenset(CAPABILITY_MANIFEST)

    def preferred_service_order(self) -> str:
        """Servis secim tercih sirasi - modele hangi yolun once denendigini soyler."""
        return "released OData V4 -> released OData V2/SOAP -> released custom (Tier 2)"

    # --- Saglik kontrolu ----------------------------------------------------
    def ping(self) -> dict[str, str]:
        return {"backend": self.name, "status": "ok"}

    def close(self) -> None:  # pragma: no cover - varsayilan no-op
        return None
