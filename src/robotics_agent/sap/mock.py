"""Mock SAP backend.

Gercek S/4HANA'ya erisim olmadan tum akisi ucdan uca calistirmak icin kullanilir.
Ayni portlari implemente ettigi icin tool katmani mock/gercek ayrimini gormez.

Mock, gercek sistemin **yeteneklerini** de taklit eder: ATP tarih bazli hesaplanir,
MRP arz/talep elementleri uretilir, PR prepare/submit ayrimi ve idempotent
referans arama gercek backend ile ayni sozlesmeyi izler. Boylece mock uzerinde
gecen bir akis gercek sisteme tasindiginda kontrat degismez.
"""

from __future__ import annotations

import itertools
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..core.tenant_profile import DEFAULT_DOCUMENT_TYPE
from . import mock_data
from .base import SAPBackend, SAPError, effective_unit_price
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
    WorkflowStep,
)

log = logging.getLogger(__name__)

_TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")

# ATP kontrol kuralinda rezervasyon ve emniyet stogu talep sayilir.
_ATP_RESPECT_SAFETY_STOCK = True
# Rezervasyonlarin ortalama ihtiyac tarihi (gun). Gercek sistemde RESB-BDTER.
_RESERVATION_HORIZON_DAYS = 14


def _normalize(text: str) -> str:
    return text.translate(_TR_MAP).lower()


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class MockSAPBackend(SAPBackend):
    """Bellekte tutulan, gercekci endustriyel malzeme verisiyle calisan SAP taklidi."""

    name = "mock"

    def __init__(self, settings) -> None:
        self.settings = settings
        #: Aktif tenant profili. Simulator de profili onemser: aksi halde
        #: profil davranisi yalniz gercek sistemde sinanabilirdi.
        self._profile: Any = None
        self._materials = {m["material_id"]: m for m in mock_data.MATERIALS}
        self._stock = {s["material_id"]: s for s in mock_data.STOCK}
        self._vendors = {v["vendor_id"]: v for v in mock_data.VENDORS}
        self._info_records = mock_data.INFO_RECORDS
        self._purchase_orders = list(mock_data.PURCHASE_ORDERS)
        self._project_costs = list(mock_data.PROJECT_COSTS)
        # Olusturulan PR'lar oturum boyunca saklanir; external_reference ile
        # timeout sonrasi mutabakat yapilabilir.
        self._created_requisitions: dict[str, dict] = {}
        self._reference_index: dict[str, str] = {}
        self._pr_counter = itertools.count(10_000_431)
        # Procure-to-pay zinciri
        self._requisition_links = list(mock_data.PURCHASE_REQUISITIONS)
        self._po_items = list(mock_data.PURCHASE_ORDER_ITEMS)
        self._schedule_lines = list(mock_data.SCHEDULE_LINES)
        self._goods_receipts = list(mock_data.GOODS_RECEIPTS)
        self._supplier_invoices = list(mock_data.SUPPLIER_INVOICES)
        self._workflow_steps = dict(mock_data.WORKFLOW_STEPS)
        self._payments = list(mock_data.PAYMENTS)

    # --- Malzeme ------------------------------------------------------------
    def _to_material(self, raw: dict, plant: str | None) -> Material:
        return Material(
            material_id=raw["material_id"],
            description=raw["description"],
            material_type=raw.get("material_type", "ROH"),
            material_group=raw.get("material_group", ""),
            base_unit=raw.get("base_unit", "ST"),
            gross_weight_kg=raw.get("gross_weight_kg"),
            procurement_type=raw.get("procurement_type", "F"),
            planned_delivery_days=raw.get("planned_delivery_days", 0),
            moving_avg_price=raw.get("moving_avg_price", 0.0),
            currency=self.settings.sap.currency,
            min_order_qty=raw.get("min_order_qty", 1),
            mrp_controller=raw.get("mrp_controller", ""),
            abc_indicator=raw.get("abc_indicator", ""),
            plant=plant or self.settings.sap.plant,
            attributes=raw.get("attributes", {}),
        )

    def search_materials(
        self,
        query: str = "",
        *,
        material_group: str | None = None,
        plant: str | None = None,
        attribute_filters: dict[str, tuple[float, float]] | None = None,
        limit: int = 20,
    ) -> list[Material]:
        tokens = [t for t in re.split(r"[\s,;]+", _normalize(query)) if t]
        results: list[tuple[int, dict]] = []

        for raw in self._materials.values():
            if material_group and raw.get("material_group") != material_group:
                continue

            if attribute_filters and not self._attributes_match(raw, attribute_filters):
                continue

            haystack = _normalize(
                f"{raw['material_id']} {raw['description']} {raw.get('material_group', '')} "
                f"{' '.join(str(v) for v in raw.get('attributes', {}).values())}"
            )
            if tokens:
                hits = sum(1 for t in tokens if t in haystack)
                if hits == 0:
                    continue
            else:
                hits = 1
            results.append((hits, raw))

        results.sort(key=lambda pair: (-pair[0], pair[1]["material_id"]))
        return [self._to_material(raw, plant) for _, raw in results[:limit]]

    @staticmethod
    def _attributes_match(raw: dict, filters: dict[str, tuple[float, float]]) -> bool:
        attrs = raw.get("attributes", {})
        for key, (low, high) in filters.items():
            value = attrs.get(key)
            if value is None or isinstance(value, str | list | bool):
                return False
            if not (low <= float(value) <= high):
                return False
        return True

    def get_material(self, material_id: str, *, plant: str | None = None) -> Material | None:
        raw = self._materials.get(material_id.upper())
        return self._to_material(raw, plant) if raw else None

    def get_material_classification(
        self, material_id: str, *, class_type: str = "001"
    ) -> MaterialClassification | None:
        raw = self._materials.get(material_id.upper())
        if raw is None:
            return None
        attributes = raw.get("attributes", {})
        return MaterialClassification(
            material_id=raw["material_id"],
            class_type=class_type,
            class_name=mock_data.CLASS_NAMES.get(raw.get("material_group", ""), "MATERIAL_GENERIC"),
            characteristics=dict(attributes),
            units={k: mock_data.CHARACTERISTIC_UNITS.get(k, "") for k in attributes},
            source="mock:classification",
        )

    def get_valuation(self, material_id: str, *, plant: str | None = None) -> dict | None:
        raw = self._materials.get(material_id.upper())
        if raw is None:
            return None
        return {
            "moving_avg_price": raw.get("moving_avg_price", 0.0),
            "standard_price": raw.get("moving_avg_price", 0.0),
            "currency": self.settings.sap.currency,
            "price_unit": 1,
            "source_api": "mock:valuation",
        }

    # --- Stok ---------------------------------------------------------------
    def get_stock(self, material_ids: list[str], *, plant: str | None = None) -> list[StockLevel]:
        out: list[StockLevel] = []
        target_plant = plant or self.settings.sap.plant
        for mid in material_ids:
            mid = mid.upper()
            if mid not in self._materials:
                continue
            raw = self._stock.get(mid, {})
            out.append(
                StockLevel(
                    material_id=mid,
                    plant=target_plant,
                    storage_location=self.settings.sap.storage_location,
                    unrestricted_qty=raw.get("unrestricted_qty", 0.0),
                    quality_inspection_qty=raw.get("quality_inspection_qty", 0.0),
                    blocked_qty=raw.get("blocked_qty", 0.0),
                    reserved_qty=raw.get("reserved_qty", 0.0),
                    on_order_qty=self._open_po_quantity(mid),
                    safety_stock=raw.get("safety_stock", 0.0),
                    unit=self._materials[mid].get("base_unit", "ST"),
                )
            )
        return out

    def _open_po_quantity(self, material_id: str) -> float:
        """Acik (henuz teslim edilmemis) siparis miktari.

        Siparis miktari degil, **acik** miktar toplanir: teslim edilen dusulur.
        """
        total = 0.0
        for raw in self._purchase_orders:
            if raw.get("material_id") != material_id:
                continue
            if raw.get("status") in {"delivered", "invoiced", "closed"}:
                continue
            total += max(0.0, raw.get("quantity", 0.0) - raw.get("delivered_qty", 0.0))
        return round(total, 3)

    # --- ATP ----------------------------------------------------------------
    def _supply_events(self, material_id: str) -> list[tuple[date, float, str]]:
        """Acik siparislerden gelen arz olaylari (tarih, miktar, element)."""
        events: list[tuple[date, float, str]] = []
        for raw in self._purchase_orders:
            if raw.get("material_id") != material_id:
                continue
            if raw.get("status") in {"delivered", "invoiced", "closed"}:
                continue
            open_qty = max(0.0, raw.get("quantity", 0.0) - raw.get("delivered_qty", 0.0))
            if open_qty <= 0:
                continue
            eta = _parse_date(raw.get("confirmed_delivery_date")) or _parse_date(
                raw.get("requested_delivery_date")
            )
            if eta is None:
                continue
            events.append((eta, open_qty, f"BE {raw['po_id']}"))
        events.sort(key=lambda item: item[0])
        return events

    def _best_lead_time(self, material_id: str) -> int:
        records = self.get_info_records(material_id)
        master = self._materials.get(material_id.upper(), {})
        return min(
            (r.planned_delivery_days for r in records),
            default=master.get("planned_delivery_days", 30),
        )

    def check_atp(
        self,
        material_id: str,
        *,
        quantity: float,
        requested_date: date | None = None,
        plant: str | None = None,
    ) -> AtpResult:
        mid = material_id.upper()
        master = self._materials.get(mid)
        if master is None:
            raise SAPError(
                f"Malzeme {mid} malzeme ana verisinde bulunamadi.", code="MM_MATNR_NOT_FOUND"
            )
        target_plant = plant or self.settings.sap.plant
        today = date.today()
        need_by = requested_date or today
        stock_raw = self._stock.get(mid, {})

        # ATP kontrol kurali: serbest stok - rezervasyon - emniyet stogu
        on_hand = float(stock_raw.get("unrestricted_qty", 0.0))
        reserved = float(stock_raw.get("reserved_qty", 0.0))
        safety = float(stock_raw.get("safety_stock", 0.0)) if _ATP_RESPECT_SAFETY_STOCK else 0.0
        available_now = max(0.0, on_hand - reserved - safety)

        messages: list[str] = []
        if safety > 0:
            messages.append(
                f"Kontrol kuralinda emniyet stogu ({safety:g}) talep olarak dusuldu."
            )
        if stock_raw.get("quality_inspection_qty"):
            messages.append(
                f"{stock_raw['quality_inspection_qty']:g} adet kalite kontrolde; ATP'ye dahil degil."
            )
        if stock_raw.get("blocked_qty"):
            messages.append(
                f"{stock_raw['blocked_qty']:g} adet bloke stok; ATP'ye dahil degil."
            )

        schedule: list[AtpScheduleLine] = []
        remaining = float(quantity)

        if available_now > 0 and remaining > 0:
            take = min(available_now, remaining)
            schedule.append(
                AtpScheduleLine(confirmed_date=today, confirmed_qty=round(take, 3),
                                supply_element="WB serbest stok")
            )
            remaining -= take

        for eta, qty, element in self._supply_events(mid):
            if remaining <= 0:
                break
            take = min(qty, remaining)
            schedule.append(
                AtpScheduleLine(confirmed_date=eta, confirmed_qty=round(take, 3),
                                supply_element=element)
            )
            remaining -= take

        if remaining > 0:
            # Yeni tedarik onerisi: dis tedarik icin bilgi kaydi, ic uretim icin
            # planlanan uretim suresi kullanilir.
            if master.get("procurement_type") == "E":
                lead = master.get("planned_delivery_days", 30)
                element = "Planli uretim emri onerisi"
            else:
                lead = self._best_lead_time(mid)
                element = "Yeni satinalma onerisi"
            proposal_date = today + timedelta(days=lead)
            schedule.append(
                AtpScheduleLine(confirmed_date=proposal_date, confirmed_qty=round(remaining, 3),
                                supply_element=element)
            )
            messages.append(
                f"{remaining:g} adet mevcut arzdan karsilanamiyor; {lead} gun tedarik/uretim "
                "suresiyle oneri uretildi."
            )
            remaining = 0.0

        confirmed_by_need_by = round(
            sum(line.confirmed_qty for line in schedule if line.confirmed_date <= need_by), 3
        )
        full_date = schedule[-1].confirmed_date if schedule else None

        return AtpResult(
            material_id=mid,
            plant=target_plant,
            requested_qty=float(quantity),
            requested_date=requested_date,
            unit=master.get("base_unit", "ST"),
            confirmed_qty=confirmed_by_need_by,
            full_confirmation_date=full_date,
            schedule_lines=schedule,
            checked_at=datetime.now(timezone.utc),
            source_api="mock:availability",
            calendar_considered=False,
            messages=messages,
        )

    # --- MRP ----------------------------------------------------------------
    def get_supply_demand(
        self,
        material_id: str,
        *,
        plant: str | None = None,
        horizon_days: int = 180,
    ) -> list[SupplyDemandItem]:
        mid = material_id.upper()
        master = self._materials.get(mid)
        if master is None:
            raise SAPError(
                f"Malzeme {mid} malzeme ana verisinde bulunamadi.", code="MM_MATNR_NOT_FOUND"
            )
        target_plant = plant or self.settings.sap.plant
        today = date.today()
        horizon = today + timedelta(days=horizon_days)
        unit = master.get("base_unit", "ST")
        raw = self._stock.get(mid, {})
        items: list[SupplyDemandItem] = []

        def add(element: str, qty: float, when: date, *, element_id: str = "", desc: str = "",
                wbs: str | None = None) -> None:
            if when > horizon:
                return
            items.append(
                SupplyDemandItem(
                    material_id=mid,
                    plant=target_plant,
                    mrp_element=element,
                    element_id=element_id,
                    availability_date=when,
                    quantity=round(qty, 3),
                    unit=unit,
                    description=desc,
                    wbs_element=wbs,
                )
            )

        if raw.get("unrestricted_qty"):
            add("WB", float(raw["unrestricted_qty"]), today, desc="Serbest kullanilabilir stok")
        if raw.get("quality_inspection_qty"):
            add("QM", 0.0, today,
                desc=f"{raw['quality_inspection_qty']:g} {unit} kalite kontrolde (ATP disi)")
        if raw.get("blocked_qty"):
            add("BLK", 0.0, today,
                desc=f"{raw['blocked_qty']:g} {unit} bloke stok (ATP disi)")
        if raw.get("safety_stock"):
            add("SH", -float(raw["safety_stock"]), today, desc="Emniyet stogu ihtiyaci")
        if raw.get("reserved_qty"):
            add(
                "VC",
                -float(raw["reserved_qty"]),
                today + timedelta(days=_RESERVATION_HORIZON_DAYS),
                desc="Proje/uretim rezervasyonu",
            )

        for po in self._purchase_orders:
            if po.get("material_id") != mid:
                continue
            if po.get("status") in {"delivered", "invoiced", "closed"}:
                continue
            open_qty = max(0.0, po.get("quantity", 0.0) - po.get("delivered_qty", 0.0))
            if open_qty <= 0:
                continue
            eta = _parse_date(po.get("confirmed_delivery_date")) or _parse_date(
                po.get("requested_delivery_date")
            )
            if eta is None:
                continue
            add(
                "BE",
                open_qty,
                eta,
                element_id=po["po_id"],
                desc=f"Acik satinalma siparisi ({po.get('vendor_id', '')})",
                wbs=po.get("wbs_element"),
            )

        items.sort(key=lambda i: (i.availability_date or today, -i.quantity))
        return items

    # --- Satinalma ----------------------------------------------------------
    def get_info_records(self, material_id: str, *, plant: str | None = None) -> list[InfoRecord]:
        mid = material_id.upper()
        records = []
        for raw in self._info_records:
            if raw["material_id"] != mid:
                continue
            vendor = self._vendors.get(raw["vendor_id"], {})
            records.append(
                InfoRecord(
                    material_id=mid,
                    vendor_id=raw["vendor_id"],
                    vendor_name=vendor.get("name", ""),
                    net_price=raw["net_price"],
                    currency=self.settings.sap.currency,
                    price_unit=raw.get("price_unit", 1),
                    min_order_qty=raw.get("min_order_qty", 1),
                    planned_delivery_days=raw.get("planned_delivery_days", 14),
                    incoterms=raw.get("incoterms", "DAP"),
                    payment_terms=raw.get("payment_terms", "NT30"),
                    valid_to=date(2026, 12, 31),
                    scale_prices=raw.get("scale_prices", {}),
                )
            )
        return records

    def get_vendor(self, vendor_id: str) -> Vendor | None:
        raw = self._vendors.get(vendor_id)
        return Vendor(**raw) if raw else None

    def get_supplier_score(
        self, vendor_id: str, *, purchasing_org: str | None = None
    ) -> SupplierScore | None:
        raw = self._vendors.get(vendor_id)
        if raw is None:
            return None
        vendor = Vendor(**raw)
        ppm = vendor.quality_ppm
        return SupplierScore(
            vendor_id=vendor_id,
            purchasing_org=purchasing_org or self.settings.sap.purch_org,
            overall_score=vendor.score(),
            price_score=round(vendor.price_competitiveness, 1),
            delivery_score=round(vendor.on_time_delivery_pct, 1),
            quantity_score=round(max(0.0, 100.0 - ppm / 100.0), 1),
            quality_score=round(max(0.0, 100.0 - ppm / 50.0), 1),
            service_score=round(vendor.responsiveness, 1),
            on_time_delivery_pct=vendor.on_time_delivery_pct,
            quality_ppm=ppm,
            evaluated_period="son 12 ay",
            source_api="mock:supplier_evaluation",
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
        priced_items: list[dict] = []
        diff: list[dict] = []
        total = 0.0

        for idx, item in enumerate(items, start=1):
            item_no = idx * 10
            mid = item.material_id.upper()
            master = self._materials.get(mid)
            if master is None:
                raise SAPError(
                    f"Malzeme {mid} malzeme ana verisinde bulunamadi.", code="MM_MATNR_NOT_FOUND"
                )
            if item.quantity <= 0:
                findings.append(
                    ValidationFinding(
                        severity="error", field="quantity", item_no=item_no,
                        message=f"Kalem {item_no}: miktar sifir veya negatif olamaz.",
                    )
                )

            records = self.get_info_records(mid)
            chosen = None
            if item.preferred_vendor:
                chosen = next((r for r in records if r.vendor_id == item.preferred_vendor), None)
                if chosen is None:
                    findings.append(
                        ValidationFinding(
                            severity="warning", field="preferred_vendor", item_no=item_no,
                            message=(
                                f"Kalem {item_no}: {item.preferred_vendor} icin bilgi kaydi yok, "
                                "en uygun tedarikci secildi."
                            ),
                        )
                    )
            if chosen is None and records:
                chosen = min(records, key=lambda r: r.price_for_qty(item.quantity))

            unit_price, price_warning = effective_unit_price(item.net_price, chosen.price_for_qty(item.quantity) if chosen else master["moving_avg_price"])
            if price_warning:
                findings.append(
                    ValidationFinding(
                        severity="warning", field="net_price", item_no=item_no,
                        message=f"Kalem {item_no}: {price_warning}",
                    )
                )
            line_total = round(unit_price * item.quantity, 2)
            total += line_total

            moq = chosen.min_order_qty if chosen else master.get("min_order_qty", 1)
            if item.quantity < moq:
                findings.append(
                    ValidationFinding(
                        severity="warning", field="quantity", item_no=item_no,
                        message=(
                            f"Kalem {item_no}: miktar {item.quantity:g} < minimum siparis miktari "
                            f"{moq:g} ({mid}). SAP bu kalemi uyari ile kabul eder."
                        ),
                    )
                )

            lead = chosen.planned_delivery_days if chosen else master.get("planned_delivery_days", 0)
            earliest = date.today() + timedelta(days=lead)
            if item.delivery_date and item.delivery_date < earliest:
                findings.append(
                    ValidationFinding(
                        severity="warning", field="delivery_date", item_no=item_no,
                        message=(
                            f"Kalem {item_no}: istenen teslim {item.delivery_date}, tedarik "
                            f"suresine gore en erken {earliest} ({lead} gun). Tarih gerceklesemez."
                        ),
                    )
                )
            if not (item.wbs_element or item.cost_center):
                findings.append(
                    ValidationFinding(
                        severity="warning", field="account_assignment", item_no=item_no,
                        message=(
                            f"Kalem {item_no}: hesap atamasi yok (WBS veya masraf merkezi). "
                            "Proje maliyeti izlenemez."
                        ),
                    )
                )

            priced_items.append(
                {
                    "item_no": item_no,
                    "material_id": mid,
                    "description": master["description"],
                    "quantity": item.quantity,
                    "unit": master.get("base_unit", "ST"),
                    "unit_price": round(unit_price, 2),
                    "line_total": line_total,
                    "currency": cfg.currency,
                    "vendor_id": chosen.vendor_id if chosen else None,
                    "vendor_name": chosen.vendor_name if chosen else None,
                    "lead_time_days": lead,
                    "earliest_delivery": earliest.isoformat(),
                    "requested_delivery": item.delivery_date.isoformat()
                    if item.delivery_date
                    else None,
                    "plant": item.plant or cfg.plant,
                    "wbs_element": item.wbs_element,
                    "cost_center": item.cost_center,
                }
            )
            diff.append(
                {
                    "item_no": item_no,
                    "action": "create",
                    "material_id": mid,
                    "quantity": item.quantity,
                    "unit_price": round(unit_price, 2),
                    "line_total": line_total,
                    "currency": cfg.currency,
                    "vendor_id": chosen.vendor_id if chosen else None,
                    "delivery_date": item.delivery_date.isoformat() if item.delivery_date else None,
                    "wbs_element": item.wbs_element,
                }
            )

        total = round(total, 2)
        payload = {
            "PurchaseRequisitionType": self.document_type,
            "PurchaseRequisitionHeaderText": header_text[:40],
            "PurchasingGroup": purchase_group or cfg.purch_group,
            "PurchasingOrganization": cfg.purch_org,
            "CompanyCode": cfg.company_code,
            "items": [
                {
                    "PurchaseRequisitionItem": f"{i['item_no']:05d}",
                    "Material": i["material_id"],
                    "Plant": i["plant"],
                    "RequestedQuantity": i["quantity"],
                    "BaseUnit": i["unit"],
                    "DeliveryDate": i["requested_delivery"],
                    "PurchaseRequisitionPrice": i["unit_price"],
                    "PurReqnItemCurrency": i["currency"],
                    "FixedSupplier": i["vendor_id"] or "",
                    "WBSElement": i["wbs_element"] or "",
                    "CostCenter": i["cost_center"] or "",
                }
                for i in priced_items
            ],
        }

        return PurchaseRequisitionDraft(
            draft_id=f"draft-{abs(hash(str(payload))) % 10**10:010d}",
            items=priced_items,
            header_text=header_text,
            purchase_group=purchase_group or cfg.purch_group,
            purchasing_org=cfg.purch_org,
            plant=cfg.plant,
            total_value=total,
            currency=cfg.currency,
            payload=payload,
            findings=findings,
            diff=diff,
            source_api="mock:purchase_requisition",
            requires_human_approval=total > cfg.approval_threshold,
        )

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

        # Ayni referansla daha once olusturulmus PR varsa yenisi yaratilmaz.
        existing_id = self._reference_index.get(external_reference)
        if existing_id:
            stored = self._created_requisitions[existing_id]
            return PurchaseRequisitionResult(
                requisition_id=existing_id,
                created=False,
                dry_run=False,
                total_value=stored["total"],
                currency=draft.currency,
                items=stored["items"],
                messages=[
                    f"Bu referansla ({external_reference}) daha once {existing_id} olusturuldu."
                ],
                external_reference=external_reference,
            )

        pr_id = str(next(self._pr_counter))
        self._created_requisitions[pr_id] = {
            "header_text": draft.header_text,
            "purchase_group": draft.purchase_group,
            "items": draft.items,
            "total": draft.total_value,
            "external_reference": external_reference,
            "correlation_id": correlation_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._reference_index[external_reference] = pr_id
        log.info(
            "Mock PR olusturuldu: %s / %s %s (ref %s)",
            pr_id, draft.total_value, draft.currency, external_reference,
        )
        return PurchaseRequisitionResult(
            requisition_id=pr_id,
            created=True,
            dry_run=False,
            requires_human_approval=False,
            total_value=draft.total_value,
            currency=draft.currency,
            items=draft.items,
            messages=[f"Satinalma talebi {pr_id} olusturuldu (EBAN)."],
            external_reference=external_reference,
            etag=f'W/"mock-{pr_id}"',
        )

    def read_purchase_requisition(self, requisition_id: str) -> dict | None:
        stored = self._created_requisitions.get(requisition_id)
        if stored is None:
            return None
        return {
            "PurchaseRequisition": requisition_id,
            "PurchaseRequisitionHeaderText": stored["header_text"],
            "PurchasingGroup": stored["purchase_group"],
            "item_count": len(stored["items"]),
            "total_value": stored["total"],
            "external_reference": stored["external_reference"],
            "created_at": stored["created_at"],
            "items": stored["items"],
        }

    def find_purchase_requisition_by_reference(
        self, external_reference: str
    ) -> tuple[str, dict] | None:
        pr_id = self._reference_index.get(external_reference)
        if pr_id is None:
            return None
        record = self.read_purchase_requisition(pr_id)
        return (pr_id, record) if record else None

    def get_purchase_orders(
        self,
        *,
        material_id: str | None = None,
        vendor_id: str | None = None,
        wbs_element: str | None = None,
        only_open: bool = False,
        limit: int = 50,
    ) -> list[PurchaseOrder]:
        out: list[PurchaseOrder] = []
        for raw in self._purchase_orders:
            if material_id and raw["material_id"] != material_id.upper():
                continue
            if vendor_id and raw["vendor_id"] != vendor_id:
                continue
            if wbs_element and raw.get("wbs_element") != wbs_element:
                continue
            if only_open and raw["status"] in {"delivered", "invoiced", "closed"}:
                continue
            vendor = self._vendors.get(raw["vendor_id"], {})
            out.append(
                PurchaseOrder(
                    po_id=raw["po_id"],
                    vendor_id=raw["vendor_id"],
                    vendor_name=vendor.get("name", ""),
                    created_on=_parse_date(raw.get("created_on")),
                    currency=self.settings.sap.currency,
                    net_value=raw.get("net_value", 0.0),
                    status=raw.get("status", "open"),
                    material_id=raw.get("material_id", ""),
                    description=raw.get("description", ""),
                    quantity=raw.get("quantity", 0.0),
                    delivered_qty=raw.get("delivered_qty", 0.0),
                    confirmed_delivery_date=_parse_date(raw.get("confirmed_delivery_date")),
                    requested_delivery_date=_parse_date(raw.get("requested_delivery_date")),
                    wbs_element=raw.get("wbs_element"),
                )
            )
        return out[:limit]

    # --- Procure-to-pay gorunurlugu -----------------------------------------
    def get_purchase_order_items(self, po_id: str) -> list[PurchaseOrderItem]:
        currency = self.settings.sap.currency
        plant = self.settings.sap.plant
        return [
            PurchaseOrderItem(
                po_id=raw["po_id"],
                item_no=raw["item_no"],
                material_id=raw.get("material_id", ""),
                description=raw.get("description", ""),
                plant=plant,
                quantity=raw.get("quantity", 0.0),
                unit=raw.get("unit", "ST"),
                net_price=raw.get("net_price", 0.0),
                net_value=raw.get("net_value", 0.0),
                currency=currency,
                delivered_qty=raw.get("delivered_qty", 0.0),
                invoiced_qty=raw.get("invoiced_qty", 0.0),
                wbs_element=raw.get("wbs_element"),
                account_assignment=raw.get("account_assignment", ""),
            )
            for raw in self._po_items
            if raw["po_id"] == po_id.strip()
        ]

    def get_schedule_lines(self, po_id: str, *, item_no: str = "") -> list[ScheduleLine]:
        out: list[ScheduleLine] = []
        for raw in self._schedule_lines:
            if raw["po_id"] != po_id.strip():
                continue
            if item_no and raw["item_no"] != item_no:
                continue
            out.append(
                ScheduleLine(
                    po_id=raw["po_id"],
                    item_no=raw["item_no"],
                    schedule_line=raw.get("schedule_line", "0001"),
                    requested_date=_parse_date(raw.get("requested_date")),
                    confirmed_date=_parse_date(raw.get("confirmed_date")),
                    quantity=raw.get("quantity", 0.0),
                    delivered_qty=raw.get("delivered_qty", 0.0),
                    unit=raw.get("unit", "ST"),
                )
            )
        out.sort(key=lambda line: (line.item_no, line.schedule_line))
        return out

    def get_goods_receipts(
        self, *, po_id: str = "", material_id: str = "", limit: int = 50
    ) -> list[GoodsReceipt]:
        out: list[GoodsReceipt] = []
        for raw in self._goods_receipts:
            if po_id and raw.get("po_id") != po_id.strip():
                continue
            if material_id and raw.get("material_id") != material_id.upper():
                continue
            out.append(
                GoodsReceipt(
                    material_document=raw["material_document"],
                    document_year=raw.get("document_year", 0),
                    item_no=raw.get("item_no", "0001"),
                    posting_date=_parse_date(raw.get("posting_date")),
                    movement_type=raw.get("movement_type", "101"),
                    material_id=raw.get("material_id", ""),
                    plant=self.settings.sap.plant,
                    quantity=raw.get("quantity", 0.0),
                    unit=raw.get("unit", "ST"),
                    po_id=raw.get("po_id", ""),
                    po_item=raw.get("po_item", ""),
                    batch=raw.get("batch", ""),
                )
            )
        out.sort(key=lambda gr: (gr.posting_date or date.min, gr.material_document))
        return out[:limit]

    def get_supplier_invoices(
        self,
        *,
        invoice_id: str = "",
        po_id: str = "",
        vendor_id: str = "",
        only_blocked: bool = False,
        limit: int = 50,
    ) -> list[SupplierInvoice]:
        out: list[SupplierInvoice] = []
        for raw in self._supplier_invoices:
            if invoice_id and raw["invoice_id"] != invoice_id.strip():
                continue
            if po_id and po_id.strip() not in raw.get("po_ids", []):
                continue
            if vendor_id and raw.get("vendor_id") != vendor_id:
                continue
            vendor = self._vendors.get(raw.get("vendor_id", ""), {})
            blocks = [
                InvoiceBlock(
                    invoice_id=raw["invoice_id"],
                    currency=self.settings.sap.currency,
                    **{k: v for k, v in block.items()},
                )
                for block in raw.get("blocks", [])
            ]
            invoice = SupplierInvoice(
                invoice_id=raw["invoice_id"],
                fiscal_year=raw.get("fiscal_year", 0),
                vendor_id=raw.get("vendor_id", ""),
                vendor_name=vendor.get("name", ""),
                company_code=raw.get("company_code", self.settings.sap.company_code),
                invoice_date=_parse_date(raw.get("invoice_date")),
                posting_date=_parse_date(raw.get("posting_date")),
                due_date=_parse_date(raw.get("due_date")),
                gross_amount=raw.get("gross_amount", 0.0),
                net_amount=raw.get("net_amount", 0.0),
                tax_amount=raw.get("tax_amount", 0.0),
                currency=self.settings.sap.currency,
                status=raw.get("status", "posted"),
                payment_block=raw.get("payment_block", ""),
                payment_terms=raw.get("payment_terms", ""),
                paid_on=_parse_date(raw.get("paid_on")),
                accounting_document=raw.get("accounting_document", ""),
                po_ids=list(raw.get("po_ids", [])),
                blocks=blocks,
                source_api="mock:supplier_invoice",
            )
            if only_blocked and not invoice.is_blocked:
                continue
            out.append(invoice)
        out.sort(key=lambda inv: (inv.posting_date or date.min), reverse=True)
        return out[:limit]

    def get_workflow_status(self, *, object_type: str, object_id: str) -> list[WorkflowStep]:
        raw_steps = self._workflow_steps.get(f"{object_type}:{object_id.strip()}", [])
        steps = [
            WorkflowStep(
                workflow_id=raw["workflow_id"],
                step_no=raw.get("step_no", 0),
                step_name=raw.get("step_name", ""),
                status=raw.get("status", "in_progress"),
                decision=raw.get("decision", ""),
                processor_name=raw.get("processor_name", ""),
                processor_role=raw.get("processor_role", ""),
                started_at=_parse_datetime(raw.get("started_at")),
                completed_at=_parse_datetime(raw.get("completed_at")),
                due_at=_parse_datetime(raw.get("due_at")),
                note=raw.get("note", ""),
            )
            for raw in raw_steps
        ]
        steps.sort(key=lambda step: step.step_no)
        return steps

    def get_document_flow(
        self,
        document_id: str,
        *,
        document_type: str = "auto",
        include_payments: bool = True,
    ) -> list[DocumentFlowNode]:
        """PR -> PO -> mal kabul -> fatura -> odeme zinciri.

        Zincir **her zaman PO uzerinden** kurulur: PO, P2P akisindaki tek
        dugumdur ki hem yukari (PR) hem asagi (GR, fatura) referans tasir.
        Girdi hangi belge olursa olsun once ilgili PO'lar bulunur; bir PO'ya
        baglanamayan belge icin bos zincir doner - tahmin uretilmez.
        """
        target = document_id.strip()
        if not target:
            raise SAPError("Belge numarasi bos olamaz.", code="DOC_ID_REQUIRED")

        resolved = document_type if document_type != "auto" else self._detect_type(target)
        po_ids = self._po_ids_for(resolved, target)
        if not po_ids:
            return []

        nodes: list[DocumentFlowNode] = []
        for po_id in sorted(po_ids):
            nodes.extend(self._flow_for_po(po_id, include_payments=include_payments))
        return nodes

    def _detect_type(self, document_id: str) -> str:
        """Belge numarasi araligindan tipi cikarir (SAP numara araligi mantigi)."""
        if any(raw["po_id"] == document_id for raw in self._po_items):
            return "purchase_order"
        if any(raw["requisition_id"] == document_id for raw in self._requisition_links):
            return "purchase_requisition"
        if any(raw["invoice_id"] == document_id for raw in self._supplier_invoices):
            return "supplier_invoice"
        if any(raw["material_document"] == document_id for raw in self._goods_receipts):
            return "goods_receipt"
        return "unknown"

    def _po_ids_for(self, document_type: str, document_id: str) -> set[str]:
        if document_type == "purchase_order":
            return {document_id} if any(
                raw["po_id"] == document_id for raw in self._po_items
            ) else set()
        if document_type == "purchase_requisition":
            return {
                raw["po_id"]
                for raw in self._requisition_links
                if raw["requisition_id"] == document_id and raw.get("po_id")
            }
        if document_type == "supplier_invoice":
            return {
                po_id
                for raw in self._supplier_invoices
                if raw["invoice_id"] == document_id
                for po_id in raw.get("po_ids", [])
            }
        if document_type == "goods_receipt":
            return {
                raw["po_id"]
                for raw in self._goods_receipts
                if raw["material_document"] == document_id and raw.get("po_id")
            }
        return set()

    def _flow_for_po(self, po_id: str, *, include_payments: bool) -> list[DocumentFlowNode]:
        currency = self.settings.sap.currency
        nodes: list[DocumentFlowNode] = []

        # 1. PR -> PO (EKPO-BANFN)
        for raw in self._requisition_links:
            if raw.get("po_id") != po_id:
                continue
            nodes.append(
                DocumentFlowNode(
                    document_type="purchase_requisition",
                    document_id=raw["requisition_id"],
                    item_no=raw.get("item_no", ""),
                    document_date=_parse_date(raw.get("created_on")),
                    status=raw.get("status", ""),
                    quantity=raw.get("quantity"),
                    unit="ST",
                    linked_by="EKPO-BANFN",
                    source_api="mock:purchase_requisition",
                )
            )

        # 2. PO kalemleri
        header = next((p for p in self._purchase_orders if p["po_id"] == po_id), {})
        for item in self.get_purchase_order_items(po_id):
            predecessor = next(
                (
                    r["requisition_id"]
                    for r in self._requisition_links
                    if r.get("po_id") == po_id
                ),
                "",
            )
            nodes.append(
                DocumentFlowNode(
                    document_type="purchase_order",
                    document_id=item.po_id,
                    item_no=item.item_no,
                    document_date=_parse_date(header.get("created_on")),
                    status=header.get("status", ""),
                    quantity=item.quantity,
                    unit=item.unit,
                    amount=item.net_value,
                    currency=currency,
                    linked_by="EKKO-EBELN",
                    predecessor_id=predecessor,
                    source_api="mock:purchase_order",
                    notes=(
                        [f"Acik miktar {item.open_qty:g} {item.unit}"]
                        if item.open_qty
                        else []
                    ),
                )
            )

        # 3. Mal kabul (MSEG-EBELN)
        for receipt in self.get_goods_receipts(po_id=po_id):
            nodes.append(
                DocumentFlowNode(
                    document_type="goods_receipt",
                    document_id=receipt.material_document,
                    item_no=receipt.item_no,
                    document_date=receipt.posting_date,
                    status="reversal" if receipt.is_reversal else "posted",
                    quantity=receipt.quantity,
                    unit=receipt.unit,
                    linked_by="MSEG-EBELN",
                    predecessor_id=receipt.po_id,
                    source_api="mock:material_document",
                    notes=(
                        [f"Hareket tipi {receipt.movement_type} (iptal)"]
                        if receipt.is_reversal
                        else [f"Hareket tipi {receipt.movement_type}"]
                    ),
                )
            )

        # 4. Fatura (RSEG-EBELN) ve 5. odeme
        for invoice in self.get_supplier_invoices(po_id=po_id):
            nodes.append(
                DocumentFlowNode(
                    document_type="supplier_invoice",
                    document_id=invoice.invoice_id,
                    document_date=invoice.posting_date,
                    status=invoice.status,
                    amount=invoice.gross_amount,
                    currency=currency,
                    linked_by="RSEG-EBELN",
                    predecessor_id=po_id,
                    source_api="mock:supplier_invoice",
                    notes=(
                        [f"Odeme blokaji '{invoice.payment_block}'"]
                        if invoice.payment_block
                        else []
                    ),
                )
            )
            if not include_payments:
                continue
            for payment in self._payments:
                if payment.get("invoice_id") != invoice.invoice_id:
                    continue
                nodes.append(
                    DocumentFlowNode(
                        document_type="payment",
                        document_id=payment["payment_id"],
                        document_date=_parse_date(payment.get("posting_date")),
                        status=payment.get("status", ""),
                        amount=payment.get("amount"),
                        currency=currency,
                        linked_by="BSAK-AUGBL",
                        predecessor_id=invoice.invoice_id,
                        source_api="mock:payment",
                    )
                )
        return nodes

    # --- Kontrolling --------------------------------------------------------
    def get_project_costs(
        self, *, wbs_element: str | None = None, fiscal_year: int | None = None
    ) -> list[ProjectCost]:
        out: list[ProjectCost] = []
        for raw in self._project_costs:
            if wbs_element and not raw["wbs_element"].startswith(wbs_element):
                continue
            if fiscal_year and raw.get("fiscal_year") != fiscal_year:
                continue
            out.append(ProjectCost(currency=self.settings.sap.currency, **raw))
        return out

    def set_active_profile(self, profile: Any) -> None:
        self._profile = profile

    @property
    def document_type(self) -> str:
        """Belge tipi profilden gelir; yoksa SAP standardi."""
        return getattr(self._profile, "document_type", None) or DEFAULT_DOCUMENT_TYPE

    def ping(self) -> dict[str, str]:
        return {
            "backend": "mock",
            "status": "ok",
            "materials": str(len(self._materials)),
            "vendors": str(len(self._vendors)),
            "open_pos": str(sum(1 for p in self._purchase_orders if p["status"] == "open")),
            "note": "Gercek SAP icin .env dosyasinda SAP_BACKEND=odata yapin.",
        }
