"""Model tarafindan bildirilen fiyat onay esigini asagi cekemez.

## Bulunan acik

`approval_policy="threshold"` onay ihtiyacini hazirlanan taslagin tutarindan
turetir. Uc backend de (mock, odata, ecc) modelin bildirdigi `net_price`
degerini SAP bilgi kaydinin/degerlemenin YERINE kosulsuz kullaniyordu.

Sonuc: 50 adetlik 59.000 EUR'luk bir talep, `net_price: 1.0` bildirilerek
50 EUR'a iner, 25.000 EUR'luk onay esigini gecer ve **hicbir onay olmadan**
SAP'a yazilirdi. `core/policy.py` bu senaryoya karsi `require_approval_for_value`
ikinci kapisini tasiyor, ama kapiya verilen "dogrulanmis tutar" modelin kendi
fiyatindan hesaplandigi icin kapi bos calisiyordu.

## Kural

`effective_unit_price` risk skorlamasindaki `max(bildirilen, runtime)` ilkesini
fiyata uygular: bildirilen fiyat yalniz YUKARI calisir. Alicinin bildigi ve
SAP'tekinden yuksek pazarlik fiyati aynen gecerlidir; esigi asagi cekme yolu
kapalidir.
"""

from __future__ import annotations

import json

import pytest

from robotics_agent.sap.base import effective_unit_price
from robotics_agent.tools import execute_tool, load_all_tools

MATERIAL = "HD-GEAR-CSF25-100"


def pr_items(quantity: float, net_price: float | None = None) -> list[dict]:
    item: dict = {"material_id": MATERIAL, "quantity": quantity, "plant": "1100"}
    if net_price is not None:
        item["net_price"] = net_price
    return [item]


# --- Birim kural ------------------------------------------------------------


def test_declared_price_below_source_is_ignored():
    price, warning = effective_unit_price(1.0, 1180.0)
    assert price == 1180.0
    assert "asagi cekemez" in warning


def test_declared_price_above_source_is_honoured():
    """Mesru kullanim korunur: pazarlik fiyati SAP'tekinden yuksekse gecerli."""
    price, warning = effective_unit_price(1500.0, 1180.0)
    assert price == 1500.0
    assert warning == ""


def test_missing_declaration_uses_source_price():
    price, warning = effective_unit_price(None, 1180.0)
    assert price == 1180.0
    assert warning == ""


def test_equal_price_produces_no_warning():
    price, warning = effective_unit_price(1180.0, 1180.0)
    assert price == 1180.0
    assert warning == ""


def test_zero_declaration_cannot_zero_out_value():
    price, _ = effective_unit_price(0.0, 1180.0)
    assert price == 1180.0


# --- Uctan uca --------------------------------------------------------------


@pytest.fixture(autouse=True)
def _tools():
    load_all_tools()


def test_underdeclared_price_still_requires_approval(settings, purchaser):
    """Esigi asan talep, dusuk fiyat bildirilerek onaysiz gecemez."""
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import ToolContext

    object.__setattr__(settings.sap, "dry_run", False)
    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)

    payload, is_error = execute_tool(
        "sap_pr_submit",
        {
            "items": pr_items(50, net_price=1.0),
            "header_text": "Dusuk beyan denemesi",
            "idempotency_key": "guard-underdeclared",
        },
        ctx,
    )
    body = json.loads(payload)

    assert is_error, f"dusuk fiyat beyani esigi atlatti: {payload[:300]}"
    assert body["denial_code"] in {
        "APPROVAL_REQUIRED",
        "APPROVAL_SCOPE_EXCEEDED",
        "APPROVAL_INVALID",
    }
    assert not body.get("business_object_id"), "SAP'a belge yazilmis olmamali"


def test_prepare_reports_real_value_and_warns(settings, purchaser):
    """Taslak, bildirilen degil GERCEK tutari raporlar ve sapmayi bildirir."""
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import ToolContext

    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)

    honest, _ = execute_tool(
        "sap_pr_prepare", {"items": pr_items(50), "header_text": "Gercek"}, ctx
    )
    spoofed, _ = execute_tool(
        "sap_pr_prepare",
        {"items": pr_items(50, net_price=1.0), "header_text": "Dusuk beyan"},
        ctx,
    )

    honest_body, spoofed_body = json.loads(honest), json.loads(spoofed)

    assert spoofed_body["total_value"] == honest_body["total_value"], (
        "bildirilen dusuk fiyat taslak tutarini degistirmemeli"
    )
    assert spoofed_body["requires_human_approval"] is True
    findings = json.dumps(spoofed_body, ensure_ascii=False)
    assert "asagi cekemez" in findings, "sapma kullaniciya bildirilmeli"


def test_higher_negotiated_price_raises_value(settings, purchaser):
    """Yuksek pazarlik fiyati tutari yukseltir; risk yonu her zaman yukaridir."""
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import ToolContext

    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)

    base, _ = execute_tool("sap_pr_prepare", {"items": pr_items(5)}, ctx)
    raised, _ = execute_tool(
        "sap_pr_prepare", {"items": pr_items(5, net_price=99_000.0)}, ctx
    )

    base_body, raised_body = json.loads(base), json.loads(raised)
    assert raised_body["total_value"] > base_body["total_value"]
    assert raised_body["requires_human_approval"] is True


def test_low_value_request_still_passes_without_approval(settings, purchaser):
    """Duzeltme mesru akisi bozmamali: esik altindaki talep onaysiz gecer."""
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import ToolContext

    object.__setattr__(settings.sap, "dry_run", False)
    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)

    payload, is_error = execute_tool(
        "sap_pr_submit",
        {
            "items": pr_items(5),
            "header_text": "Kucuk talep",
            "idempotency_key": "guard-low-value",
        },
        ctx,
    )
    body = json.loads(payload)

    assert not is_error, f"esik altindaki talep engellenmemeli: {payload[:300]}"
    assert body["write_status"] == "created"
