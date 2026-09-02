"""SAP is nesnelerinin tip guvenli temsilleri.

Alan adlari SAP tarafindaki teknik alanlara (MARA-MATNR, EKKO-EBELN vb.) esittir;
her modelde SAP karsiligi yorum olarak belirtilmistir.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field


class Material(BaseModel):
    """Malzeme ana verisi (MARA + MAKT + MARC)."""

    material_id: str = Field(description="MARA-MATNR")
    description: str = Field(description="MAKT-MAKTX")
    material_type: Literal["FERT", "HALB", "ROH", "HIBE", "DIEN"] = Field(
        default="ROH", description="MARA-MTART: mamul / yari mamul / hammadde / ticari mal / hizmet"
    )
    material_group: str = Field(default="", description="MARA-MATKL")
    base_unit: str = Field(default="ST", description="MARA-MEINS")
    gross_weight_kg: float | None = Field(default=None, description="MARA-BRGEW")
    procurement_type: Literal["E", "F", "X"] = Field(
        default="F", description="MARC-BESKZ: E=uretim, F=disaridan tedarik, X=her ikisi"
    )
    planned_delivery_days: int = Field(default=0, description="MARC-PLIFZ")
    moving_avg_price: float = Field(default=0.0, description="MBEW-VERPR")
    currency: str = Field(default="EUR", description="MBEW-WAERS")
    price_unit: int = Field(default=1, description="MBEW-PEINH")
    min_order_qty: float = Field(default=1.0, description="MARC-BSTMI")
    lot_size_key: str = Field(default="EX", description="MARC-DISLS")
    mrp_controller: str = Field(default="", description="MARC-DISPO")
    abc_indicator: str = Field(default="", description="MARC-MAABC")
    plant: str = Field(default="", description="MARC-WERKS")
    # Teknik alan verisi - SAP tarafinda siniflandirma (CAWN/AUSP) karsiligi
    attributes: dict[str, Any] = Field(
        default_factory=dict, description="Siniflandirma karakteristikleri (payload_kg, reach_mm...)"
    )


def _normalise_characteristic(name: str) -> str:
    """Karakteristik adini karsilastirilabilir hale getirir.

    `REACH_MM`, `reach mm` ve `Reach-MM` ayni karakteristiktir; SAP'in
    yazim kurali ile modelin yazdigi ad arasindaki fark bir filtre
    hatasina donusmemeli.
    """
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


class MaterialClassification(BaseModel):
    """Malzeme siniflandirmasi (CLFN: KLAH/CABN/CAWN/AUSP).

    Teknik karakteristikler burada yasar. Birim ve donusum bilerek modelde:
    "1.8 m reach" ile "1800 mm reach" ayni kisiti ifade eder ve karsilastirma
    yapilmadan once normalize edilmelidir.
    """

    material_id: str
    class_type: str = Field(default="001", description="KLAH-KLART: 001 malzeme sinifi")
    class_name: str = Field(default="", description="KLAH-CLASS")
    characteristics: dict[str, Any] = Field(
        default_factory=dict, description="Karakteristik adi -> deger (AUSP)"
    )
    units: dict[str, str] = Field(
        default_factory=dict, description="Karakteristik adi -> birim (CABN-MSEHI)"
    )
    source: str = Field(default="", description="Verinin geldigi API/CDS")

    def numeric(self, name: str) -> float | None:
        """Karakteristigin sayisal degeri; ad eslesmesi buyuk/kucuk harf duyarsizdir.

        SAP karakteristik adlari (CABN-ATNAM) her zaman BUYUK harf ve
        genellikle alt cizgilidir: `REACH_MM`, `ANTRIEBSMOMENT_NM`. Model ise
        kullanicinin yazdigi sekilde `reach_mm` gonderir. Tam eslesme
        arayan eski hali bu durumda `None` donuyordu ve sonuc **sessiz sifir
        eslesme** oluyordu: filtre calismadi degil, "hicbir malzeme uymadi"
        gibi gorunuyordu. Ad normalize edilerek karsilastirilir.
        """
        value = self.characteristics.get(name)
        if value is None:
            value = self._lookup_relaxed(name)
        if isinstance(value, bool) or value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _lookup_relaxed(self, name: str) -> Any:
        """Buyuk/kucuk harf ve ayrac (`_`, `-`, bosluk) farkini yok sayan arama."""
        wanted = _normalise_characteristic(name)
        if not wanted:
            return None
        for key, value in self.characteristics.items():
            if _normalise_characteristic(key) == wanted:
                return value
        return None


class StockLevel(BaseModel):
    """Stok durumu (MARD / MSKA / EBAN acik miktarlar).

    Dikkat: bu model **stok fotografidir**, ATP degildir. `unreserved_qty`
    "serbest stok eksi rezervasyon"dur ve tarih bazli bir teyit yerine gecmez.
    """

    material_id: str
    plant: str = Field(description="MARD-WERKS")
    storage_location: str = Field(default="0001", description="MARD-LGORT")
    unrestricted_qty: float = Field(default=0.0, description="MARD-LABST: serbest kullanilabilir")
    quality_inspection_qty: float = Field(default=0.0, description="MARD-INSME: kalite kontrolde")
    blocked_qty: float = Field(default=0.0, description="MARD-SPEME: bloke")
    reserved_qty: float = Field(default=0.0, description="RESB uzerinden rezerve")
    on_order_qty: float = Field(default=0.0, description="EKPO acik siparis (teslim edilmemis) miktari")
    safety_stock: float = Field(default=0.0, description="MARC-EISBE")
    unit: str = "ST"

    @property
    def unreserved_qty(self) -> float:
        """Serbest stok eksi rezervasyon. ATP teyidi degildir."""
        return round(self.unrestricted_qty - self.reserved_qty, 3)

    @property
    def available_qty(self) -> float:
        """Geriye donuk uyumluluk icin `unreserved_qty` takma adi."""
        return self.unreserved_qty

    @property
    def below_safety_stock(self) -> bool:
        return self.unreserved_qty < self.safety_stock


class SupplyDemandItem(BaseModel):
    """MRP arz/talep elementi (API_MRP_MATERIALS_SRV_01/SupplyDemandItems)."""

    material_id: str
    plant: str
    mrp_element: str = Field(description="MRP element tipi: WB stok, BE acik PO, VC rezervasyon...")
    element_id: str = Field(default="", description="Belge numarasi (PO, rezervasyon, plan siparisi)")
    availability_date: date | None = None
    quantity: float = Field(default=0.0, description="Isaretli miktar: + arz, - talep")
    unit: str = "ST"
    description: str = ""
    wbs_element: str | None = None

    @property
    def is_supply(self) -> bool:
        return self.quantity > 0

    @property
    def is_demand(self) -> bool:
        return self.quantity < 0


class SupplierScore(BaseModel):
    """Tedarikci operasyonel degerlendirme skoru (A_SUPPLIEROPLSCORESAV_CDS).

    `estimated_fields`: SAP'ta gerceklesen veri bulunamadigi icin tahmin/fallback
    ile dolduruldugunu bildirir. Fallback alanlari acikca isaretlenmelidir;
    aksi halde karar yanlis kaynaga dayanir.
    """

    vendor_id: str
    purchasing_org: str = ""
    overall_score: float | None = None
    price_score: float | None = None
    delivery_score: float | None = None
    quantity_score: float | None = None
    quality_score: float | None = None
    service_score: float | None = None
    on_time_delivery_pct: float | None = None
    quality_ppm: int | None = None
    evaluated_period: str = ""
    source_api: str = ""
    estimated_fields: list[str] = Field(default_factory=list)

    @property
    def has_real_data(self) -> bool:
        return self.overall_score is not None and not self.estimated_fields

    def to_summary(self) -> dict[str, Any]:
        payload = {
            "vendor_id": self.vendor_id,
            "overall_score": self.overall_score,
            "price": self.price_score,
            "delivery": self.delivery_score,
            "quantity": self.quantity_score,
            "quality": self.quality_score,
            "service": self.service_score,
            "on_time_delivery_pct": self.on_time_delivery_pct,
            "quality_ppm": self.quality_ppm,
            "period": self.evaluated_period,
            "source_api": self.source_api,
        }
        if self.estimated_fields:
            payload["estimated"] = True
            payload["estimated_fields"] = list(self.estimated_fields)
        return {k: v for k, v in payload.items() if v is not None}


class InfoRecord(BaseModel):
    """Satinalma bilgi kaydi (EINA + EINE) - malzeme/tedarikci fiyat ve teslim kosullari."""

    material_id: str
    vendor_id: str = Field(description="EINA-LIFNR")
    vendor_name: str = ""
    net_price: float = Field(description="EINE-NETPR")
    currency: str = "EUR"
    price_unit: int = Field(default=1, description="EINE-PEINH")
    min_order_qty: float = Field(default=1.0, description="EINE-MINBM")
    planned_delivery_days: int = Field(default=14, description="EINE-APLFZ")
    incoterms: str = Field(default="DAP", description="EINE-INCO1")
    payment_terms: str = Field(default="NT30", description="EINE-ZTERM")
    valid_to: date | None = None
    # Kademeli fiyat (Scale prices - KONM): {miktar: birim fiyat}
    scale_prices: dict[str, float] = Field(default_factory=dict)

    def price_for_qty(self, qty: float) -> float:
        """Kademeli fiyat tablosundan verilen miktar icin gecerli birim fiyati dondurur."""
        applicable = self.net_price
        for scale_qty_raw, scale_price in sorted(
            self.scale_prices.items(), key=lambda kv: float(kv[0])
        ):
            if qty >= float(scale_qty_raw):
                applicable = scale_price
        return applicable


class Vendor(BaseModel):
    """Tedarikci ana verisi (LFA1) + tedarikci degerlendirme (ME6H benzeri) skorlari."""

    vendor_id: str = Field(description="LFA1-LIFNR")
    name: str = Field(description="LFA1-NAME1")
    country: str = Field(default="", description="LFA1-LAND1")
    city: str = ""
    blocked: bool = Field(default=False, description="LFA1-SPERM")
    # Tedarikci degerlendirme kriterleri (0-100).
    #
    # `None` = SAP'ta OLCUM YOK. Sifir degil: sifir "olculdu ve sifir cikti"
    # demektir ve tam ters karari verdirir - 0 ppm kusursuz kalite, %0 zamaninda
    # teslim ise tam guvenilmezlik anlamina gelir. Degerlendirme CDS'i kapali
    # bir sistemde ikisini de sifir yazmak, olculmemis bir tedarikciyi ya
    # kusursuz ya felaket gostermek demekti.
    on_time_delivery_pct: float | None = None
    quality_ppm: int | None = Field(default=None, description="Milyonda hatali parca")
    price_competitiveness: float | None = Field(
        default=None, description="0-100, yuksek = daha rekabetci"
    )
    responsiveness: float | None = None
    certifications: list[str] = Field(default_factory=list)
    single_source_risk: bool = False
    avg_lead_time_days: int = 0

    @property
    def unmeasured_fields(self) -> list[str]:
        """SAP'ta olcumu bulunmayan degerlendirme alanlari."""
        eksik = []
        if self.on_time_delivery_pct is None:
            eksik.append("on_time_delivery_pct")
        if self.quality_ppm is None:
            eksik.append("quality_ppm")
        if self.price_competitiveness is None:
            eksik.append("price_competitiveness")
        if self.responsiveness is None:
            eksik.append("responsiveness")
        return eksik

    def score(self) -> float | None:
        """Agirlikli tedarikci skoru (0-100), olcum yoksa `None`.

        Eksik kriteri sifir sayip skoru yine de uretmek, olculmemis bir
        tedarikciyi olculmus ve kotu cikmis gibi siralardi. Kismi veriden
        skor uretilmez; cagiran taraf `unmeasured_fields` ile eksigi bildirir.
        """
        if self.blocked:
            return 0.0
        if self.unmeasured_fields:
            return None
        quality_score = max(0.0, 100.0 - (self.quality_ppm or 0) / 50.0)
        raw = (
            0.35 * (self.on_time_delivery_pct or 0.0)
            + 0.30 * quality_score
            + 0.20 * (self.price_competitiveness or 0.0)
            + 0.15 * (self.responsiveness or 0.0)
        )
        if self.single_source_risk:
            raw -= 5.0
        return round(max(0.0, min(100.0, raw)), 1)


class PurchaseRequisitionItem(BaseModel):
    """Satinalma talebi kalemi (EBAN)."""

    material_id: str
    quantity: float
    unit: str = "ST"
    delivery_date: date | None = Field(default=None, description="EBAN-LFDAT")
    plant: str = ""
    preferred_vendor: str | None = Field(default=None, description="EBAN-FLIEF sabit tedarikci")
    net_price: float | None = None
    currency: str = "EUR"
    cost_center: str | None = Field(default=None, description="EBKN-KOSTL")
    wbs_element: str | None = Field(default=None, description="EBKN-PS_PSP_PNR")
    item_text: str = ""


class ValidationFinding(BaseModel):
    """Yazma oncesi deterministik dogrulama bulgusu."""

    severity: Literal["error", "warning", "info"] = "warning"
    field: str = ""
    item_no: int | None = None
    message: str

    @property
    def blocking(self) -> bool:
        return self.severity == "error"


class PurchaseRequisitionDraft(BaseModel):
    """PR taslagi: hicbir kosulda SAP'a yazmaz.

    `payload` gonderim govdesidir ve onay hash'i bunun uzerinden hesaplanir.
    `findings` MOQ/termin/fiyat dogrulamasinin sonucudur; `blocking_findings`
    varsa submit reddedilir.
    """

    draft_id: str = ""
    items: list[dict[str, Any]] = Field(default_factory=list)
    header_text: str = ""
    purchase_group: str = ""
    purchasing_org: str = ""
    plant: str = ""
    total_value: float = 0.0
    currency: str = "EUR"
    payload: dict[str, Any] = Field(default_factory=dict)
    findings: list[ValidationFinding] = Field(default_factory=list)
    diff: list[dict[str, Any]] = Field(default_factory=list)
    source_api: str = ""
    requires_human_approval: bool = False

    @property
    def blocking_findings(self) -> list[ValidationFinding]:
        return [f for f in self.findings if f.blocking]

    @property
    def is_submittable(self) -> bool:
        return bool(self.items) and not self.blocking_findings


class PurchaseRequisitionResult(BaseModel):
    """PR olusturma sonucu."""

    requisition_id: str | None = None
    created: bool = False
    dry_run: bool = True
    requires_human_approval: bool = False
    total_value: float = 0.0
    currency: str = "EUR"
    items: list[dict[str, Any]] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    external_reference: str = Field(
        default="", description="Idempotency anahtarinin SAP'ta saklandigi referans"
    )
    etag: str = ""


class PurchaseOrder(BaseModel):
    """Satinalma siparisi basligi + ozet kalem bilgisi (EKKO + EKPO + EKET)."""

    po_id: str = Field(description="EKKO-EBELN")
    vendor_id: str = Field(description="EKKO-LIFNR")
    vendor_name: str = ""
    created_on: date | None = Field(default=None, description="EKKO-AEDAT")
    currency: str = "EUR"
    net_value: float = 0.0
    status: Literal["open", "partially_delivered", "delivered", "invoiced", "closed"] = "open"
    material_id: str = ""
    description: str = ""
    quantity: float = 0.0
    delivered_qty: float = 0.0
    confirmed_delivery_date: date | None = Field(default=None, description="EKET-EINDT")
    requested_delivery_date: date | None = None
    wbs_element: str | None = None

    @property
    def open_qty(self) -> float:
        return round(self.quantity - self.delivered_qty, 3)


# ---------------------------------------------------------------------------
# Procure-to-pay gorunurlugu
# ---------------------------------------------------------------------------
class PurchaseOrderItem(BaseModel):
    """Satinalma siparisi kalemi (EKPO) + teslimat/fatura durumu."""

    po_id: str = Field(description="EKKO-EBELN")
    item_no: str = Field(description="EKPO-EBELP")
    material_id: str = Field(default="", description="EKPO-MATNR")
    description: str = Field(default="", description="EKPO-TXZ01")
    plant: str = Field(default="", description="EKPO-WERKS")
    quantity: float = Field(default=0.0, description="EKPO-MENGE")
    unit: str = "ST"
    net_price: float = Field(default=0.0, description="EKPO-NETPR")
    net_value: float = Field(default=0.0, description="EKPO-NETWR")
    currency: str = "EUR"
    delivered_qty: float = Field(default=0.0, description="Toplam mal kabul miktari")
    invoiced_qty: float = Field(default=0.0, description="Fatura girisi yapilan miktar")
    goods_receipt_required: bool = Field(default=True, description="EKPO-WEPOS")
    invoice_receipt_required: bool = Field(default=True, description="EKPO-REPOS")
    deletion_indicator: bool = Field(default=False, description="EKPO-LOEKZ")
    wbs_element: str | None = None
    account_assignment: str = Field(default="", description="EKPO-KNTTP")

    @property
    def open_qty(self) -> float:
        return round(max(0.0, self.quantity - self.delivered_qty), 3)

    @property
    def uninvoiced_qty(self) -> float:
        """Teslim alinmis ama henuz faturalanmamis miktar (GR/IR farki)."""
        return round(max(0.0, self.delivered_qty - self.invoiced_qty), 3)

    @property
    def fully_delivered(self) -> bool:
        return self.open_qty <= 0


class ScheduleLine(BaseModel):
    """Teslimat plani satiri (EKET): hangi tarihte ne kadar bekleniyor."""

    po_id: str
    item_no: str
    schedule_line: str = Field(default="0001", description="EKET-ETENR")
    requested_date: date | None = Field(default=None, description="EKET-EINDT talep")
    confirmed_date: date | None = Field(default=None, description="Tedarikci teyidi")
    quantity: float = 0.0
    delivered_qty: float = 0.0
    unit: str = "ST"

    @property
    def delay_days(self) -> int:
        if not (self.requested_date and self.confirmed_date):
            return 0
        return max(0, (self.confirmed_date - self.requested_date).days)


class DocumentFlowNode(BaseModel):
    """Belge zincirinin tek dugumu (PR -> PO -> GR -> fatura -> odeme).

    `linked_by` alani **uydurma bag kurulmadiginin kanitidir**: her dugum,
    kendisini onceki belgeye baglayan SAP alanini tasir (or. `EKPO-BANFN`).
    Bag kaynagi gosterilemiyorsa dugum zincire eklenmez; belge baglari
    tahminle uretilmez.
    """

    document_type: Literal[
        "purchase_requisition", "purchase_order", "goods_receipt",
        "supplier_invoice", "payment",
    ]
    document_id: str
    item_no: str = ""
    document_date: date | None = None
    status: str = ""
    quantity: float | None = None
    unit: str = ""
    amount: float | None = None
    currency: str = ""
    # Bu dugumu bir onceki belgeye baglayan SAP alani.
    linked_by: str = ""
    predecessor_id: str = ""
    source_api: str = ""
    notes: list[str] = Field(default_factory=list)


class InvoiceBlock(BaseModel):
    """Fatura blokaj nedeni (RSEG/RBKP tolerans kontrolleri).

    SAP blokaj anahtarlari deterministiktir; model bunlari yorumlamaz, kod
    cozer. `tolerance_key` gercek SAP tolerans anahtaridir (OMR6).
    """

    invoice_id: str
    item_no: str = ""
    # "unknown" bilerek vardir. Kaynak API blokajin NEDENINI yayinlamiyorsa
    # bir neden secmek uydurmaktir: "manual" demek, otomatik tolerans blokajini
    # elle konmus gibi gostererek kullaniciyi YANLIS islemin ustune yollar
    # (MRBR ile serbest birakma yerine elle blokaj kaldirma).
    block_reason: Literal[
        "price", "quantity", "date", "order_price_unit", "quality", "manual", "amount", "unknown"
    ] = "unknown"
    tolerance_key: str = Field(default="", description="OMR6 tolerans anahtari (PP, DQ, ST...)")
    # RBKP-ZLSPR odeme blokaj anahtari. OMR6 tolerans anahtari ILE AYNI SEY
    # DEGILDIR: farkli kod listeleri, farkli anlamlar. Ayni alanda tasimak
    # 'R' (fatura dogrulama blokaji) degerini bir tolerans anahtari gibi
    # gosteriyordu.
    payment_block_key: str = Field(default="", description="RBKP-ZLSPR odeme blokaj anahtari")
    expected_value: float | None = None
    actual_value: float | None = None
    variance_abs: float | None = None
    variance_pct: float | None = None
    tolerance_limit_abs: float | None = None
    tolerance_limit_pct: float | None = None
    currency: str = ""
    unit: str = ""
    po_id: str = ""
    po_item: str = ""
    description: str = ""


class SupplierInvoice(BaseModel):
    """Tedarikci faturasi (RBKP) + muhasebe/odeme durumu."""

    invoice_id: str = Field(description="RBKP-BELNR")
    fiscal_year: int = 0
    vendor_id: str = Field(default="", description="RBKP-LIFNR")
    vendor_name: str = ""
    company_code: str = ""
    invoice_date: date | None = Field(default=None, description="RBKP-BLDAT")
    posting_date: date | None = Field(default=None, description="RBKP-BUDAT")
    due_date: date | None = None
    gross_amount: float = 0.0
    net_amount: float = 0.0
    tax_amount: float = 0.0
    currency: str = "EUR"
    status: Literal["parked", "posted", "blocked", "paid", "cancelled"] = "posted"
    payment_block: str = Field(default="", description="RBKP-ZLSPR odeme blokaj anahtari")
    payment_terms: str = ""
    paid_on: date | None = None
    accounting_document: str = Field(default="", description="Muhasebe belgesi (BKPF-BELNR)")
    po_ids: list[str] = Field(default_factory=list, description="Referans verilen PO'lar")
    po_item_quantities: dict[str, float] = Field(
        default_factory=dict,
        description="PO/kalem bazinda faturalanan miktar; anahtar EBELN/EBELP",
    )
    blocks: list[InvoiceBlock] = Field(default_factory=list)
    source_api: str = ""

    @property
    def is_blocked(self) -> bool:
        return self.status == "blocked" or bool(self.payment_block) or bool(self.blocks)

    def days_overdue(self, *, today: date | None = None) -> int:
        # Iptal edilmis veya henuz park halinde olan belge odeme acigi
        # degildir. Bunlari gecikmis saymak, ozellikle Hub sandbox'taki
        # cancelled ornekleri aktif borc gibi gostermisti.
        if self.due_date is None or self.status in {"paid", "cancelled", "parked"}:
            return 0
        return max(0, ((today or date.today()) - self.due_date).days)


class GoodsReceipt(BaseModel):
    """Mal kabul / malzeme belgesi (MKPF + MSEG)."""

    material_document: str = Field(description="MKPF-MBLNR")
    document_year: int = 0
    item_no: str = Field(default="0001", description="MSEG-ZEILE")
    posting_date: date | None = Field(default=None, description="MKPF-BUDAT")
    movement_type: str = Field(default="101", description="MSEG-BWART")
    material_id: str = ""
    plant: str = ""
    quantity: float = 0.0
    unit: str = "ST"
    po_id: str = Field(default="", description="MSEG-EBELN")
    po_item: str = Field(default="", description="MSEG-EBELP")
    batch: str = ""
    #: Bu satir bir TERS KAYIT mi? (102/122/162 veya `ReversedMaterialDocument`
    #: dolu). Net miktar hesabinda isareti eksi olan satir budur.
    reversed: bool = Field(default=False, description="Ters kayit hareketi mi")
    #: Bu satirin kendisi iptal EDILDI mi? SAP iptalde IKI belge birakir:
    #: asil 101 satiri `GoodsMovementIsCancelled=true` isaretlenir, ayrica
    #: bir 102 ters kayit belgesi olusur. Ikisini de eksi saymak miktari
    #: iki kez dusurur; bu alan ayrimi korur.
    cancelled: bool = Field(default=False, description="Asil hareket iptal edilmis mi")
    #: Ters kaydin hangi belgeyi iptal ettigi (MSEG-SMBLN).
    reverses_document: str = Field(default="", description="Ters kaydin hedefi")

    @property
    def is_reversal(self) -> bool:
        return self.reversed or self.movement_type in {"102", "122", "162"}

    @property
    def signed_quantity(self) -> float:
        """Net miktara katkisi. Iptal edilen asil satir +, ters kayit -.

        SAP aritmetigi budur: her malzeme belgesi satiri gercek bir kayittir
        ve isaretini hareket tipi belirler. Asil 101 (+10) ile onu iptal eden
        102 (-10) birbirini goturur; net 0. Ikisini birden eksi saymak -20
        verirdi - kismen teslim edilmis bir siparis "hic teslim edilmedi"
        gorunurdu.
        """
        return -self.quantity if self.is_reversal else self.quantity
