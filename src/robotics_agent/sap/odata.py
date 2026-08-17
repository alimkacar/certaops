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
from .base import SAPBackend, effective_unit_price
from .models import (
    AtpResult,
    AtpScheduleLine,
    InfoRecord,
    Material,
    MaterialClassification,
    ProjectCost,
    PurchaseOrder,
    PurchaseRequisitionDraft,
    PurchaseRequisitionItem,
    PurchaseRequisitionResult,
    StockLevel,
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
        "to_Plant/PlndDelryDurnInDays,to_Plant/MinimumLotSizeQuantity,"
        "to_Plant/MRPController,to_Plant/ABCIndicator"
    ),
    "valuation": (
        "Material,ValuationArea,MovingAveragePrice,StandardPrice,Currency,PriceUnitQty"
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
        "to_PurgInfoRecdOrgPlantData/PaymentTerms,"
        "to_PurgInfoRecdOrgPlantData/PriceValidityEndDate"
    ),
    # A_Supplier'da vergi/banka/adres alanlari BILEREK yok: satinalma karari
    # icin gerekli degiller ve D2/D3 sinifindalar.
    "supplier": "Supplier,SupplierName,SupplierFullName,Country,CityName,"
    "PurchasingIsBlockedForSupplier",
    "supplier_score": (
        "Supplier,PurchasingOrganization,OverallScore,PriceScore,DeliveryScore,"
        "QuantityScore,QualityScore"
    ),
    "classification": "Product,Characteristic,CharcInternalID,CharcValue,"
    "CharcFromNumericValue,CharcValueUnit,ClassTypeInternalID",
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
            allowed_hosts=allowed,
            token_provider=self.connection.token_provider,
            breaker=self.breaker,
        )
        # V2 ve V4 ayni HTTP baglantisini paylasir; yalniz $format/`d` farki degisir.
        self._core_v2 = ODataHttpCore(
            client=http_client,
            odata_version="v2",
            sap_client=cfg.client,
            allowed_hosts=allowed,
            token_provider=self.connection.token_provider,
            breaker=self.breaker,
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
                    "$filter": " or ".join(f"substringof('{quote(t)}',Product)" for t in tokens),
                    "$select": "Product",
                    "$top": limit * 2,
                },
            )
            seen: set[str] = set()
            for candidate in described + [r.get("Product", "") for r in by_id]:
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    product_ids.append(candidate)

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
                "$expand": "to_Description,to_Plant",
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
        clauses = " or ".join(f"substringof('{quote(t)}',ProductDescription)" for t in tokens)
        rows = self.v2.read(
            service,
            "A_ProductDescription",
            params={"$filter": clauses, "$select": "Product,ProductDescription", "$top": limit},
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
        plant_row = next(
            (p for p in plants if p.get("Plant") == target_plant), plants[0] if plants else {}
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
            planned_delivery_days=int(plant_row.get("PlndDelryDurnInDays") or 0),
            moving_avg_price=0.0,  # get_valuation ile ayrica okunur
            currency=self.settings.sap.currency,
            min_order_qty=float(plant_row.get("MinimumLotSizeQuantity") or 1),
            mrp_controller=plant_row.get("MRPController", ""),
            abc_indicator=plant_row.get("ABCIndicator", ""),
            plant=target_plant,
            attributes={},
        )

    def get_material(self, material_id: str, *, plant: str | None = None) -> Material | None:
        rows = self.v2.read(
            service_path("product"),
            f"A_Product({escape_key(material_id)})",
            params={
                "$expand": "to_Description,to_Plant",
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
                "$expand": "to_Description,to_Plant",
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
        id_filter = _in_filter("Material", ids)
        try:
            rows = self.v2.read(
                capability.service_path,
                "A_MaterialValuation",
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
            material_id = str(row.get("Material", ""))
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
                    f"ClassTypeInternalID eq '{quote(class_type)}'"
                ),
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
            unit = row.get("CharcValueUnit") or row.get("Unit") or ""
            key = str(name).strip().lower()
            characteristics[key] = _coerce_number(raw_value)
            if unit:
                units[key] = str(unit)

        class_rows = self.v2.read(
            capability.service_path,
            "A_ProductClass",
            params={"$filter": f"Product eq '{quote(material_id)}'", "$top": 5},
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
        for material_id in ids:
            # Rezervasyon ve emniyet stogu MRP tarafindan gelir; stok servisi vermez.
            reservations = self._reservation_quantity(material_id, target_plant)
            if reservations is not None:
                levels[material_id].reserved_qty = reservations
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
            expand=("_PurchaseOrderScheduleLine",),
            top=200,
        )
        for row in page.rows:
            material_id = _bucket(row)
            if material_id is None:
                continue
            ordered = float(row.get("OrderQuantity") or 0)
            delivered = sum(
                float(line.get("ScheduleLineDeliveredQuantity") or 0)
                for line in _nav_rows(row, "_PurchaseOrderScheduleLine")
            )
            totals[material_id] += max(0.0, ordered - delivered)
        return {k: round(v, 3) for k, v in totals.items()}

    def _reservation_quantity(self, material_id: str, plant: str) -> float | None:
        """MRP arz/talep elementlerinden rezervasyon toplamini cikarir."""
        try:
            items = self.get_supply_demand(material_id, plant=plant, horizon_days=365)
        except SAPError:
            return None
        return round(
            sum(-item.quantity for item in items if item.mrp_element in {"VC", "MS", "AR"}), 3
        )

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
        items: list[SupplyDemandItem] = []
        for row in rows:
            when = parse_odata_datetime(row.get("MRPAvailabilityDate"))
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
        # `$select` olmadan A_Supplier vergi numarasi, banka ve adres alanlarini
        # da dondururdu; satinalma karari icin gerekli degiller.
        rows = self.v2.read(
            service_path("supplier"),
            f"A_Supplier({escape_key(vendor_id)})",
            params={"$select": SELECT_FIELDS["supplier"]},
        )
        if not rows:
            return None
        row = rows[0]
        vendor = Vendor(
            vendor_id=row.get("Supplier", vendor_id),
            name=row.get("SupplierName") or row.get("SupplierFullName", ""),
            country=row.get("Country", ""),
            city=row.get("CityName", ""),
            blocked=bool(row.get("PurchasingIsBlockedForSupplier")),
        )
        # Performans alanlari standart tedarikci API'sinde yok; degerlendirme
        # CDS'inden gelirse doldurulur, gelmezse 0 kalir ve tool bunu isaretler.
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
        capability = CAPABILITY_MANIFEST["supplier_score"]
        org = purchasing_org or self.settings.sap.purch_org
        try:
            rows = self.v2.read(
                capability.service_path,
                "A_SupplierOplScoresAv",
                params={
                    "$filter": (
                        f"Supplier eq '{quote(vendor_id)}' and "
                        f"PurchasingOrganization eq '{quote(org)}'"
                    ),
                    "$top": 5,
                },
            )
        except SAPError as exc:
            log.info("Tedarikci skoru okunamadi (%s): %s", vendor_id, exc)
            return SupplierScore(
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
        if not rows:
            return SupplierScore(
                vendor_id=vendor_id,
                purchasing_org=org,
                source_api=capability.service_path,
                estimated_fields=["overall_score", "delivery_score", "quality_score"],
            )
        row = rows[0]
        return SupplierScore(
            vendor_id=vendor_id,
            purchasing_org=org,
            overall_score=_opt_float(row.get("OverallScore")),
            price_score=_opt_float(row.get("PriceScore")),
            delivery_score=_opt_float(row.get("DeliveryScore")),
            quantity_score=_opt_float(row.get("QuantityScore")),
            quality_score=_opt_float(row.get("QualityScore")),
            service_score=_opt_float(row.get("ServiceScore")),
            on_time_delivery_pct=_opt_float(row.get("OnTimeDeliveryPercentage")),
            quality_ppm=int(row["QualityPPM"]) if row.get("QualityPPM") else None,
            evaluated_period=str(row.get("EvaluationPeriod") or ""),
            source_api=capability.service_path,
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
            "PurchaseRequisitionType": "NB",
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
                "PurchaseRequisitionType": draft.payload.get("PurchaseRequisitionType", "NB"),
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
                "PurchaseRequisitionType": draft.payload.get("PurchaseRequisitionType", "NB"),
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
            "PurchaseRequisitionType": draft.payload.get("PurchaseRequisitionType", "NB"),
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
        if wbs_element:
            filters.append(f"WBSElement eq '{quote(wbs_element)}'")
        if only_open:
            filters.append("IsCompletelyDelivered eq false")

        if capability.odata_version == "v2":
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

        # Baslik $expand ile geliyor: baslik basina ek GET yok (N+1 kalkti).
        page = self.v4.read_collection(
            capability.service_path,
            "PurchaseOrderItem",
            filter_expr=" and ".join(filters),
            expand=("_PurchaseOrder", "_PurchaseOrderScheduleLine"),
            top=limit,
        )

        out: list[PurchaseOrder] = []
        for row in page.rows:
            header = _nav_single(row, "_PurchaseOrder")
            supplier = header.get("Supplier", "")
            if vendor_id and supplier != vendor_id:
                continue

            schedule = _nav_rows(row, "_PurchaseOrderScheduleLine")
            ordered = float(row.get("OrderQuantity") or 0)
            delivered = sum(
                float(s.get("ScheduleLineDeliveredQuantity") or 0) for s in schedule
            )
            requested = _weighted_date(schedule, "ScheduleLineDeliveryDate")
            confirmed = (
                _weighted_date(schedule, "PurchaseOrderConfirmedDeliveryDate") or requested
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
                    wbs_element=row.get("WBSElement") or None,
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
