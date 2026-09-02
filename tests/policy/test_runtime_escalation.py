"""Calisma zamani risk yukseltmesi gercekten cagriliyor mu?

## Bulunan acik

`PolicyDecisionPoint.reassess()` ve `escalation_blocker()` tanimliydi,
testleri de vardi - ama `src/` icinde **hicbir cagiran yoktu**. Yani kapi
kagit uzerinde duruyordu, akista degil.

Sonucu somut: `approval_policy="threshold"` tutari cagri aninda bilmedigi
icin onay istemeyip ikinci kapiya devrediyor; `require_approval_for_value`
ise onay kaydinda `max_value` yoksa `None` donuyor. Bu ikisi birlesince
**max_value'suz tek bir R3 onayi**, SAP fiyatlandirmasi tutari milyonlara
cikardiginda bile yazmayi geciriyordu. README'nin "dogrulanmis tutar R4'e
cikarirsa iki onaylayan gerekir" anlatisi calismiyordu.

## Kural

`sap_pr_submit` taslagi fiyatlandirdiktan SONRA `reassess()` cagirir ve
`escalation_blocker()` bir mesaj donerse `RISK_ESCALATED` ile durur. Bu
kapi onay kapisindan ONCE devreye girer: korunan degismez "yazma olmaz",
hangi kodun donduugu degil.
"""

from __future__ import annotations

import json

import pytest

from robotics_agent.contracts import ActorContext
from robotics_agent.sap import build_backend
from robotics_agent.tools import ToolContext, execute_tool, load_all_tools

MATERIAL = "HD-GEAR-CSF25-100"


@pytest.fixture(autouse=True)
def _tools(settings):
    object.__setattr__(settings.sap, "read_only", False)
    object.__setattr__(settings.sap, "dry_run", False)
    load_all_tools()


def _ctx(settings, actor: ActorContext) -> ToolContext:
    return ToolContext(settings=settings, sap=build_backend(settings), actor=actor)


def _items(quantity: float) -> list[dict]:
    return [{"material_id": MATERIAL, "quantity": quantity, "plant": "1100"}]


def test_verified_value_escalates_past_single_r3_approval(settings, purchaser, grant_approval):
    """Tam bulgu senaryosu: max_value'suz tek onay + milyonluk SAP tutari."""
    ctx = _ctx(settings, purchaser)
    arguments = {
        "items": _items(5_000),  # SAP fiyati ~1.180 EUR/adet -> ~5.900.000 EUR
        "header_text": "Yukseltme kapisi",
        "idempotency_key": "escalation-gate-1",
    }
    # max_value=None: onay kaydi tutar tavani tasimiyor. Eski davranista
    # `require_approval_for_value` burada None donup yazmayi geciriyordu.
    arguments["approval_id"] = grant_approval(
        ctx, tool="sap_pr_submit", arguments=arguments, max_value=None
    )

    payload, is_error = execute_tool("sap_pr_submit", arguments, ctx)
    body = json.loads(payload)

    assert is_error, f"milyonluk yazma tek R3 onayiyla gecti: {payload[:400]}"
    assert body["denial_code"] == "RISK_ESCALATED"
    assert body["effective_tier"] == "R4"
    assert body["declared_tier"] == "R3"
    assert body["total_value"] > 1_000_000
    assert not body.get("business_object_id"), "SAP'a belge yazilmis olmamali"


def test_escalation_is_audited_with_explainable_scores(settings, purchaser, grant_approval):
    """Karar aciklanabilir olmali: hangi skor, hangi seviye, hangi tutar."""
    ctx = _ctx(settings, purchaser)
    arguments = {
        "items": _items(5_000),
        "header_text": "Denetim izi",
        "idempotency_key": "escalation-gate-2",
    }
    arguments["approval_id"] = grant_approval(
        ctx, tool="sap_pr_submit", arguments=arguments, max_value=None
    )
    execute_tool("sap_pr_submit", arguments, ctx)

    entries = [
        e for e in ctx.audit.recent(limit=200) if e.get("event") == "policy.escalation_blocked"
    ]
    assert entries, "yukseltme kaydi audit'e dusmedi"
    detail = entries[0]["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)
    assert detail["declared_tier"] == "R3"
    assert detail["effective_tier"] == "R4"
    assert isinstance(detail["impact_score"], int)
    assert detail["total_value"] > 1_000_000


def test_dual_approval_clears_the_gate(settings, purchaser, approver, grant_approval):
    """Kapi mesru akisi kesmiyor: iki ayri onaylayan varsa yazma gecer."""
    ctx = _ctx(settings, purchaser)
    second = ActorContext(
        subject="ikinci.onay@firma.test",
        tenant=approver.tenant,
        roles=approver.roles,
        plants=approver.plants,
        auth_method="test",
    )
    arguments = {
        "items": _items(5_000),
        "header_text": "Cift onay",
        "idempotency_key": "escalation-gate-3",
    }
    arguments["approval_id"] = grant_approval(
        ctx,
        tool="sap_pr_submit",
        arguments=arguments,
        max_value=None,
        approvers=[approver, second],
    )

    payload, is_error = execute_tool("sap_pr_submit", arguments, ctx)
    body = json.loads(payload)

    assert not is_error, f"iki onaylayanli mesru yazma engellendi: {payload[:400]}"
    assert body.get("denial_code") != "RISK_ESCALATED"


def test_small_request_is_untouched_by_the_gate(settings, purchaser, grant_approval):
    """Esik altindaki talep yukseltilmemeli; kapi sadece tutar buyudugunde acilir."""
    ctx = _ctx(settings, purchaser)
    arguments = {
        "items": _items(2),
        "header_text": "Kucuk talep",
        "idempotency_key": "escalation-gate-4",
    }
    arguments["approval_id"] = grant_approval(
        ctx, tool="sap_pr_submit", arguments=arguments, max_value=None
    )

    payload, is_error = execute_tool("sap_pr_submit", arguments, ctx)
    body = json.loads(payload)

    assert not is_error, f"kucuk talep engellendi: {payload[:400]}"
    assert body["write_status"] == "created"
