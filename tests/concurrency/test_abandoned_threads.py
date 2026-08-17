"""Zaman asimi sonrasi terk edilmis tool thread'lerinin muhasebesi.

Timeout **cagiriciyi** serbest birakir; arka plandaki is devam eder. Bu
bilincli bir tercihtir (Python'da bir thread guvenle oldurulemez) ama sinirsiz
birakilirsa uzun omurlu bir serviste thread'ler birikir.

Bu testler uc seyi korur:
  1. Terk edilen thread sayiliyor (gorunmez sizinti olmuyor).
  2. Is bitince sayimdan dusuyor (kendi kendini toparliyor).
  3. Sinir asilinca yeni cagri ACIK hata veriyor, sessizce beklemiyor.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import time

import pytest

from robotics_agent.tools import ToolContext, execute_tool, load_all_tools
from robotics_agent.tools.registry import REGISTRY, abandoned_tool_threads


@pytest.fixture(autouse=True)
def _tools_and_clean():
    load_all_tools()
    from robotics_agent.tools import registry

    with registry._ABANDONED_LOCK:
        registry._ABANDONED.clear()
    yield
    # Asili testlerin birbirine sizmamasi icin serbest birak.
    for event in list(_EVENTS):
        event.set()
    _EVENTS.clear()
    time.sleep(0.05)
    with registry._ABANDONED_LOCK:
        registry._ABANDONED.clear()


_EVENTS: list[threading.Event] = []


def _hanging_tool(name: str, timeout_s: float = 0.05) -> threading.Event:
    """Serbest birakilana kadar asili kalan bir tool kurar."""
    event = threading.Event()
    _EVENTS.append(event)

    def handler(ctx, **kw):
        event.wait(timeout=10)
        return {"ok": True}

    base = REGISTRY[name]
    REGISTRY[name] = dataclasses.replace(base, handler=handler, timeout_s=timeout_s)
    return event


def _restore(name: str, base):
    REGISTRY[name] = base


def test_terk_edilen_thread_sayiliyor(settings, purchaser):
    ctx = ToolContext(settings=settings, actor=purchaser)
    base = REGISTRY["sap_search_materials"]
    event = _hanging_tool("sap_search_materials")
    try:
        assert abandoned_tool_threads() == 0
        payload, is_error = execute_tool("sap_search_materials", {"query": "x"}, ctx)
        assert is_error
        assert json.loads(payload)["denial_code"] == "TOOL_TIMEOUT"
        assert abandoned_tool_threads() == 1, "terk edilen thread gorunmez olmamali"
    finally:
        event.set()
        _restore("sap_search_materials", base)


def test_is_bitince_sayimdan_dusuyor(settings, purchaser):
    """Kendi kendini toparlama: gec tamamlanan tool sayimdan cikar."""
    ctx = ToolContext(settings=settings, actor=purchaser)
    base = REGISTRY["sap_search_materials"]
    event = _hanging_tool("sap_search_materials")
    try:
        execute_tool("sap_search_materials", {"query": "x"}, ctx)
        assert abandoned_tool_threads() == 1
        event.set()
        for _ in range(50):
            if abandoned_tool_threads() == 0:
                break
            time.sleep(0.02)
        assert abandoned_tool_threads() == 0, "biten thread sayimda kalmamali"
    finally:
        event.set()
        _restore("sap_search_materials", base)


def test_sinir_asilinca_acik_hata_verir(settings_factory, tmp_path, purchaser):
    """Sessizce beklemek yerine operasyona gorunur hata."""
    settings = settings_factory(tmp_path, **{"budget.max_abandoned_tool_threads": 2})
    ctx = ToolContext(settings=settings, actor=purchaser)
    base = REGISTRY["sap_search_materials"]
    event = _hanging_tool("sap_search_materials")
    try:
        for _ in range(2):
            execute_tool("sap_search_materials", {"query": "x"}, ctx)
        assert abandoned_tool_threads() == 2

        payload, is_error = execute_tool("sap_search_materials", {"query": "x"}, ctx)
        body = json.loads(payload)
        assert is_error
        assert body["denial_code"] == "TOOL_EXECUTOR_SATURATED"
        assert body["retryable"] is True
        # Doygunlukta is HIC baslamaz: mutabakat gerekmez.
        assert "needs_review" not in body
    finally:
        event.set()
        _restore("sap_search_materials", base)


def test_timeout_mutating_toolda_mutabakat_ister(settings, purchaser):
    """Regresyon: yazma tool'unda timeout 'yazilmadi' anlamina GELMEZ."""
    ctx = ToolContext(settings=settings, actor=purchaser)
    base = REGISTRY["sap_pr_submit"]
    event = _hanging_tool("sap_pr_submit")
    try:
        payload, is_error = execute_tool(
            "sap_pr_submit",
            {"draft_id": "d1", "idempotency_key": "k1", "approval_id": "a1"},
            ctx,
        )
        body = json.loads(payload)
        if body.get("denial_code") == "TOOL_TIMEOUT":
            assert body["needs_review"] is True
            assert "sap_reconcile_execution" in body["remediation"]
    finally:
        event.set()
        _restore("sap_pr_submit", base)
