"""ECC 6.0 EHP8 yetenek manifesti.

`adapters.sap.capabilities` icindeki `ServiceCapability`, `parse_metadata` ve
`verify_contract` protokol-genel yapilardir; ECC'de aynen kullanilir. Burada
yalniz **manifest** ECC'ye ozgudur.

Tasarim kararlari:

  1. **Az sayida genis servis.** Her SEGW projesi gercek ABAP emegidir. 14 dar
     servis yerine 8 domain servisi tanimlanir; entity set'ler servis icinde
     cogaltilir.
  2. **CDS'te join, Python'da degil.** `MaterialSet` MARA+MARC+MAKT+MBEW'i
     birlestirilmis dondurur. S/4 adapterindaki uc ayri cagri (arama, aciklama,
     degerleme) tek cagriya duser; `PerformanceBudget.max_sap_calls` boylece
     V2'de de tutar.
  3. **EKBE merkezli P2P.** ECC'de satinalma siparisi gecmisi (EKBE) mal kabul
     ve fatura girisini kalem bazinda zaten tasir. Belge akisi bu yuzden S/4'ten
     daha az cagriyla ve **tahmin yapmadan** kurulur.
  4. **Idempotency SAP tarafinda.** S/4 adapteri referansi PR baslik metnine
     gomup `contains()` ile ariyor. ECC'de baslik metni STXH/STXL'de yasar ve
     **filtrelenemez**; bu yuzden `ZAGENT_IDEMPOTENCY` tablosu zorunludur ve
     dogrudan anahtar esitligiyle sorgulanir. Substring taramasindan saglamdir.
  5. **Hepsi STATUS_CUSTOM.** ECC'de released public OData API yoktur. Manifest
     bunu gizlemez; upgrade regresyon yuku acikca gorunur kalir.

Servis adlandirma: `ZAGENT_<modul>_<konu>_SRV`, SICF yolu
`/sap/opu/odata/sap/<servis>`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..sap.capabilities import STATUS_CUSTOM, ServiceCapability

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

#: Idempotency mutabakatinin okundugu entity set (bkz. ABAP_SOURCES["purchase_requisition"]).
IDEMPOTENCY_ENTITY_SET = "IdempotencySet"

#: ECC MD04 arz/talep element kodlari -> okunabilir aciklama.
#: `SupplyDemandItem.mrp_element` bu kodlari oldugu gibi tasir; model kod
#: uydurmaz. Rezervasyon tespiti (`_reservation_quantity`) bu tabloya dayanir.
MRP_ELEMENT_LABELS: dict[str, str] = {
    "WB": "Stok (Werksbestand)",
    "BE": "Acik satinalma siparisi",
    "BA": "Satinalma talebi",
    "LA": "Teslimat plani cagrisi",
    "FE": "Uretim siparisi",
    "PA": "Planlanan siparis",
    "VC": "Musteri siparisi",
    "VJ": "Teslimat",
    "AR": "Rezervasyon",
    "MS": "Bagimsiz ihtiyac (PIR)",
    "SH": "Emniyet stogu",
    "U1": "Stok transferi",
    "QM": "Kalite kontrol stogu",
}

#: Talep (negatif) sayilan MRP elementleri. Rezervasyon toplaminda kullanilir.
DEMAND_ELEMENTS: frozenset[str] = frozenset({"AR", "VC", "VJ", "MS", "U1"})


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
ECC_CAPABILITY_MANIFEST: dict[str, ServiceCapability] = {
    # --- Malzeme ana verisi -------------------------------------------------
    "product": ServiceCapability(
        alias="product",
        service_path="/sap/opu/odata/sap/ZAGENT_MM_MATERIAL_SRV",
        odata_version="v2",
        purpose="Malzeme ana verisi: MARA + MARC + MAKT + MBEW birlestirilmis",
        entity_sets=("MaterialSet", "MaterialDescriptionSet"),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#zagent_mm_material_srv",
        critical_properties={
            # Tek cagri hedefi: aciklama ve degerleme burada gelir, ek GET yok.
            "MaterialSet": (
                "Material",
                "Plant",
                "MaterialDescription",
                "MaterialType",
                "MaterialGroup",
                "BaseUnit",
                "GrossWeight",
                "ProcurementType",
                "PlannedDeliveryDays",
                "MinimumLotSize",
                "MRPController",
                "ABCIndicator",
                "SafetyStock",
                "MovingAveragePrice",
                "StandardPrice",
                "PriceUnit",
                "Currency",
            ),
            "MaterialDescriptionSet": ("Material", "Language", "MaterialDescription"),
        },
    ),
    "classification": ServiceCapability(
        alias="classification",
        service_path="/sap/opu/odata/sap/ZAGENT_MM_MATERIAL_SRV",
        odata_version="v2",
        purpose="Siniflandirma karakteristikleri (KLAH/CABN/CAWN/AUSP)",
        entity_sets=("MaterialClassSet", "MaterialCharcValueSet"),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#zagent_mm_material_srv",
        critical_properties={
            "MaterialCharcValueSet": (
                "Material",
                "ClassType",
                "Characteristic",
                "CharcValue",
                "CharcValueUnit",
            ),
            "MaterialClassSet": ("Material", "ClassType", "ClassName"),
        },
    ),
    "valuation": ServiceCapability(
        alias="valuation",
        service_path="/sap/opu/odata/sap/ZAGENT_MM_MATERIAL_SRV",
        odata_version="v2",
        purpose="Hareketli ortalama / standart fiyat (MBEW) - ayri okuma yolu",
        entity_sets=("MaterialValuationSet",),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#zagent_mm_material_srv",
        critical_properties={
            "MaterialValuationSet": (
                "Material",
                "ValuationArea",
                "MovingAveragePrice",
                "StandardPrice",
                "PriceUnit",
                "PriceControl",
                "Currency",
            )
        },
    ),
    # --- Stok, ATP, MRP -----------------------------------------------------
    "stock": ServiceCapability(
        alias="stock",
        service_path="/sap/opu/odata/sap/ZAGENT_MM_STOCK_SRV",
        odata_version="v2",
        purpose="Depo yeri bazinda stok (MARD/MCHB) + emniyet stogu",
        entity_sets=("StockSet",),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#zagent_mm_stock_srv",
        critical_properties={
            "StockSet": (
                "Material",
                "Plant",
                "StorageLocation",
                "UnrestrictedQuantity",
                "QualityInspectionQuantity",
                "BlockedQuantity",
                "SafetyStock",
                "BaseUnit",
            )
        },
    ),
    "availability": ServiceCapability(
        alias="availability",
        service_path="/sap/opu/odata/sap/ZAGENT_MM_STOCK_SRV",
        odata_version="v2",
        purpose="Gercek ATP: BAPI_MATERIAL_AVAILABILITY sarmalayicisi",
        entity_sets=("AvailabilitySet",),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#atp",
        critical_properties={
            # Girdi (RequestedQuantity/RequestedDate) filtrede gelir, cikti
            # ATP agacidir. `CalendarConsidered` servisten okunur, sabitlenmez.
            "AvailabilitySet": (
                "Material",
                "Plant",
                "RequestedQuantity",
                "RequestedDate",
                "CommittedQuantity",
                "CommittedDate",
                "SupplyElement",
                "CheckingRule",
                "CalendarConsidered",
                "BaseUnit",
            )
        },
    ),
    "mrp": ServiceCapability(
        alias="mrp",
        service_path="/sap/opu/odata/sap/ZAGENT_MM_STOCK_SRV",
        odata_version="v2",
        purpose="MD04 arz/talep listesi (MD_STOCK_REQUIREMENTS_LIST_API)",
        entity_sets=("SupplyDemandSet",),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#zagent_mm_stock_srv",
        critical_properties={
            "SupplyDemandSet": (
                "Material",
                "Plant",
                "MRPElement",
                "MRPElementId",
                "AvailabilityDate",
                "Quantity",
                "BaseUnit",
                "ElementDescription",
                "WBSElement",
            )
        },
    ),
    # --- Kaynak bulma -------------------------------------------------------
    "inforecord": ServiceCapability(
        alias="inforecord",
        service_path="/sap/opu/odata/sap/ZAGENT_MM_SOURCING_SRV",
        odata_version="v2",
        purpose="Satinalma bilgi kaydi (EINA/EINE) + kademeli fiyat (KONP/KONM)",
        entity_sets=("InfoRecordSet", "InfoRecordScaleSet"),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#zagent_mm_sourcing_srv",
        critical_properties={
            "InfoRecordSet": (
                "InfoRecord",
                "Material",
                "Supplier",
                "SupplierName",
                "PurchasingOrganization",
                "Plant",
                "NetPrice",
                "Currency",
                "PriceUnit",
                "MinimumQuantity",
                "PlannedDeliveryDays",
                "Incoterms",
                "PaymentTerms",
                "ValidTo",
                "DeletionIndicator",
            ),
            "InfoRecordScaleSet": ("InfoRecord", "ScaleQuantity", "ScalePrice"),
        },
    ),
    "supplier": ServiceCapability(
        alias="supplier",
        service_path="/sap/opu/odata/sap/ZAGENT_MM_SOURCING_SRV",
        odata_version="v2",
        purpose="Tedarikci ana verisi (LFA1 + LFM1)",
        entity_sets=("SupplierSet",),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#zagent_mm_sourcing_srv",
        critical_properties={
            "SupplierSet": (
                "Supplier",
                "SupplierName",
                "Country",
                "City",
                "PurchasingBlock",
                "DeletionIndicator",
            )
        },
    ),
    "supplier_score": ServiceCapability(
        alias="supplier_score",
        service_path="/sap/opu/odata/sap/ZAGENT_MM_SOURCING_SRV",
        odata_version="v2",
        purpose="Klasik tedarikci degerlendirmesi (ELBK/ELBP, ME6H)",
        entity_sets=("SupplierScoreSet",),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#tedarikci-skoru",
        critical_properties={
            # OnTimeDeliveryPct ve QualityPPM ECC'de standart alan DEGILDIR.
            # Kritik listeye alinmazlar; yoklar ise `estimated_fields` isaretlenir.
            "SupplierScoreSet": (
                "Supplier",
                "PurchasingOrganization",
                "OverallScore",
                "PriceScore",
                "QualityScore",
                "DeliveryScore",
                "ServiceScore",
                "EvaluationPeriod",
            )
        },
    ),
    # --- Satinalma talebi (tek yazma yolu) ----------------------------------
    "purchase_requisition": ServiceCapability(
        alias="purchase_requisition",
        service_path="/sap/opu/odata/sap/ZAGENT_MM_PR_SRV",
        odata_version="v2",
        purpose="PR olusturma/okuma (BAPI_PR_CREATE) + idempotency mutabakati",
        entity_sets=(
            "PurchaseRequisitionSet",
            "PurchaseRequisitionItemSet",
            IDEMPOTENCY_ENTITY_SET,
        ),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#zagent_mm_pr_srv",
        critical_properties={
            "PurchaseRequisitionSet": (
                "PurchaseRequisition",
                "DocumentType",
                "HeaderText",
                "IdempotencyKey",
                "CreatedBy",
                "CreationDate",
            ),
            "PurchaseRequisitionItemSet": (
                "PurchaseRequisition",
                "PurchaseRequisitionItem",
                "Material",
                "Plant",
                "Quantity",
                "BaseUnit",
                "DeliveryDate",
                "PurchasingGroup",
                "PurchasingOrganization",
                "CompanyCode",
                "Price",
                "Currency",
                "FixedSupplier",
                "ItemText",
                "WBSElement",
                "CostCenter",
                "DeletionIndicator",
            ),
            # Mutabakat sozlesmesi: anahtar -> belge. Substring taramasi YOK.
            IDEMPOTENCY_ENTITY_SET: (
                "IdempotencyKey",
                "ObjectType",
                "ObjectId",
                "CreatedAt",
                "CreatedBy",
            ),
        },
    ),
    # --- Satinalma siparisi ve P2P -----------------------------------------
    "purchase_order": ServiceCapability(
        alias="purchase_order",
        service_path="/sap/opu/odata/sap/ZAGENT_MM_PO_SRV",
        odata_version="v2",
        purpose="PO baslik/kalem/teslimat plani (EKKO/EKPO/EKET) + EKBE ozetleri",
        entity_sets=("PurchaseOrderSet", "PurchaseOrderItemSet", "ScheduleLineSet"),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#zagent_mm_po_srv",
        critical_properties={
            "PurchaseOrderSet": (
                "PurchaseOrder",
                "Supplier",
                "SupplierName",
                "CreationDate",
                "DocumentCurrency",
                "CompanyCode",
                "PurchasingOrganization",
            ),
            # DeliveredQuantity/InvoicedQuantity EKBE'den CDS icinde toplanir:
            # kalem basina ek gecmis cagrisi yapilmaz (N+1 engellenir).
            "PurchaseOrderItemSet": (
                "PurchaseOrder",
                "PurchaseOrderItem",
                "Material",
                "Plant",
                "ItemText",
                "Quantity",
                "BaseUnit",
                "NetPrice",
                "NetValue",
                "PriceUnit",
                "DeliveredQuantity",
                "InvoicedQuantity",
                "GoodsReceiptIndicator",
                "InvoiceReceiptIndicator",
                "DeletionIndicator",
                "AccountAssignmentCategory",
                "WBSElement",
                "PurchaseRequisition",
                "PurchaseRequisitionItem",
            ),
            "ScheduleLineSet": (
                "PurchaseOrder",
                "PurchaseOrderItem",
                "ScheduleLine",
                "DeliveryDate",
                "ConfirmedDate",
                "ScheduleQuantity",
                "DeliveredQuantity",
                "BaseUnit",
            ),
        },
    ),
    "po_history": ServiceCapability(
        alias="po_history",
        service_path="/sap/opu/odata/sap/ZAGENT_MM_PO_SRV",
        odata_version="v2",
        purpose="EKBE satinalma siparisi gecmisi: belge akisinin omurgasi",
        entity_sets=("PurchaseOrderHistorySet", "GoodsReceiptSet"),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#belge-akisi",
        critical_properties={
            # HistoryCategory (EKBE-BEWTP): E=mal kabul, Q=fatura girisi,
            # U=stok transferi. Belge akisi bu alandan kurulur, tahminle degil.
            "PurchaseOrderHistorySet": (
                "PurchaseOrder",
                "PurchaseOrderItem",
                "HistoryCategory",
                "MaterialDocument",
                "MaterialDocumentYear",
                "MaterialDocumentItem",
                "PostingDate",
                "Quantity",
                "Amount",
                "Currency",
                "MovementType",
                "DebitCreditIndicator",
                "ReferenceDocument",
            ),
            "GoodsReceiptSet": (
                "MaterialDocument",
                "MaterialDocumentYear",
                "MaterialDocumentItem",
                "PostingDate",
                "MovementType",
                "Material",
                "Plant",
                "Quantity",
                "BaseUnit",
                "PurchaseOrder",
                "PurchaseOrderItem",
                "Batch",
            ),
        },
    ),
    # --- Fatura -------------------------------------------------------------
    "supplier_invoice": ServiceCapability(
        alias="supplier_invoice",
        service_path="/sap/opu/odata/sap/ZAGENT_FI_INVOICE_SRV",
        odata_version="v2",
        purpose="Tedarikci faturasi (RBKP/RSEG) + blokaj nedenleri + odeme",
        entity_sets=("SupplierInvoiceSet", "SupplierInvoiceItemSet", "InvoiceBlockSet"),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#zagent_fi_invoice_srv",
        critical_properties={
            "SupplierInvoiceSet": (
                "SupplierInvoice",
                "FiscalYear",
                "Supplier",
                "SupplierName",
                "CompanyCode",
                "DocumentDate",
                "PostingDate",
                "DueDate",
                "GrossAmount",
                "NetAmount",
                "TaxAmount",
                "Currency",
                "InvoiceStatus",
                "PaymentBlock",
                "PaymentTerms",
                "ClearingDate",
                "AccountingDocument",
            ),
            "SupplierInvoiceItemSet": (
                "SupplierInvoice",
                "FiscalYear",
                "InvoiceItem",
                "PurchaseOrder",
                "PurchaseOrderItem",
                "Quantity",
                "Amount",
                "Currency",
            ),
            # Blokaj nedenleri deterministiktir: OMR6 tolerans anahtari tasinir.
            "InvoiceBlockSet": (
                "SupplierInvoice",
                "FiscalYear",
                "InvoiceItem",
                "BlockReason",
                "ToleranceKey",
                "ExpectedValue",
                "ActualValue",
                "ToleranceLimitAbsolute",
                "ToleranceLimitPercent",
                "Currency",
                "PurchaseOrder",
                "PurchaseOrderItem",
            ),
        },
    ),
    # --- Is akisi -----------------------------------------------------------
    "workflow": ServiceCapability(
        alias="workflow",
        service_path="/sap/opu/odata/sap/ZAGENT_WF_STATUS_SRV",
        odata_version="v2",
        purpose="SAP Business Workflow durumu (SAP_WAPI_* sarmalayicisi)",
        entity_sets=("WorkflowStepSet",),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#zagent_wf_status_srv",
        critical_properties={
            "WorkflowStepSet": (
                "ObjectType",
                "ObjectId",
                "WorkflowId",
                "WorkItemId",
                "StepNumber",
                "StepName",
                "WorkItemStatus",
                "Decision",
                "ProcessorName",
                "ProcessorRole",
                "StartedAt",
                "CompletedAt",
                "DueAt",
                "Note",
            )
        },
    ),
    # --- Proje maliyeti -----------------------------------------------------
    "project_cost": ServiceCapability(
        alias="project_cost",
        service_path="/sap/opu/odata/sap/ZAGENT_PS_COST_SRV",
        odata_version="v2",
        purpose="WBS plan/fiili/taahhut (PRPS + COSP/COSS + COOI)",
        entity_sets=("ProjectCostSet",),
        status=STATUS_CUSTOM,
        doc_url="docs/ECC_ABAP_REQUIREMENTS.md#zagent_ps_cost_srv",
        critical_properties={
            "ProjectCostSet": (
                "WBSElement",
                "WBSDescription",
                "PlanCost",
                "ActualCost",
                "Commitment",
                "Currency",
                "FiscalYear",
                "CompletionPercent",
            )
        },
    ),
}


# ---------------------------------------------------------------------------
# ABAP kaynak nesneleri
# ---------------------------------------------------------------------------
#: Her servis alias'i icin ABAP tarafinda uygulanmasi gereken nesneler.
#: `docs/ECC_ABAP_REQUIREMENTS.md` bu sozlukten uretilebilir; boylece Python
#: sozlesmesi ile ABAP is listesi tek kaynaktan beslenir ve ayrisamaz.
ABAP_SOURCES: dict[str, dict[str, tuple[str, ...]]] = {
    "product": {
        "tables": ("MARA", "MARC", "MAKT", "MBEW"),
        "cds": ("ZI_AGENT_MATERIAL", "ZI_AGENT_MATERIAL_DESC"),
        "notes": (
            "MaterialSet tesis granuleritesinde olmali (MARA x MARC).",
            "MaterialDescription dil filtresi ile join edilir; SY-LANGU degil, "
            "istekteki dil kullanilmali.",
            "MBEW join'i VBELN degil BWKEY (degerleme alani) uzerinden yapilir.",
        ),
    },
    "classification": {
        "tables": ("KLAH", "KSSK", "CABN", "CAWN", "AUSP"),
        "function_modules": ("CLAF_CLASSIFICATION_OF_OBJECTS", "BAPI_OBJCL_GETDETAIL"),
        "notes": (
            "Karakteristik adlari kucuk harfe normalize edilerek dondurulmeli; "
            "Python tarafi `payload_kg` gibi anahtarlar bekler.",
            "Sayisal karakteristikte AUSP-ATFLV, karakter tipinde CAWN-ATWRT.",
        ),
    },
    "valuation": {
        "tables": ("MBEW",),
        "notes": ("PriceControl (MBEW-VPRSV) dondurulmeli: S=standart, V=hareketli.",),
    },
    "stock": {
        "tables": ("MARD", "MCHB", "MARC"),
        "notes": (
            "Depo yeri bazinda satir dondurulur; toplama Python tarafinda yapilir.",
            "SafetyStock MARC-EISBE'den gelir ve tesis bazindadir: her depo yeri "
            "satirinda ayni deger tekrarlanir, toplanmamalidir.",
        ),
    },
    "availability": {
        "function_modules": ("BAPI_MATERIAL_AVAILABILITY",),
        "notes": (
            "WMDVEX cikti tablosu ATP agacini verir; her satir bir "
            "AvailabilitySet kaydina donusur.",
            "CalendarConsidered, kullanilan kontrol kuralinin fabrika takvimini "
            "dikkate alip almadigini bildirmeli. Sabit true DONDURULMEMELI.",
            "gATP/APO varsa BAPI_APO_AVAILABILITY_CHECK tercih edilir; servis "
            "hangisini kullandigini SupplyElement alaninda bildirir.",
        ),
    },
    "mrp": {
        "function_modules": ("MD_STOCK_REQUIREMENTS_LIST_API",),
        "notes": (
            "MDEZX tablosu arz/talep satirlarini verir.",
            "Quantity isaretli olmali: arz +, talep -. MD04 ekranindaki isaret "
            "mantigi korunur.",
        ),
    },
    "inforecord": {
        "tables": ("EINA", "EINE", "KONP", "KONM"),
        "notes": (
            "Silinmis kayitlar (EINA-LOEKZ / EINE-LOEKZ) filtrelenmeli.",
            "Kademeli fiyat KONM'den okunur; yoksa InfoRecordScaleSet bos doner "
            "ve net fiyat kullanilir.",
        ),
    },
    "supplier": {
        "tables": ("LFA1", "LFM1", "LFB1"),
        "notes": (
            "PurchasingBlock LFM1-SPERM (satinalma org bazli) ve LFA1-SPERM "
            "(genel) birlestirilerek dondurulur.",
        ),
    },
    "supplier_score": {
        "tables": ("ELBK", "ELBP", "ELBM", "ELBA"),
        "transactions": ("ME6H", "ME61"),
        "notes": (
            "OnTimeDeliveryPct ve QualityPPM ECC'de standart degildir.",
            "Hesaplanabiliyorsa EKES (teyit) ve EKBE (mal kabul) karsilastirmasi, "
            "kalite icin QMEL kullanilir; hesaplanamiyorsa alan BOS birakilmali. "
            "Sifir dondurmek yanlis karara yol acar - Python bos degeri "
            "`estimated_fields` olarak isaretler.",
        ),
    },
    "purchase_requisition": {
        "tables": ("EBAN", "EBKN", "ZAGENT_IDEMPOTENCY"),
        "function_modules": (
            "BAPI_PR_CREATE",
            "BAPI_TRANSACTION_COMMIT",
            "ENQUEUE_EMEBANE",
            "DEQUEUE_EMEBANE",
        ),
        "notes": (
            "ZAGENT_IDEMPOTENCY: MANDT + IDEMPOTENCY_KEY (CHAR64, birincil "
            "anahtar) + OBJECT_TYPE + OBJECT_ID + CREATED_AT + CREATED_BY.",
            "Yazma sirasi: anahtari INSERT et (cakismada mevcut belgeyi dondur) "
            "-> BAPI_PR_CREATE -> COMMIT. Ayni LUW'da olmali.",
            "Cakisma durumunda HTTP 409 degil, mevcut PR numarasi ile 200 "
            "donmeli: mutabakat tekrar POST etmeden cozulur.",
            "Baslik metni STXH/STXL'de yasar ve FILTRELENEMEZ. Referans "
            "aramasi yalniz ZAGENT_IDEMPOTENCY uzerinden yapilir.",
        ),
    },
    "purchase_order": {
        "tables": ("EKKO", "EKPO", "EKET", "EKBE"),
        "function_modules": ("BAPI_PO_GETDETAIL1",),
        "notes": (
            "DeliveredQuantity/InvoicedQuantity CDS icinde EKBE'den toplanir "
            "(BEWTP='E' ve 'Q'), kalem basina ayri cagri YAPILMAZ.",
            "SHKZG (borc/alacak) dikkate alinmali: iade satirlari cikarilir.",
            "PurchaseRequisition alani EKPO-BANFN'dir; belge akisinin PR bagi "
            "bu alandan kurulur.",
        ),
    },
    "po_history": {
        "tables": ("EKBE", "MKPF", "MSEG"),
        "notes": (
            "HistoryCategory = EKBE-BEWTP. E=mal kabul, Q=fatura girisi.",
            "Iptal hareketleri (101 karsisinda 102, 122, 161/162) ayri satir "
            "olarak dondurulur; netlestirme Python tarafinda yapilir.",
        ),
    },
    "supplier_invoice": {
        "tables": ("RBKP", "RSEG", "RBKP_BLOCKED", "BKPF", "BSEG"),
        "function_modules": (
            "BAPI_INCOMINGINVOICE_GETDETAIL",
            "BAPI_INCOMINGINVOICE_GETLIST",
        ),
        "notes": (
            "PaymentBlock = RBKP-ZLSPR. Blokaj nedeni RSEG tolerans "
            "kontrollerinden turetilir (OMR6 anahtarlari).",
            "ClearingDate BSEG-AUGDT uzerinden; odenmis fatura tespiti buna dayanir.",
            "InvoiceStatus: parked (RBKP-RBSTAT='A'), posted ('5'), "
            "blocked (ZLSPR dolu), cancelled ('3').",
        ),
    },
    "workflow": {
        "function_modules": (
            "SAP_WAPI_WORKITEMS_TO_OBJECT",
            "SAP_WAPI_GET_WORKITEM_DETAIL",
            "SAP_WAPI_GET_HEADER",
        ),
        "notes": (
            "ObjectType ornekleri: BUS2105 (PR), BUS2012 (PO), BUS2081 (fatura).",
            "ProcessorName kisisel veridir (D2); servis maskeleme yapmaz, "
            "Python DLP katmani yapar. Ham deger dondurulmeli.",
            "WorkItemStatus SAP kodlari (READY/STARTED/COMPLETED/CANCELLED) "
            "oldugu gibi dondurulur; esleme Python tarafinda.",
        ),
    },
    "project_cost": {
        "tables": ("PRPS", "PROJ", "COSP", "COSS", "COOI", "RPSCO"),
        "function_modules": ("BAPI_PROJECT_GETINFO",),
        "notes": (
            "Commitment COOI'den (acik siparis taahhutu) gelir.",
            "CompletionPercent PRPS'te standart alan degildir; ilerleme analizi "
            "(CNE5) yoksa bos birakilmali.",
        ),
    },
}


# ---------------------------------------------------------------------------
# Yardimcilar
# ---------------------------------------------------------------------------
def ecc_service_path(alias: str) -> str:
    """Alias -> SICF servis yolu. Bilinmeyen alias sessizce gecmez."""
    capability = ECC_CAPABILITY_MANIFEST.get(alias)
    if capability is None:
        raise KeyError(f"Bilinmeyen ECC servis alias'i: {alias}")
    return capability.service_path


def ecc_manifest_summary(aliases: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """`sap_discover_capabilities` icin manifest ozeti (+ ABAP kaynaklari)."""
    keys = list(aliases) if aliases else list(ECC_CAPABILITY_MANIFEST)
    out: list[dict[str, Any]] = []
    for key in keys:
        capability = ECC_CAPABILITY_MANIFEST.get(key)
        if capability is None:
            continue
        payload = capability.to_dict()
        sources = ABAP_SOURCES.get(key, {})
        if sources:
            payload["abap_sources"] = {k: list(v) for k, v in sources.items()}
        out.append(payload)
    return out


def distinct_service_paths() -> tuple[str, ...]:
    """Manifestteki benzersiz SICF yollari - saglik kontrolu ve aktivasyon icin."""
    return tuple(dict.fromkeys(c.service_path for c in ECC_CAPABILITY_MANIFEST.values()))
