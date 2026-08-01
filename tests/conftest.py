"""Ortak test fixture'lari.

Her test oturumu izole bir durum dizini kullanir: onay, idempotency, oturum ve
audit kayitlari gercek kullanicinin `state/` dizinine karismaz.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from robotics_agent.adapters.bpa import ApprovalRequest, LocalApprovalGateway
from robotics_agent.config import Settings
from robotics_agent.contracts import ActorContext
from robotics_agent.core import approval_payload_for, reset_audit_cache, reset_state_db_cache
from robotics_agent.sap import build_backend
from robotics_agent.tools import ToolContext, execute_tool, load_all_tools

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_settings(tmp_path: Path, **overrides) -> Settings:
    """Izole durum/cikti dizinleriyle ayar nesnesi uretir."""
    settings = Settings()
    object.__setattr__(settings, "output_dir", tmp_path / "out")
    object.__setattr__(settings.state, "dir", tmp_path / "state")
    settings.ensure_dirs()
    for key, value in overrides.items():
        target = settings
        if "." in key:
            section, key = key.split(".", 1)
            target = getattr(settings, section)
        object.__setattr__(target, key, value)
    return settings


@pytest.fixture(autouse=True)
def _isolate_state():
    """Her testten once/sonra paylasilan onbellekleri temizler."""
    reset_state_db_cache()
    reset_audit_cache()
    yield
    reset_state_db_cache()
    reset_audit_cache()


@pytest.fixture(scope="function")
def settings(tmp_path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture(scope="function")
def settings_factory():
    """Ozel ayar override'lari gereken testler icin fabrika.

    Alt dizinlerdeki test modulleri conftest'i relative import edemedigi icin
    yardimci fonksiyon fixture olarak sunulur.
    """
    return make_settings


@pytest.fixture(scope="function")
def purchaser() -> ActorContext:
    return ActorContext(
        subject="satinalmaci@firma.test",
        tenant="100",
        roles=("PURCHASER",),
        company_codes=frozenset({"1000"}),
        plants=frozenset({"1100"}),
        purchasing_orgs=frozenset({"1000"}),
        auth_method="test",
    )


@pytest.fixture(scope="function")
def approver() -> ActorContext:
    return ActorContext(
        subject="onaylayan@firma.test",
        tenant="100",
        roles=("APPROVER",),
        plants=frozenset({"1100"}),
        auth_method="test",
    )


@pytest.fixture(scope="function")
def engineer() -> ActorContext:
    return ActorContext(
        subject="muhendis@firma.test",
        tenant="100",
        roles=("ENGINEER",),
        plants=frozenset({"1100"}),
        purchasing_orgs=frozenset({"1000"}),
        company_codes=frozenset({"1000"}),
        auth_method="test",
    )


@pytest.fixture(scope="function")
def auditor() -> ActorContext:
    return ActorContext(
        subject="denetci@firma.test", tenant="100", roles=("AUDITOR",), auth_method="test"
    )


@pytest.fixture(scope="function")
def viewer() -> ActorContext:
    return ActorContext(
        subject="okur@firma.test",
        tenant="100",
        roles=("VIEWER",),
        plants=frozenset({"2200"}),
        auth_method="test",
    )


@pytest.fixture(scope="function")
def ctx(settings, engineer) -> ToolContext:
    """Genel amacli baglam: okuma + simulasyon + hazirlama yetkisi olan muhendis."""
    load_all_tools()
    return ToolContext(settings=settings, sap=build_backend(settings), actor=engineer)


@pytest.fixture(scope="function")
def buyer_ctx(settings, purchaser) -> ToolContext:
    """Yazma yetkisi olan satinalmaci baglami (SAP_DRY_RUN kapali)."""
    load_all_tools()
    object.__setattr__(settings.sap, "dry_run", False)
    return ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)


@pytest.fixture(scope="function")
def run_tool():
    """Tool cagirip JSON sonucu dondurur; hata bayragini da verir."""

    def _run(name: str, ctx: ToolContext, *, expect_error: bool = False, **kwargs) -> dict:
        payload, is_error = execute_tool(name, kwargs, ctx)
        result = json.loads(payload)
        if expect_error:
            assert is_error, f"{name} hata dondurmeliydi: {payload}"
        else:
            assert not is_error, f"{name} hata dondurdu: {payload}"
        return result

    return _run


@pytest.fixture(scope="function")
def grant_approval(approver):
    """Bir tool cagrisi icin gecerli onay kaydi uretir."""

    def _grant(
        ctx: ToolContext,
        *,
        tool: str,
        arguments: dict,
        max_value: float | None = None,
        approvers: list[ActorContext] | None = None,
    ) -> str:
        gateway = LocalApprovalGateway(ctx.approvals, ttl_minutes=30)
        canonical = approval_payload_for(arguments)
        request = ApprovalRequest(
            tool=tool,
            payload=canonical,
            tenant=(approvers or [approver])[0].tenant,
            requested_by=ctx.actor.subject if ctx.actor else "",
            subject_line=f"{tool} onayi",
            diff=[],
            max_value=max_value,
        )
        task = gateway.request(request)
        record = gateway.complete(
            task_id=task["task_id"], approvers=approvers or [approver], request=request
        )
        return record.approval_id

    return _grant
