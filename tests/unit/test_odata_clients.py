"""OData V2/V4 istemci protokol ve hata-isleme testleri.

Gercek ag kullanilmaz: `httpx.MockTransport` ile ETag, If-Match, nextLink
sayfalamasi, $batch, CSRF yenileme, Retry-After ve host allowlist davranisi
dogrulanir.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from robotics_agent.adapters.sap import (
    BatchRequest,
    HostNotAllowed,
    ODataHttpCore,
    ODataV2Client,
    ODataV4Client,
    SAPError,
    parse_sap_error,
)
from robotics_agent.adapters.sap.http import split_link
from robotics_agent.adapters.sap.odata_v2 import parse_odata_datetime, to_odata_datetime

METADATA_XML = (
    '<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">'
    "<edmx:DataServices/></edmx:Edmx>"
)


def build_client(handler, **kwargs) -> ODataV4Client:
    core = ODataHttpCore(
        client=httpx.Client(base_url="https://s4.firma.test", transport=httpx.MockTransport(handler)),
        sap_client="100",
        sleep=lambda _: None,
        **kwargs,
    )
    return ODataV4Client(core, page_size=2, max_pages=5)


# --- Okuma ve sayfalama ----------------------------------------------------
def test_read_collection_follows_next_link():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "skiptoken" in str(request.url):
            return httpx.Response(200, json={"value": [{"id": 3}]})
        return httpx.Response(
            200,
            json={
                "value": [{"id": 1}, {"id": 2}],
                "@odata.nextLink": "srv/Items?$skiptoken=abc",
                "@odata.count": 3,
            },
        )

    page = build_client(handler).read_collection("srv", "Items", count=True)
    assert [row["id"] for row in page.rows] == [1, 2, 3]
    assert page.total_count == 3
    assert page.has_more is False
    # sap-client her sayfada korunmali, skiptoken kaybolmamali.
    assert "skiptoken=abc" in calls[1].replace("%24", "$")
    assert "sap-client=100" in calls[1]


def test_read_collection_stops_at_max_pages_and_returns_cursor():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"value": [{"id": 1}], "@odata.nextLink": "srv/Items?$skiptoken=z"}
        )

    core = ODataHttpCore(
        client=httpx.Client(base_url="https://s4.firma.test", transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    page = ODataV4Client(core, page_size=1, max_pages=2).read_collection("srv", "Items")
    assert len(page.rows) == 2
    assert page.has_more is True  # imlec modele gosterilir, sinirsiz cekilmez


def test_read_entity_returns_none_on_404():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "yok"}})

    row, etag = build_client(handler).read_entity("srv", "Items('1')")
    assert row is None and etag == ""


def test_etag_is_read_from_body_or_header():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": 1, "@odata.etag": 'W/"abc"'})

    _, etag = build_client(handler).read_entity("srv", "Items('1')")
    assert etag == 'W/"abc"'


def test_split_link_preserves_query():
    path, params = split_link("srv/Items?$skiptoken=abc&extra=1")
    assert path == "srv/Items"
    assert params["$skiptoken"] == "abc" and params["extra"] == "1"


# --- Yazma ve optimistic locking -------------------------------------------
def test_create_fetches_csrf_token_and_posts():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=METADATA_XML, headers={"x-csrf-token": "TOK"})
        seen["csrf"] = request.headers.get("x-csrf-token", "")
        seen["prefer"] = request.headers.get("prefer", "")
        return httpx.Response(201, json={"PurchaseRequisition": "10000001"})

    created, _ = build_client(handler).create("srv", "PurchaseRequisition", {"a": 1})
    assert created["PurchaseRequisition"] == "10000001"
    assert seen["csrf"] == "TOK"
    assert "representation" in seen["prefer"]


def test_update_requires_etag():
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - cagrilmamali
        return httpx.Response(200, json={})

    with pytest.raises(SAPError) as exc:
        build_client(handler).update("srv", "Items('1')", {"a": 1}, etag="")
    assert exc.value.code == "ETAG_REQUIRED"


def test_update_sends_if_match():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=METADATA_XML, headers={"x-csrf-token": "TOK"})
        seen["if_match"] = request.headers.get("if-match", "")
        return httpx.Response(200, json={"a": 2})

    build_client(handler).update("srv", "Items('1')", {"a": 2}, etag='W/"v1"')
    assert seen["if_match"] == 'W/"v1"'


def test_stale_update_surfaces_as_concurrency_conflict():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=METADATA_XML, headers={"x-csrf-token": "TOK"})
        return httpx.Response(
            412, json={"error": {"code": "PRECONDITION", "message": "ETag mismatch"}}
        )

    with pytest.raises(SAPError) as exc:
        build_client(handler).update("srv", "Items('1')", {"a": 1}, etag='W/"old"')
    assert exc.value.is_concurrency
    assert "yeniden okuyup" in exc.value.as_dict()["conflict"]


def test_expired_csrf_token_is_refreshed_once():
    state = {"metadata_calls": 0, "posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/$metadata"):
            state["metadata_calls"] += 1
            return httpx.Response(
                200, text=METADATA_XML, headers={"x-csrf-token": f"TOK{state['metadata_calls']}"}
            )
        state["posts"] += 1
        if state["posts"] == 1:
            return httpx.Response(403, text="CSRF token validation failed")
        return httpx.Response(201, json={"PurchaseRequisition": "1"})

    created, _ = build_client(handler).create("srv", "PurchaseRequisition", {"a": 1})
    assert created["PurchaseRequisition"] == "1"
    assert state["metadata_calls"] == 2  # token yenilendi
    assert state["posts"] == 2


def test_write_is_not_blindly_retried_on_server_error():
    """Idempotent olmayan yazma 500 alinca korlemesine tekrarlanmamali."""
    state = {"posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=METADATA_XML, headers={"x-csrf-token": "TOK"})
        state["posts"] += 1
        return httpx.Response(500, json={"error": {"code": "X", "message": "patladi"}})

    with pytest.raises(SAPError):
        build_client(handler).create("srv", "Items", {"a": 1})
    assert state["posts"] == 1


def test_write_timeout_propagates_for_reconciliation():
    """Timeout yukselir; karar idempotency/mutabakat katmanina aittir."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=METADATA_XML, headers={"x-csrf-token": "TOK"})
        raise httpx.TimeoutException("timeout", request=request)

    with pytest.raises(httpx.TimeoutException):
        build_client(handler).create("srv", "Items", {"a": 1})


# --- Retry ve Retry-After --------------------------------------------------
def test_read_retries_on_429_and_honours_retry_after():
    delays: list[float] = []
    state = {"calls": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {}})
        return httpx.Response(200, json={"value": [{"id": 1}]})

    core = ODataHttpCore(
        client=httpx.Client(base_url="https://s4.firma.test", transport=httpx.MockTransport(handler)),
        sleep=delays.append,
    )
    page = ODataV4Client(core).read_collection("srv", "Items")
    assert page.rows == [{"id": 1}]
    assert delays == [2.0]


# --- $batch ----------------------------------------------------------------
def test_batch_returns_per_request_responses():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/$metadata"):
            return httpx.Response(200, text=METADATA_XML, headers={"x-csrf-token": "TOK"})
        return httpx.Response(
            200,
            json={
                "responses": [
                    {"id": "a", "status": 200, "body": {"SupplierName": "RoboDrive"}},
                    {"id": "b", "status": 404, "body": {"error": {"message": "yok"}}},
                ]
            },
        )

    responses = build_client(handler).batch(
        "srv",
        [
            BatchRequest(id="a", method="GET", url="A_Supplier('1')"),
            BatchRequest(id="b", method="GET", url="A_Supplier('2')"),
        ],
    )
    assert responses[0].is_success and responses[0].body["SupplierName"] == "RoboDrive"
    assert not responses[1].is_success


# --- Guvenlik --------------------------------------------------------------
def test_host_allowlist_blocks_unknown_host():
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, json={})

    core = ODataHttpCore(
        client=httpx.Client(base_url="https://evil.example", transport=httpx.MockTransport(handler)),
        allowed_hosts=("s4.firma.test",),
    )
    with pytest.raises(HostNotAllowed) as exc:
        ODataV4Client(core).read_collection("srv", "Items")
    assert exc.value.code == "EGRESS_BLOCKED"


def test_host_allowlist_accepts_wildcard_subdomain():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": []})

    core = ODataHttpCore(
        client=httpx.Client(
            base_url="https://prod.s4.firma.test", transport=httpx.MockTransport(handler)
        ),
        allowed_hosts=("*.firma.test",),
    )
    assert ODataV4Client(core).read_collection("srv", "Items").rows == []


def test_correlation_id_is_sent():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["corr"] = request.headers.get("sap-correlationid", "")
        return httpx.Response(200, json={"value": []})

    build_client(handler).read_collection("srv", "Items", correlation_id="corr-42")
    assert seen["corr"] == "corr-42"


# --- Hata cozumleme -------------------------------------------------------
def test_parses_v2_error_shape():
    fault = parse_sap_error(
        status_code=400,
        body='{"error":{"code":"MM/123","message":{"lang":"tr","value":"Malzeme yok"},'
        '"innererror":{"errordetails":[{"code":"D1","message":"detay","severity":"error"}]}}}',
        headers={"sap-correlationid": "c1"},
        target_api="product",
    )
    assert fault.code == "MM/123"
    assert fault.message == "Malzeme yok"
    assert fault.correlation_id == "c1"
    assert fault.details[0]["code"] == "D1"


def test_parses_v4_error_shape():
    fault = parse_sap_error(
        status_code=400,
        body='{"error":{"code":"PR/001","message":"Kalem hatasi",'
        '"details":[{"code":"E1","message":"m","target":"Material"}],'
        '"@SAP__common.numericSeverity":4}}',
        headers={},
        target_api="purchase_requisition",
    )
    assert fault.message == "Kalem hatasi"
    assert fault.severity == "error"
    assert fault.details[0]["target"] == "Material"


def test_html_error_body_is_stripped():
    fault = parse_sap_error(
        status_code=500,
        body="<html><body><h1>Internal Error</h1></body></html>",
        headers={},
        target_api="x",
    )
    assert "<" not in fault.message
    assert "Internal Error" in fault.message


def test_authorization_fault_is_classified():
    fault = parse_sap_error(status_code=403, body="", headers={}, target_api="x")
    assert fault.is_authorization
    assert "yetki" in fault.to_dict()["authorization"].lower()


def test_retryable_classification():
    assert parse_sap_error(status_code=503, body="", headers={}, target_api="x").is_retryable
    assert not parse_sap_error(status_code=400, body="", headers={}, target_api="x").is_retryable


# --- V2 ozel davranislar ---------------------------------------------------
def test_v2_unwraps_d_results():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"d": {"results": [{"Product": "M1"}, {"Product": "M2"}]}})

    core = ODataHttpCore(
        client=httpx.Client(base_url="https://s4.firma.test", transport=httpx.MockTransport(handler)),
        odata_version="v2",
    )
    rows = ODataV2Client(core).read("srv", "A_Product")
    assert [r["Product"] for r in rows] == ["M1", "M2"]


def test_v2_date_literals_roundtrip():
    literal = to_odata_datetime(date(2026, 9, 30))
    assert literal.startswith("/Date(")
    assert parse_odata_datetime(literal) == date(2026, 9, 30)
    assert parse_odata_datetime("2026-09-30T00:00:00") == date(2026, 9, 30)
    assert parse_odata_datetime(None) is None


def test_v2_adds_json_format_param():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"d": {"results": []}})

    core = ODataHttpCore(
        client=httpx.Client(base_url="https://s4.firma.test", transport=httpx.MockTransport(handler)),
        odata_version="v2",
        sap_client="100",
    )
    ODataV2Client(core).read("srv", "A_Product")
    assert "format=json" in seen["url"]
