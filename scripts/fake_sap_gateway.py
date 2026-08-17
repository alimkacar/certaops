#!/usr/bin/env python3
"""Gercek SAP olmadan tum HTTP yolunu calistiran sahte SAP Gateway.

Neden gerekli
-------------
Birim testleri `httpx.MockTransport` kullanir: hizlidir ama **gercek bir HTTP
katmani yoktur**. Baglanti kurulumu, CSRF token akisi, proxy, TLS dogrulamasi,
timeout ve yeniden baglanma davranisi mock transport ile hic denenmez.

Bu script gercek bir TCP soketi acar ve S/4HANA'nin OData V2 + V4 uclarini
taklit eder. Boylece `SAP_BACKEND=odata` ile, gercek bir sisteme baglanmadan:

  - CSRF fetch + POST akisi,
  - deep insert govdesinin tam olarak nasil gittigi,
  - read-after-write ve idempotency mutabakati,
  - egress allowlist ve 401 sonrasi yeniden baglanma

uctan uca calistirilabilir.

Kullanim
--------
    python scripts/fake_sap_gateway.py                 # 127.0.0.1:8099
    python scripts/fake_sap_gateway.py --port 9000
    python scripts/fake_sap_gateway.py --no-v4         # V4 servisleri kapali
                                                       # (V2 fallback'i dener)
    python scripts/fake_sap_gateway.py --expire-auth   # ilk istekte 401 doner
                                                       # (yeniden baglanmayi dener)

Sonra baska bir terminalde:

    SAP_BACKEND=odata \\
    SAP_BASE_URL=http://127.0.0.1:8099 \\
    SAP_ALLOWED_HOSTS=127.0.0.1 \\
    SAP_AUTH_MODE=basic SAP_USERNAME=svc SAP_PASSWORD=pw \\
    SAP_DRY_RUN=false \\
    python scripts/smoke_odata.py

UYARI: Bu bir test kuklasidir, SAP simulatoru degil. Is kurallarini
dogrulamaz; yalniz sozlesmenin sekline uyan yanitlar dondurur. "Burada gecti"
demek "gercek sistemde gecer" demek DEGILDIR - kontrat dogrulamasi icin
`sap_discover_capabilities` (probe=true) hedef sisteme karsi calistirilmalidir.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("fake-sap")

# --- Servis yollari (manifest ile birebir) ----------------------------------
PRODUCT = "/sap/opu/odata/sap/API_PRODUCT_SRV"
VALUATION = "/sap/opu/odata/sap/API_MATERIAL_VALUATION_SRV"
STOCK = "/sap/opu/odata/sap/API_MATERIAL_STOCK_SRV"
INFORECORD = "/sap/opu/odata/sap/API_INFORECORD_PROCESS_SRV"
SUPPLIER = "/sap/opu/odata/sap/API_BUSINESS_PARTNER"
PR_V4 = "/sap/opu/odata4/sap/api_purchaserequisition_2/srvd_a2x/sap/purchaserequisition/0001"
PR_V2 = "/sap/opu/odata/sap/API_PURCHASEREQ_PROCESS_SRV"
PO_V4 = "/sap/opu/odata4/sap/api_purchaseorder_2/srvd_a2x/sap/purchaseorder/0001"

CSRF_TOKEN = "FAKE-CSRF-TOKEN"

MATERIALS = ("MAT-1", "MAT-2", "MAT-3")


def _edmx(
    entity_sets: dict[str, tuple[str, ...]],
    navigations: dict[str, tuple[tuple[str, str], ...]] | None = None,
) -> str:
    """Verilen entity set + alan + navigation listesinden EDMX uretir.

    Kontrat dogrulamasi ($metadata okuyup alan/navigation arayan kod yolu)
    gercek bir belge gormeli; bos `<edmx/>` dondurmek o yolu test etmezdi.
    Navigation'lar onemli: yazma govdesinin ic ice yapisi buradan turetilir.
    """
    navigations = navigations or {}
    types = "".join(
        f'<EntityType Name="{name}Type">'
        + "".join(f'<Property Name="{p}"/>' for p in props)
        + "".join(
            f'<NavigationProperty Name="{nav}" Type="Collection(x.{target}Type)"/>'
            for nav, target in navigations.get(name, ())
        )
        + "</EntityType>"
        for name, props in entity_sets.items()
    )
    sets = "".join(
        f'<EntitySet Name="{name}" EntityType="x.{name}Type"/>' for name in entity_sets
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">'
        '<edmx:DataServices>'
        '<Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="x">'
        f"{types}<EntityContainer Name=\"Container\">{sets}</EntityContainer>"
        "</Schema></edmx:DataServices></edmx:Edmx>"
    )


METADATA: dict[str, str] = {
    # Released S/4HANA PR servisinin sekli: hesap atamasi AYRI bir alt
    # entity'dir, kalem yalniz kategoriyi tasir.
    PR_V4: _edmx(
        {
            "PurchaseRequisition": ("PurchaseRequisition", "PurchaseRequisitionType",
                                    "PurchaseRequisitionHeaderText"),
            "PurchaseRequisitionItem": (
                "PurchaseRequisition", "PurchaseRequisitionItem", "Material", "Plant",
                "RequestedQuantity", "BaseUnit", "DeliveryDate",
                "PurchaseRequisitionPrice", "PurReqnItemCurrency", "PurchasingGroup",
                "PurchasingOrganization", "CompanyCode", "FixedSupplier",
                "PurchaseRequisitionItemText", "AccountAssignmentCategory",
            ),
            "PurchaseReqnAcctAssgmt": (
                "PurchaseRequisition", "PurchaseRequisitionItem",
                "PurchaseRequisitionAcctAssgmt", "WBSElement", "CostCenter",
            ),
        },
        navigations={
            "PurchaseRequisition": (("_PurchaseRequisitionItem", "PurchaseRequisitionItem"),),
            "PurchaseRequisitionItem": (
                ("_PurchaseReqnAcctAssgmt", "PurchaseReqnAcctAssgmt"),
            ),
        },
    ),
    PO_V4: _edmx(
        {
            "PurchaseOrder": ("PurchaseOrder", "Supplier", "CreationDate", "DocumentCurrency"),
            "PurchaseOrderItem": (
                "PurchaseOrder", "PurchaseOrderItem", "Material", "Plant",
                "OrderQuantity", "NetPriceAmount", "WBSElement",
            ),
            "PurchaseOrderScheduleLine": (
                "PurchaseOrder", "PurchaseOrderItem", "ScheduleLineOrderQuantity",
                "ScheduleLineDeliveryDate", "PurchaseOrderQuantityUnit",
            ),
        }
    ),
    # V2 fallback yolu da dogrulanabilsin diye deprecated servislerin
    # sozlesmesi de sunulur.
    PR_V2: _edmx(
        {
            "A_PurchaseRequisitionHeader": (
                "PurchaseRequisition", "PurchaseRequisitionType",
                "PurchaseRequisitionHeaderText",
            ),
            "A_PurchaseReqnItem": (
                "PurchaseRequisition", "PurchaseRequisitionItem", "Material", "Plant",
                "RequestedQuantity", "BaseUnit", "DeliveryDate",
                "PurchaseRequisitionPrice", "PurReqnItemCurrency", "PurchasingGroup",
                "PurchasingOrganization", "CompanyCode", "FixedSupplier",
                "PurchaseRequisitionItemText", "AccountAssignmentCategory",
            ),
            "A_PurRequisitionAcctAssgmt": (
                "PurchaseRequisition", "PurchaseRequisitionItem",
                "PurchaseRequisitionAcctAssgmt", "WBSElement", "CostCenter",
            ),
        },
        navigations={
            "A_PurchaseRequisitionHeader": (
                ("to_PurchaseReqnItem", "A_PurchaseReqnItem"),
            ),
            "A_PurchaseReqnItem": (
                ("to_PurchaseReqnAcctAssgmt", "A_PurRequisitionAcctAssgmt"),
            ),
        },
    ),
    "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV": _edmx(
        {
            "A_PurchaseOrder": ("PurchaseOrder", "Supplier", "CreationDate"),
            "A_PurchaseOrderItem": (
                "PurchaseOrder", "PurchaseOrderItem", "Material", "Plant",
                "OrderQuantity", "NetPriceAmount",
            ),
            "A_PurOrdScheduleLine": (
                "PurchaseOrder", "PurchaseOrderItem", "ScheduleLineDeliveryDate",
            ),
        }
    ),
}


def _product_row(material_id: str) -> dict[str, Any]:
    return {
        "Product": material_id,
        "ProductType": "ROH",
        "ProductGroup": "R200",
        "BaseUnit": "ST",
        "GrossWeight": "2.5",
        "to_Description": {
            "results": [
                {"Product": material_id, "Language": "TR",
                 "ProductDescription": f"Test malzemesi {material_id}"}
            ]
        },
        "to_Plant": {
            "results": [
                {"Product": material_id, "Plant": "1100", "ProcurementType": "F",
                 "PlndDelryDurnInDays": "10", "MinimumLotSizeQuantity": "1",
                 "MRPController": "001", "ABCIndicator": "A"}
            ]
        },
    }


class FakeGatewayState:
    """Surec omru boyunca yasayan sahte sistem durumu."""

    def __init__(self, *, v4_enabled: bool = True, expire_auth: bool = False) -> None:
        self.v4_enabled = v4_enabled
        self.expire_auth = expire_auth
        self.requisitions: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self._auth_failures = 0
        self._lock = threading.Lock()

    def next_pr_id(self) -> str:
        with self._lock:
            return f"1000{len(self.requisitions) + 1:04d}"

    def should_reject_auth(self) -> bool:
        """`--expire-auth` ile ilk istek 401 doner; yeniden baglanma denenir."""
        if not self.expire_auth:
            return False
        with self._lock:
            self._auth_failures += 1
            return self._auth_failures == 1


STATE = FakeGatewayState()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # pragma: no cover
        log.debug(fmt, *args)

    # --- Alt yapi ----------------------------------------------------------
    def _send(self, code: int, body: Any, ctype: str = "application/json",
              extra: dict[str, str] | None = None) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, code: int, message: str, sap_code: str = "TEST/001") -> None:
        # Gercek Gateway hata govdesi: V2 ve V4 bicimini birlikte tasir ki
        # ayristirici her iki yolu da gorsun.
        self._send(code, {
            "error": {
                "code": sap_code,
                "message": {"lang": "tr", "value": message},
                "innererror": {"errordetails": [{"message": message, "severity": "error"}]},
            }
        })

    def _is_v4(self, path: str) -> bool:
        return "/odata4/" in path

    def _wrap(self, path: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {"value": rows} if self._is_v4(path) else {"d": {"results": rows}}

    def _single(self, path: str, row: dict[str, Any]) -> dict[str, Any]:
        return row if self._is_v4(path) else {"d": row}

    def _requested_ids(self, filter_expr: str, field: str = "Material") -> list[str]:
        return re.findall(rf"{field} eq '([^']+)'", filter_expr)

    # --- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler arayuzu
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        STATE.requests.append(("GET", path))

        if STATE.should_reject_auth():
            return self._error(401, "Kimlik dogrulama basarisiz (test).", "AUTH/401")

        if path.endswith("/$metadata"):
            service = path[: -len("/$metadata")]
            if self._is_v4(path) and not STATE.v4_enabled:
                return self._error(404, "Servis SICF'te aktif degil (test).", "SRV/404")
            document = METADATA.get(service) or _edmx({"Dummy": ("Id",)})
            return self._send(200, document.encode("utf-8"), "application/xml",
                              {"x-csrf-token": CSRF_TOKEN})

        filter_expr = (query.get("$filter") or [""])[0]

        if path.startswith(PRODUCT + "/A_ProductDescription"):
            return self._send(200, self._wrap(path, [
                {"Product": m, "ProductDescription": f"Test malzemesi {m}"}
                for m in MATERIALS
            ]))
        if path.startswith(PRODUCT + "/A_Product"):
            if "A_Product(" in path:
                key = re.search(r"A_Product\('([^']+)'\)", path)
                return self._send(200, self._single(path, _product_row(
                    key.group(1) if key else MATERIALS[0])))
            ids = self._requested_ids(filter_expr, "Product") or list(MATERIALS)
            return self._send(200, self._wrap(path, [_product_row(m) for m in ids]))

        if path.startswith(VALUATION):
            ids = self._requested_ids(filter_expr) or list(MATERIALS)
            return self._send(200, self._wrap(path, [
                {"Material": m, "ValuationArea": "1100", "MovingAveragePrice": "1500.00",
                 "StandardPrice": "1500.00", "Currency": "EUR", "PriceUnitQty": "1"}
                for m in ids
            ]))

        if path.startswith(STOCK):
            ids = self._requested_ids(filter_expr) or list(MATERIALS)
            rows = []
            for m in ids:
                rows.append({"Material": m, "Plant": "1100", "StorageLocation": "0001",
                             "InventoryStockType": "01",
                             "MatlWrhsStkQtyInMatlBaseUnit": "11.0"})
                rows.append({"Material": m, "Plant": "1100", "StorageLocation": "0001",
                             "InventoryStockType": "02",
                             "MatlWrhsStkQtyInMatlBaseUnit": "2.0"})
            return self._send(200, self._wrap(path, rows))

        if path.startswith(INFORECORD):
            ids = self._requested_ids(filter_expr) or list(MATERIALS)
            return self._send(200, self._wrap(path, [
                {"PurchasingInfoRecord": f"IR-{m}", "Material": m, "Supplier": "V-100",
                 "IsDeleted": False,
                 "to_PurgInfoRecdOrgPlantData": {"results": [{
                     "PurchasingOrganization": "1000", "NetPriceAmount": "1450.00",
                     "Currency": "EUR", "MaterialPriceUnitQty": "1",
                     "MinimumPurchaseOrderQuantity": "1",
                     "MaterialPlannedDeliveryDurn": "12",
                     "IncotermsClassification": "DAP", "PaymentTerms": "NT30"}]}}
                for m in ids
            ]))

        if path.startswith(SUPPLIER):
            row = {"Supplier": "V-100", "SupplierName": "Test Tedarikci A.S.",
                   "Country": "TR", "CityName": "Istanbul",
                   "PurchasingIsBlockedForSupplier": False}
            if "A_Supplier(" in path:
                return self._send(200, self._single(path, row))
            return self._send(200, self._wrap(path, [row]))

        if path.startswith(PO_V4) or path.startswith(
            "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV"
        ):
            return self._send(200, self._wrap(path, []))

        return self._purchase_requisition_get(path, query)

    def _purchase_requisition_get(self, path: str, query: dict[str, list[str]]) -> None:
        for root in (PR_V4, PR_V2):
            if not path.startswith(root):
                continue
            key = re.search(r"\('([^']+)'\)", path)
            if key:
                record = STATE.requisitions.get(key.group(1))
                if record is None:
                    return self._error(404, "Satinalma talebi bulunamadi.", "EBAN/404")
                return self._send(200, self._single(path, record))
            filter_expr = (query.get("$filter") or [""])[0]
            token = re.search(r"'([^']+)'", filter_expr)
            rows = list(STATE.requisitions.values())
            if token:
                needle = token.group(1)
                rows = [r for r in rows
                        if needle in str(r.get("PurchaseRequisitionHeaderText", ""))]
            return self._send(200, self._wrap(path, rows))
        return self._error(404, f"Bilinmeyen servis: {path}", "SRV/404")

    # --- POST --------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        STATE.requests.append(("POST", path))

        # CSRF: gercek Gateway gibi davran. Token yoksa 403 + "Required".
        if self.headers.get("x-csrf-token") != CSRF_TOKEN:
            return self._send(403, {"error": {"code": "CSRF",
                                              "message": {"value": "CSRF token validation failed"}}},
                              extra={"x-csrf-token": "Required"})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._error(400, "Govde gecerli JSON degil.", "REQ/400")

        if path.endswith("/$batch"):
            # V4 JSON batch. V2 servise gelirse gercek Gateway 415 dondururdu;
            # bu davranisi bilerek taklit ediyoruz.
            if not self._is_v4(path):
                return self._error(
                    415, "OData V2 $batch multipart/mixed ister.", "BATCH/415"
                )
            return self._send(200, {"responses": [
                {"id": r.get("id"), "status": 200, "headers": {}, "body": {}}
                for r in body.get("requests", [])
            ]})

        for root, items_key in ((PR_V4, "_PurchaseRequisitionItem"),
                                (PR_V2, "to_PurchaseReqnItem")):
            if not path.startswith(root + "/"):
                continue
            items = body.get(items_key) or []
            if not items:
                return self._error(400, f"{items_key} bos olamaz.", "EBAN/400")
            pr_id = STATE.next_pr_id()
            record = {
                "PurchaseRequisition": pr_id,
                "PurchaseRequisitionType": body.get("PurchaseRequisitionType", "NB"),
                "PurchaseRequisitionHeaderText": body.get(
                    "PurchaseRequisitionHeaderText", ""),
                items_key: items,
                "@odata.etag": 'W/"1"',
            }
            STATE.requisitions[pr_id] = record
            log.info("PR olusturuldu: %s (%d kalem)", pr_id, len(items))
            return self._send(201, self._single(path, record), extra={"ETag": 'W/"1"'})

        return self._error(404, f"Bilinmeyen yazma ucu: {path}", "SRV/404")


def serve(port: int = 8099, host: str = "127.0.0.1") -> HTTPServer:
    """Sunucuyu arka planda baslatir ve nesnesini dondurur (testlerde kullanilir)."""
    server = HTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-v4", action="store_true",
                        help="V4 servisleri 404 dondurur; V2 fallback yolu denenir.")
    parser.add_argument("--expire-auth", action="store_true",
                        help="Ilk istek 401 doner; yeniden baglanma yolu denenir.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(levelname)-7s %(message)s")
    STATE.v4_enabled = not args.no_v4
    STATE.expire_auth = args.expire_auth

    server = HTTPServer((args.host, args.port), Handler)
    log.info("Sahte SAP Gateway: http://%s:%d", args.host, args.port)
    log.info("OData V4 servisleri: %s", "acik" if STATE.v4_enabled else "KAPALI (404)")
    log.info("Durdurmak icin Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Kapatiliyor.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
