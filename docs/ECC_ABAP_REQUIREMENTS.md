# ECC 6.0 EHP8 - ABAP Gereksinim Dokumani

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

## Servisler
### `ZAGENT_MM_MATERIAL_SRV`
SICF yolu: `/sap/opu/odata/sap/ZAGENT_MM_MATERIAL_SRV`  
OData surumu: **V2**  
Kapsadigi yetenekler: `product`, `classification`, `valuation`

#### product - Malzeme ana verisi: MARA + MARC + MAKT + MBEW birlestirilmis
- **Tablolar:** `MARA`, `MARC`, `MAKT`, `MBEW`
- **CDS view'lari:** `ZI_AGENT_MATERIAL`, `ZI_AGENT_MATERIAL_DESC`

**`MaterialSet`**

| Alan | Onerilen tip |
|---|---|
| `Material` | Edm.String |
| `Plant` | Edm.String |
| `MaterialDescription` | Edm.String |
| `MaterialType` | Edm.String |
| `MaterialGroup` | Edm.String |
| `BaseUnit` | Edm.String |
| `GrossWeight` | Edm.Decimal |
| `ProcurementType` | Edm.String |
| `PlannedDeliveryDays` | Edm.Int32 |
| `MinimumLotSize` | Edm.Decimal |
| `MRPController` | Edm.String |
| `ABCIndicator` | Edm.String(1) |
| `SafetyStock` | Edm.Decimal |
| `MovingAveragePrice` | Edm.Decimal |
| `StandardPrice` | Edm.Decimal |
| `PriceUnit` | Edm.Decimal |
| `Currency` | Edm.String |

**`MaterialDescriptionSet`**

| Alan | Onerilen tip |
|---|---|
| `Material` | Edm.String |
| `Language` | Edm.String |
| `MaterialDescription` | Edm.String |

**Notlar:**

- MaterialSet tesis granuleritesinde olmali (MARA x MARC).
- MaterialDescription dil filtresi ile join edilir; SY-LANGU degil, istekteki dil kullanilmali.
- MBEW join'i VBELN degil BWKEY (degerleme alani) uzerinden yapilir.


#### classification - Siniflandirma karakteristikleri (KLAH/CABN/CAWN/AUSP)
- **Tablolar:** `KLAH`, `KSSK`, `CABN`, `CAWN`, `AUSP`
- **Fonksiyon modulleri:** `CLAF_CLASSIFICATION_OF_OBJECTS`, `BAPI_OBJCL_GETDETAIL`

**`MaterialClassSet`**

| Alan | Onerilen tip |
|---|---|
| `Material` | Edm.String |
| `ClassType` | Edm.String |
| `ClassName` | Edm.String |

**`MaterialCharcValueSet`**

| Alan | Onerilen tip |
|---|---|
| `Material` | Edm.String |
| `ClassType` | Edm.String |
| `Characteristic` | Edm.String |
| `CharcValue` | Edm.Decimal |
| `CharcValueUnit` | Edm.Decimal |

**Notlar:**

- Karakteristik adlari kucuk harfe normalize edilerek dondurulmeli; Python tarafi `payload_kg` gibi anahtarlar bekler.
- Sayisal karakteristikte AUSP-ATFLV, karakter tipinde CAWN-ATWRT.


#### valuation - Hareketli ortalama / standart fiyat (MBEW) - ayri okuma yolu
- **Tablolar:** `MBEW`

**`MaterialValuationSet`**

| Alan | Onerilen tip |
|---|---|
| `Material` | Edm.String |
| `ValuationArea` | Edm.String |
| `MovingAveragePrice` | Edm.Decimal |
| `StandardPrice` | Edm.Decimal |
| `PriceUnit` | Edm.Decimal |
| `PriceControl` | Edm.Decimal |
| `Currency` | Edm.String |

**Notlar:**

- PriceControl (MBEW-VPRSV) dondurulmeli: S=standart, V=hareketli.


### `ZAGENT_MM_STOCK_SRV`
SICF yolu: `/sap/opu/odata/sap/ZAGENT_MM_STOCK_SRV`  
OData surumu: **V2**  
Kapsadigi yetenekler: `stock`, `availability`, `mrp`

#### stock - Depo yeri bazinda stok (MARD/MCHB) + emniyet stogu
- **Tablolar:** `MARD`, `MCHB`, `MARC`

**`StockSet`**

| Alan | Onerilen tip |
|---|---|
| `Material` | Edm.String |
| `Plant` | Edm.String |
| `StorageLocation` | Edm.String |
| `UnrestrictedQuantity` | Edm.Decimal |
| `QualityInspectionQuantity` | Edm.Decimal |
| `BlockedQuantity` | Edm.Decimal |
| `SafetyStock` | Edm.Decimal |
| `BaseUnit` | Edm.String |

**Notlar:**

- Depo yeri bazinda satir dondurulur; toplama Python tarafinda yapilir.
- SafetyStock MARC-EISBE'den gelir ve tesis bazindadir: her depo yeri satirinda ayni deger tekrarlanir, toplanmamalidir.


#### availability - Gercek ATP: BAPI_MATERIAL_AVAILABILITY sarmalayicisi
- **Fonksiyon modulleri:** `BAPI_MATERIAL_AVAILABILITY`

**`AvailabilitySet`**

| Alan | Onerilen tip |
|---|---|
| `Material` | Edm.String |
| `Plant` | Edm.String |
| `RequestedQuantity` | Edm.Decimal |
| `RequestedDate` | Edm.DateTime |
| `CommittedQuantity` | Edm.Decimal |
| `CommittedDate` | Edm.DateTime |
| `SupplyElement` | Edm.String |
| `CheckingRule` | Edm.String |
| `CalendarConsidered` | Edm.String(1) |
| `BaseUnit` | Edm.String |

**Notlar:**

- WMDVEX cikti tablosu ATP agacini verir; her satir bir AvailabilitySet kaydina donusur.
- CalendarConsidered, kullanilan kontrol kuralinin fabrika takvimini dikkate alip almadigini bildirmeli. Sabit true DONDURULMEMELI.
- gATP/APO varsa BAPI_APO_AVAILABILITY_CHECK tercih edilir; servis hangisini kullandigini SupplyElement alaninda bildirir.


#### mrp - MD04 arz/talep listesi (MD_STOCK_REQUIREMENTS_LIST_API)
- **Fonksiyon modulleri:** `MD_STOCK_REQUIREMENTS_LIST_API`

**`SupplyDemandSet`**

| Alan | Onerilen tip |
|---|---|
| `Material` | Edm.String |
| `Plant` | Edm.String |
| `MRPElement` | Edm.String |
| `MRPElementId` | Edm.String |
| `AvailabilityDate` | Edm.DateTime |
| `Quantity` | Edm.Decimal |
| `BaseUnit` | Edm.String |
| `ElementDescription` | Edm.String |
| `WBSElement` | Edm.String |

**Notlar:**

- MDEZX tablosu arz/talep satirlarini verir.
- Quantity isaretli olmali: arz +, talep -. MD04 ekranindaki isaret mantigi korunur.


### `ZAGENT_MM_SOURCING_SRV`
SICF yolu: `/sap/opu/odata/sap/ZAGENT_MM_SOURCING_SRV`  
OData surumu: **V2**  
Kapsadigi yetenekler: `inforecord`, `supplier`, `supplier_score`

#### inforecord - Satinalma bilgi kaydi (EINA/EINE) + kademeli fiyat (KONP/KONM)
- **Tablolar:** `EINA`, `EINE`, `KONP`, `KONM`

**`InfoRecordSet`**

| Alan | Onerilen tip |
|---|---|
| `InfoRecord` | Edm.String |
| `Material` | Edm.String |
| `Supplier` | Edm.String |
| `SupplierName` | Edm.String |
| `PurchasingOrganization` | Edm.String |
| `Plant` | Edm.String |
| `NetPrice` | Edm.Decimal |
| `Currency` | Edm.String |
| `PriceUnit` | Edm.Decimal |
| `MinimumQuantity` | Edm.Decimal |
| `PlannedDeliveryDays` | Edm.Int32 |
| `Incoterms` | Edm.String |
| `PaymentTerms` | Edm.String |
| `ValidTo` | Edm.DateTime |
| `DeletionIndicator` | Edm.String(1) |

**`InfoRecordScaleSet`**

| Alan | Onerilen tip |
|---|---|
| `InfoRecord` | Edm.String |
| `ScaleQuantity` | Edm.Decimal |
| `ScalePrice` | Edm.Decimal |

**Notlar:**

- Silinmis kayitlar (EINA-LOEKZ / EINE-LOEKZ) filtrelenmeli.
- Kademeli fiyat KONM'den okunur; yoksa InfoRecordScaleSet bos doner ve net fiyat kullanilir.


#### supplier - Tedarikci ana verisi (LFA1 + LFM1)
- **Tablolar:** `LFA1`, `LFM1`, `LFB1`

**`SupplierSet`**

| Alan | Onerilen tip |
|---|---|
| `Supplier` | Edm.String |
| `SupplierName` | Edm.String |
| `Country` | Edm.String |
| `City` | Edm.String |
| `PurchasingBlock` | Edm.String(1) |
| `DeletionIndicator` | Edm.String(1) |

**Notlar:**

- PurchasingBlock LFM1-SPERM (satinalma org bazli) ve LFA1-SPERM (genel) birlestirilerek dondurulur.


#### supplier_score - Klasik tedarikci degerlendirmesi (ELBK/ELBP, ME6H)
- **Tablolar:** `ELBK`, `ELBP`, `ELBM`, `ELBA`
- **Islemler:** `ME6H`, `ME61`

**`SupplierScoreSet`**

| Alan | Onerilen tip |
|---|---|
| `Supplier` | Edm.String |
| `PurchasingOrganization` | Edm.String |
| `OverallScore` | Edm.Decimal |
| `PriceScore` | Edm.Decimal |
| `QualityScore` | Edm.Decimal |
| `DeliveryScore` | Edm.Decimal |
| `ServiceScore` | Edm.Decimal |
| `EvaluationPeriod` | Edm.String |

**Notlar:**

- OnTimeDeliveryPct ve QualityPPM ECC'de standart degildir.
- Hesaplanabiliyorsa EKES (teyit) ve EKBE (mal kabul) karsilastirmasi, kalite icin QMEL kullanilir; hesaplanamiyorsa alan BOS birakilmali. Sifir dondurmek yanlis karara yol acar - Python bos degeri `estimated_fields` olarak isaretler.


### `ZAGENT_MM_PR_SRV`
SICF yolu: `/sap/opu/odata/sap/ZAGENT_MM_PR_SRV`  
OData surumu: **V2**  
Kapsadigi yetenekler: `purchase_requisition`

#### purchase_requisition - PR olusturma/okuma (BAPI_PR_CREATE) + idempotency mutabakati
- **Tablolar:** `EBAN`, `EBKN`, `ZAGENT_IDEMPOTENCY`
- **Fonksiyon modulleri:** `BAPI_PR_CREATE`, `BAPI_TRANSACTION_COMMIT`, `ENQUEUE_EMEBANE`, `DEQUEUE_EMEBANE`

**`PurchaseRequisitionSet`**

| Alan | Onerilen tip |
|---|---|
| `PurchaseRequisition` | Edm.String |
| `DocumentType` | Edm.String |
| `HeaderText` | Edm.String |
| `IdempotencyKey` | Edm.String |
| `CreatedBy` | Edm.String |
| `CreationDate` | Edm.DateTime |

**`PurchaseRequisitionItemSet`**

| Alan | Onerilen tip |
|---|---|
| `PurchaseRequisition` | Edm.String |
| `PurchaseRequisitionItem` | Edm.String |
| `Material` | Edm.String |
| `Plant` | Edm.String |
| `Quantity` | Edm.Decimal |
| `BaseUnit` | Edm.String |
| `DeliveryDate` | Edm.DateTime |
| `PurchasingGroup` | Edm.String |
| `PurchasingOrganization` | Edm.String |
| `CompanyCode` | Edm.String |
| `Price` | Edm.Decimal |
| `Currency` | Edm.String |
| `FixedSupplier` | Edm.String |
| `ItemText` | Edm.String |
| `WBSElement` | Edm.String |
| `CostCenter` | Edm.Decimal |
| `DeletionIndicator` | Edm.String(1) |

**`IdempotencySet`**

| Alan | Onerilen tip |
|---|---|
| `IdempotencyKey` | Edm.String |
| `ObjectType` | Edm.String |
| `ObjectId` | Edm.String |
| `CreatedAt` | Edm.DateTime |
| `CreatedBy` | Edm.String |

**Notlar:**

- ZAGENT_IDEMPOTENCY: MANDT + IDEMPOTENCY_KEY (CHAR64, birincil anahtar) + OBJECT_TYPE + OBJECT_ID + CREATED_AT + CREATED_BY.
- Yazma sirasi: anahtari INSERT et (cakismada mevcut belgeyi dondur) -> BAPI_PR_CREATE -> COMMIT. Ayni LUW'da olmali.
- Cakisma durumunda HTTP 409 degil, mevcut PR numarasi ile 200 donmeli: mutabakat tekrar POST etmeden cozulur.
- Baslik metni STXH/STXL'de yasar ve FILTRELENEMEZ. Referans aramasi yalniz ZAGENT_IDEMPOTENCY uzerinden yapilir.


### `ZAGENT_MM_PO_SRV`
SICF yolu: `/sap/opu/odata/sap/ZAGENT_MM_PO_SRV`  
OData surumu: **V2**  
Kapsadigi yetenekler: `purchase_order`, `po_history`

#### purchase_order - PO baslik/kalem/teslimat plani (EKKO/EKPO/EKET) + EKBE ozetleri
- **Tablolar:** `EKKO`, `EKPO`, `EKET`, `EKBE`
- **Fonksiyon modulleri:** `BAPI_PO_GETDETAIL1`

**`PurchaseOrderSet`**

| Alan | Onerilen tip |
|---|---|
| `PurchaseOrder` | Edm.String |
| `Supplier` | Edm.String |
| `SupplierName` | Edm.String |
| `CreationDate` | Edm.DateTime |
| `DocumentCurrency` | Edm.String |
| `CompanyCode` | Edm.String |
| `PurchasingOrganization` | Edm.String |

**`PurchaseOrderItemSet`**

| Alan | Onerilen tip |
|---|---|
| `PurchaseOrder` | Edm.String |
| `PurchaseOrderItem` | Edm.String |
| `Material` | Edm.String |
| `Plant` | Edm.String |
| `ItemText` | Edm.String |
| `Quantity` | Edm.Decimal |
| `BaseUnit` | Edm.String |
| `NetPrice` | Edm.Decimal |
| `NetValue` | Edm.Decimal |
| `PriceUnit` | Edm.Decimal |
| `DeliveredQuantity` | Edm.Decimal |
| `InvoicedQuantity` | Edm.Decimal |
| `GoodsReceiptIndicator` | Edm.String(1) |
| `InvoiceReceiptIndicator` | Edm.String(1) |
| `DeletionIndicator` | Edm.String(1) |
| `AccountAssignmentCategory` | Edm.String |
| `WBSElement` | Edm.String |
| `PurchaseRequisition` | Edm.String |
| `PurchaseRequisitionItem` | Edm.String |

**`ScheduleLineSet`**

| Alan | Onerilen tip |
|---|---|
| `PurchaseOrder` | Edm.String |
| `PurchaseOrderItem` | Edm.String |
| `ScheduleLine` | Edm.String |
| `DeliveryDate` | Edm.DateTime |
| `ConfirmedDate` | Edm.DateTime |
| `ScheduleQuantity` | Edm.Decimal |
| `DeliveredQuantity` | Edm.Decimal |
| `BaseUnit` | Edm.String |

**Notlar:**

- DeliveredQuantity/InvoicedQuantity CDS icinde EKBE'den toplanir (BEWTP='E' ve 'Q'), kalem basina ayri cagri YAPILMAZ.
- SHKZG (borc/alacak) dikkate alinmali: iade satirlari cikarilir.
- PurchaseRequisition alani EKPO-BANFN'dir; belge akisinin PR bagi bu alandan kurulur.


#### po_history - EKBE satinalma siparisi gecmisi: belge akisinin omurgasi
- **Tablolar:** `EKBE`, `MKPF`, `MSEG`

**`PurchaseOrderHistorySet`**

| Alan | Onerilen tip |
|---|---|
| `PurchaseOrder` | Edm.String |
| `PurchaseOrderItem` | Edm.String |
| `HistoryCategory` | Edm.String |
| `MaterialDocument` | Edm.String |
| `MaterialDocumentYear` | Edm.Int32 |
| `MaterialDocumentItem` | Edm.String |
| `PostingDate` | Edm.DateTime |
| `Quantity` | Edm.Decimal |
| `Amount` | Edm.Decimal |
| `Currency` | Edm.String |
| `MovementType` | Edm.String |
| `DebitCreditIndicator` | Edm.String(1) |
| `ReferenceDocument` | Edm.String |

**`GoodsReceiptSet`**

| Alan | Onerilen tip |
|---|---|
| `MaterialDocument` | Edm.String |
| `MaterialDocumentYear` | Edm.Int32 |
| `MaterialDocumentItem` | Edm.String |
| `PostingDate` | Edm.DateTime |
| `MovementType` | Edm.String |
| `Material` | Edm.String |
| `Plant` | Edm.String |
| `Quantity` | Edm.Decimal |
| `BaseUnit` | Edm.String |
| `PurchaseOrder` | Edm.String |
| `PurchaseOrderItem` | Edm.String |
| `Batch` | Edm.String |

**Notlar:**

- HistoryCategory = EKBE-BEWTP. E=mal kabul, Q=fatura girisi.
- Iptal hareketleri (101 karsisinda 102, 122, 161/162) ayri satir olarak dondurulur; netlestirme Python tarafinda yapilir.


### `ZAGENT_FI_INVOICE_SRV`
SICF yolu: `/sap/opu/odata/sap/ZAGENT_FI_INVOICE_SRV`  
OData surumu: **V2**  
Kapsadigi yetenekler: `supplier_invoice`

#### supplier_invoice - Tedarikci faturasi (RBKP/RSEG) + blokaj nedenleri + odeme
- **Tablolar:** `RBKP`, `RSEG`, `RBKP_BLOCKED`, `BKPF`, `BSEG`
- **Fonksiyon modulleri:** `BAPI_INCOMINGINVOICE_GETDETAIL`, `BAPI_INCOMINGINVOICE_GETLIST`

**`SupplierInvoiceSet`**

| Alan | Onerilen tip |
|---|---|
| `SupplierInvoice` | Edm.String |
| `FiscalYear` | Edm.Int32 |
| `Supplier` | Edm.String |
| `SupplierName` | Edm.String |
| `CompanyCode` | Edm.String |
| `DocumentDate` | Edm.DateTime |
| `PostingDate` | Edm.DateTime |
| `DueDate` | Edm.DateTime |
| `GrossAmount` | Edm.Decimal |
| `NetAmount` | Edm.Decimal |
| `TaxAmount` | Edm.Decimal |
| `Currency` | Edm.String |
| `InvoiceStatus` | Edm.String |
| `PaymentBlock` | Edm.String(1) |
| `PaymentTerms` | Edm.String |
| `ClearingDate` | Edm.DateTime |
| `AccountingDocument` | Edm.String |

**`SupplierInvoiceItemSet`**

| Alan | Onerilen tip |
|---|---|
| `SupplierInvoice` | Edm.String |
| `FiscalYear` | Edm.Int32 |
| `InvoiceItem` | Edm.String |
| `PurchaseOrder` | Edm.String |
| `PurchaseOrderItem` | Edm.String |
| `Quantity` | Edm.Decimal |
| `Amount` | Edm.Decimal |
| `Currency` | Edm.String |

**`InvoiceBlockSet`**

| Alan | Onerilen tip |
|---|---|
| `SupplierInvoice` | Edm.String |
| `FiscalYear` | Edm.Int32 |
| `InvoiceItem` | Edm.String |
| `BlockReason` | Edm.String |
| `ToleranceKey` | Edm.String |
| `ExpectedValue` | Edm.Decimal |
| `ActualValue` | Edm.Decimal |
| `ToleranceLimitAbsolute` | Edm.String |
| `ToleranceLimitPercent` | Edm.Decimal |
| `Currency` | Edm.String |
| `PurchaseOrder` | Edm.String |
| `PurchaseOrderItem` | Edm.String |

**Notlar:**

- PaymentBlock = RBKP-ZLSPR. Blokaj nedeni RSEG tolerans kontrollerinden turetilir (OMR6 anahtarlari).
- ClearingDate BSEG-AUGDT uzerinden; odenmis fatura tespiti buna dayanir.
- InvoiceStatus: parked (RBKP-RBSTAT='A'), posted ('5'), blocked (ZLSPR dolu), cancelled ('3').


### `ZAGENT_WF_STATUS_SRV`
SICF yolu: `/sap/opu/odata/sap/ZAGENT_WF_STATUS_SRV`  
OData surumu: **V2**  
Kapsadigi yetenekler: `workflow`

#### workflow - SAP Business Workflow durumu (SAP_WAPI_* sarmalayicisi)
- **Fonksiyon modulleri:** `SAP_WAPI_WORKITEMS_TO_OBJECT`, `SAP_WAPI_GET_WORKITEM_DETAIL`, `SAP_WAPI_GET_HEADER`

**`WorkflowStepSet`**

| Alan | Onerilen tip |
|---|---|
| `ObjectType` | Edm.String |
| `ObjectId` | Edm.String |
| `WorkflowId` | Edm.String |
| `WorkItemId` | Edm.String |
| `StepNumber` | Edm.Int32 |
| `StepName` | Edm.String |
| `WorkItemStatus` | Edm.String |
| `Decision` | Edm.String |
| `ProcessorName` | Edm.String |
| `ProcessorRole` | Edm.String |
| `StartedAt` | Edm.DateTime |
| `CompletedAt` | Edm.DateTime |
| `DueAt` | Edm.DateTime |
| `Note` | Edm.String |

**Notlar:**

- ObjectType ornekleri: BUS2105 (PR), BUS2012 (PO), BUS2081 (fatura).
- ProcessorName kisisel veridir (D2); servis maskeleme yapmaz, Python DLP katmani yapar. Ham deger dondurulmeli.
- WorkItemStatus SAP kodlari (READY/STARTED/COMPLETED/CANCELLED) oldugu gibi dondurulur; esleme Python tarafinda.


### `ZAGENT_PS_COST_SRV`
SICF yolu: `/sap/opu/odata/sap/ZAGENT_PS_COST_SRV`  
OData surumu: **V2**  
Kapsadigi yetenekler: `project_cost`

#### project_cost - WBS plan/fiili/taahhut (PRPS + COSP/COSS + COOI)
- **Tablolar:** `PRPS`, `PROJ`, `COSP`, `COSS`, `COOI`, `RPSCO`
- **Fonksiyon modulleri:** `BAPI_PROJECT_GETINFO`

**`ProjectCostSet`**

| Alan | Onerilen tip |
|---|---|
| `WBSElement` | Edm.String |
| `WBSDescription` | Edm.String |
| `PlanCost` | Edm.Decimal |
| `ActualCost` | Edm.Decimal |
| `Commitment` | Edm.Decimal |
| `Currency` | Edm.String |
| `FiscalYear` | Edm.Int32 |
| `CompletionPercent` | Edm.Decimal |

**Notlar:**

- Commitment COOI'den (acik siparis taahhutu) gelir.
- CompletionPercent PRPS'te standart alan degildir; ilerleme analizi (CNE5) yoksa bos birakilmali.



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
