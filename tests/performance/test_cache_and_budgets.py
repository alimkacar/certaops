"""Guvenli cache izolasyonu ve CI performans kapisi testleri.

Iki grup test:

  1. **Cache izolasyonu.** Cache bir performans detayi degil yetki yuzeyidir.
     Yanlis anahtarlanmis bir cache, alan bazli yetkiyi ve tenant sinirini tek
     hamlede etkisiz kilar. Kabul kriteri, cross-tenant cache/evidence
     sizintisinin sifir olmasidir.

  2. **Butce kapilari.** Sema tokeni, tool sonucu tokeni ve SAP cagri sayisi
     bildirilen sinirlarin altinda kalmali. Bu testlerin kirmizi olmasi
     release'i durdurur.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from robotics_agent.cache import (
    CachePolicy,
    DataClass,
    SecureCache,
    build_cache_key,
    entry_for,
)
from robotics_agent.contracts import ActorContext, estimate_tokens
from robotics_agent.core import AGENT_SPECS, domains_for_packs
from robotics_agent.sap import build_backend
from robotics_agent.tools import (
    ToolContext,
    anthropic_tool_definitions,
    execute_tool,
    load_all_tools,
    visible_tool_names,
)
from robotics_agent.tools.registry import REGISTRY

SCHEMA_BUDGET = 3000
TURN_RESULT_BUDGET = 6000


@pytest.fixture(autouse=True)
def _tools():
    load_all_tools()


def _actor(*, tenant="100", subject="a@firma.test", roles=("PURCHASER",), plants=("1100",)):
    return ActorContext(
        subject=subject,
        tenant=tenant,
        roles=roles,
        company_codes=frozenset({"1000"}),
        plants=frozenset(plants),
        purchasing_orgs=frozenset({"1000"}),
        auth_method="test",
    )


_POLICY = CachePolicy(ttl_seconds=60, max_class=DataClass.D2)


def _key(actor, *, tool="sap_purchase_order_360", arguments=None, policy=_POLICY, detail="standard"):
    return build_cache_key(
        tool=tool,
        tool_version="1.0.0",
        actor=actor,
        system_alias="S4-TEST",
        arguments=arguments or {"po_id": "4500019014"},
        detail=detail,
        policy=policy,
    )


# --- Cache izolasyonu ------------------------------------------------------
def test_cache_key_separates_tenants():
    assert _key(_actor(tenant="100")).digest != _key(_actor(tenant="200")).digest


def test_cross_tenant_read_is_structurally_impossible():
    """Capraz-tenant cache sizintisi yapisal olarak sifir olmalidir."""
    cache = SecureCache()
    tenant_a, tenant_b = _actor(tenant="100"), _actor(tenant="200")
    cache.set(_key(tenant_a), entry_for({"secret": "A"}, tool="t", data_class=DataClass.D1, ttl_seconds=60))
    assert cache.get(_key(tenant_b)) is None
    assert cache.get(_key(tenant_a)) is not None


def test_cache_key_separates_actors_with_different_scopes():
    """Yetkisi daraltilan kullanici eski cache'e dusmez."""
    wide = _actor(plants=("1100", "2200"))
    narrow = _actor(plants=("1100",))
    assert _key(wide).digest != _key(narrow).digest


def test_cache_key_separates_roles():
    purchaser = _actor(roles=("PURCHASER",))
    viewer = _actor(roles=("VIEWER",))
    # Farkli rol = farkli alan projeksiyonu = farkli anahtar.
    assert _key(purchaser).digest != _key(viewer).digest


def test_cache_key_separates_detail_levels():
    actor = _actor()
    assert _key(actor, detail="summary").digest != _key(actor, detail="full").digest


def test_cache_key_separates_tool_versions():
    actor = _actor()
    a = build_cache_key(
        tool="t", tool_version="1.0.0", actor=actor, system_alias="S4",
        arguments={}, detail="standard", policy=_POLICY,
    )
    b = build_cache_key(
        tool="t", tool_version="1.1.0", actor=actor, system_alias="S4",
        arguments={}, detail="standard", policy=_POLICY,
    )
    assert a.digest != b.digest


def test_cache_key_is_stable_under_argument_reordering():
    actor = _actor()
    first = _key(actor, arguments={"po_id": "X", "detail": "standard"})
    second = _key(actor, arguments={"detail": "standard", "po_id": "X"})
    assert first.digest == second.digest


def test_cache_key_ignores_non_semantic_arguments():
    actor = _actor()
    assert (
        _key(actor, arguments={"po_id": "X"}).digest
        == _key(actor, arguments={"po_id": "X", "idempotency_key": "k1"}).digest
    )


def test_subject_bound_policy_does_not_share_across_users():
    bound = CachePolicy(ttl_seconds=60, subject_bound=True)
    shared = CachePolicy(ttl_seconds=60, subject_bound=False)
    ali, veli = _actor(subject="ali@x.test"), _actor(subject="veli@x.test")
    assert _key(ali, policy=bound).digest != _key(veli, policy=bound).digest
    # Kapsami ayni olan iki kullanici paylasilan cevabi paylasabilir.
    assert _key(ali, policy=shared).digest == _key(veli, policy=shared).digest


def test_cache_key_does_not_leak_arguments_in_plaintext():
    """Anahtar listesi tek basina is bilgisi sizdirmamali."""
    key = _key(_actor(), arguments={"po_id": "4500019014", "vendor_id": "0010004"})
    assert "4500019014" not in str(key)
    assert "0010004" not in str(key)


# --- Sinif kapisi ----------------------------------------------------------
def test_restricted_data_is_never_cached():
    """D3 veri hicbir kosulda cache'lenmez."""
    cache = SecureCache()
    key = _key(_actor())
    cache.set(key, entry_for({"iban": "x"}, tool="t", data_class=DataClass.D3, ttl_seconds=60))
    assert cache.get(key) is None
    assert cache.stats.rejected_by_class == 1


def test_cache_policy_rejects_class_above_its_ceiling():
    d1_only = CachePolicy(ttl_seconds=60, max_class=DataClass.D1)
    assert d1_only.allows(DataClass.D1)
    assert not d1_only.allows(DataClass.D2)
    assert not d1_only.allows(DataClass.D3)


def test_mutating_tools_declare_no_cache():
    """Yazma tool'u cache'lenemez; kayit sirasinda zaten reddedilir."""
    for spec in REGISTRY.values():
        if spec.risk_tier.is_mutating:
            assert not spec.cache_policy.enabled, spec.name


# --- TTL ve gecersiz kilma -------------------------------------------------
def test_expired_entry_is_not_served():
    cache = SecureCache()
    key = _key(_actor())
    past = datetime.now(timezone.utc) - timedelta(seconds=120)
    cache.set(key, entry_for({"v": 1}, tool="t", data_class=DataClass.D1, ttl_seconds=60, now=past))
    assert cache.get(key) is None


def test_write_invalidates_tagged_entries_within_tenant_only():
    cache = SecureCache()
    a, b = _actor(tenant="100"), _actor(tenant="200")
    tags = frozenset({"po_id:4500019014"})
    cache.set(_key(a), entry_for({"v": "a"}, tool="t", data_class=DataClass.D1, ttl_seconds=60, tags=tags))
    cache.set(_key(b), entry_for({"v": "b"}, tool="t", data_class=DataClass.D1, ttl_seconds=60, tags=tags))

    removed = cache.invalidate_tags("100", tags)
    assert removed == 1
    assert cache.get(_key(a)) is None
    assert cache.get(_key(b)) is not None  # baska tenant etkilenmez


def test_cache_hit_reports_freshness():
    """Cache sonucu, modelin veri yasini gorebilmesi icin tazelik bilgisi tasir."""
    cache = SecureCache()
    key = _key(_actor())
    cache.set(key, entry_for({"v": 1}, tool="t", data_class=DataClass.D1, ttl_seconds=60))
    freshness = cache.get(key).freshness()
    assert freshness["cached"] is True
    assert "source_read_at" in freshness and "age_seconds" in freshness


def test_lru_eviction_bounds_memory():
    cache = SecureCache(max_entries_per_tenant=16)
    for index in range(40):
        cache.set(
            _key(_actor(), arguments={"po_id": f"PO{index}"}),
            entry_for({"v": index}, tool="t", data_class=DataClass.D1, ttl_seconds=60),
        )
    assert cache.size(tenant="100") <= 16


# --- Uctan uca cache davranisi ---------------------------------------------
def test_repeated_read_is_served_from_cache(settings, purchaser):
    from robotics_agent.cache import reset_tool_cache

    reset_tool_cache()
    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)
    first, _ = execute_tool("sap_purchase_order_360", {"po_id": "4500019014"}, ctx)
    second, _ = execute_tool("sap_purchase_order_360", {"po_id": "4500019014"}, ctx)

    assert json.loads(first)["_meta"].get("cached") is not True
    assert json.loads(second)["_meta"]["cached"] is True
    reset_tool_cache()


def test_another_tenant_does_not_see_cached_result(settings, purchaser):
    from robotics_agent.cache import reset_tool_cache

    reset_tool_cache()
    other = ActorContext(
        subject=purchaser.subject, tenant="900", roles=("PURCHASER",),
        company_codes=frozenset({"1000"}), plants=frozenset({"1100"}),
        purchasing_orgs=frozenset({"1000"}), auth_method="test",
    )
    ctx_a = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)
    ctx_b = ToolContext(settings=settings, sap=build_backend(settings), actor=other)

    execute_tool("sap_purchase_order_360", {"po_id": "4500019014"}, ctx_a)
    payload, _ = execute_tool("sap_purchase_order_360", {"po_id": "4500019014"}, ctx_b)
    assert json.loads(payload)["_meta"].get("cached") is not True
    reset_tool_cache()


# --- CI butce kapilari -----------------------------------------------------
def test_every_agent_stays_within_schema_token_budget():
    """Sema butcesi 3.000 tokeni asarsa build basarisiz olur."""
    over: list[str] = []
    for spec in AGENT_SPECS.values():
        actor = _actor(roles=("BUYER_LEAD", "AUDITOR"))
        names = visible_tool_names(domains_for_packs(spec.packs), actor)
        tokens = sum(estimate_tokens(d) for d in anthropic_tool_definitions(names))
        if tokens > SCHEMA_BUDGET:
            over.append(f"{spec.key}: {tokens} > {SCHEMA_BUDGET}")
    assert not over, "Sema butcesi asildi: " + "; ".join(over)


def test_every_tool_declares_a_performance_budget():
    for spec in REGISTRY.values():
        budget = spec.performance_budget
        assert budget is not None, spec.name
        assert budget.max_sap_calls >= 1, spec.name
        assert budget.p95_ms > 0, spec.name
        # Bildirilen token butcesi tool'un gercek sinirini asamaz.
        assert budget.max_result_tokens >= spec.result_token_budget, spec.name


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("sap_document_flow", {"document_id": "5105600231"}),
        ("sap_purchase_order_360", {"po_id": "4500019014"}),
        ("sap_workflow_status", {"object_type": "purchase_requisition", "object_id": "0010004801"}),
        ("sap_supplier_invoice_status", {"only_blocked": True}),
        ("sap_invoice_block_explain", {"invoice_id": "5105600231"}),
    ],
)
def test_p2p_tool_results_stay_within_token_budget(settings, purchaser, tool_name, arguments):
    """Varsayilan procure-to-pay sonucu en fazla 1.200 token olmalidir."""
    from robotics_agent.cache import reset_tool_cache

    reset_tool_cache()
    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)
    payload, is_error = execute_tool(tool_name, arguments, ctx)
    assert not is_error, payload
    budget = REGISTRY[tool_name].result_token_budget
    assert estimate_tokens(payload) <= budget, f"{tool_name}: {estimate_tokens(payload)} > {budget}"
    reset_tool_cache()


def test_p2p_turn_stays_within_total_result_budget(settings, purchaser):
    """Bir P2P teshis turunun tamami tur butcesini asmamali."""
    from robotics_agent.cache import reset_tool_cache
    from robotics_agent.observability import TurnMetrics

    reset_tool_cache()
    metrics = TurnMetrics(execution_id="exec-test", correlation_id="corr-test")
    ctx = ToolContext(
        settings=settings, sap=build_backend(settings), actor=purchaser, metrics=metrics
    )
    for name, args in (
        ("sap_document_flow", {"document_id": "5105600231"}),
        ("sap_purchase_order_360", {"po_id": "4500019014"}),
        ("sap_supplier_invoice_status", {"only_blocked": True}),
        ("sap_invoice_block_explain", {"invoice_id": "5105600231"}),
    ):
        execute_tool(name, args, ctx)
    assert metrics.tool_result_tokens <= TURN_RESULT_BUDGET
    reset_tool_cache()


def test_document_flow_does_not_issue_n_plus_one_queries(settings, purchaser, monkeypatch):
    """Document flow sorgusu N+1 SAP cagrisi uretmemelidir."""
    from robotics_agent.cache import reset_tool_cache

    reset_tool_cache()
    sap = build_backend(settings)
    calls: list[str] = []
    for method in (
        "get_document_flow", "get_purchase_order_items", "get_schedule_lines",
        "get_goods_receipts", "get_supplier_invoices",
    ):
        original = getattr(sap, method)

        def counted(*args, _m=method, _o=original, **kwargs):
            calls.append(_m)
            return _o(*args, **kwargs)

        monkeypatch.setattr(sap, method, counted)

    ctx = ToolContext(settings=settings, sap=sap, actor=purchaser)
    execute_tool("sap_document_flow", {"document_id": "5105600231"}, ctx)

    budget = REGISTRY["sap_document_flow"].performance_budget.max_sap_calls
    # Tool'un kendi cagrisi + adapter icinde zincir kurmak icin yapilanlar.
    assert len(calls) <= budget, f"{len(calls)} cagri > butce {budget}: {calls}"
    reset_tool_cache()
