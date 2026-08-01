"""Uretim profili kapisi ve dogrulanmis onay gecidi testleri.

Kural: `APP_ENV=production` iken guvensiz bir yapilandirma kombinasyonu servisi
**baslatmaz**. Yanlis bir deployment ayarinin sessizce gercek SAP yazmasi
yapmasindansa uygulamanin acilmamasi tercih edilir.
"""

from __future__ import annotations

import httpx
import pytest

from robotics_agent.adapters.bpa import (
    ApprovalRequest,
    BPAApprovalGateway,
    LocalApprovalGateway,
    build_approval_gateway,
)
from robotics_agent.adapters.sap.errors import SAPError
from robotics_agent.config import UnsafeProductionConfig
from robotics_agent.contracts import ActorContext
from robotics_agent.core import ApprovalStore, get_state_db


def _prod(settings_factory, tmp_path, **overrides):
    base = {"app_env": "production"}
    base.update(overrides)
    return settings_factory(tmp_path, **base)


# --- Uretim kapisi ----------------------------------------------------------
def test_auth_none_blocks_production_start(settings_factory, tmp_path):
    settings = _prod(settings_factory, tmp_path, **{"security.auth_mode": "none"})
    with pytest.raises(UnsafeProductionConfig) as exc:
        settings.enforce_production_profile()
    assert any("AGENT_AUTH_MODE=none" in p for p in exc.value.problems)


def test_odata_without_allowlist_blocks_production(settings_factory, tmp_path):
    settings = _prod(
        settings_factory,
        tmp_path,
        **{
            "security.auth_mode": "static_token",
            "sap.backend": "odata",
            "sap.auth_mode": "oauth2",
            "security.allowed_sap_hosts": (),
        },
    )
    blockers = settings.production_blockers()
    assert any("SAP_ALLOWED_HOSTS" in b for b in blockers)


def test_basic_sap_auth_blocks_production(settings_factory, tmp_path):
    settings = _prod(
        settings_factory,
        tmp_path,
        **{
            "security.auth_mode": "static_token",
            "sap.backend": "odata",
            "sap.auth_mode": "basic",
            "security.allowed_sap_hosts": ("s4.firma.test",),
        },
    )
    assert any("SAP_AUTH_MODE=basic" in b for b in settings.production_blockers())


def test_disabled_ssl_verification_blocks_production(settings_factory, tmp_path):
    settings = _prod(
        settings_factory,
        tmp_path,
        **{
            "security.auth_mode": "static_token",
            "sap.backend": "odata",
            "sap.auth_mode": "oauth2",
            "sap.verify_ssl": False,
            "security.allowed_sap_hosts": ("s4.firma.test",),
        },
    )
    assert any("SAP_VERIFY_SSL" in b for b in settings.production_blockers())


def test_real_write_with_local_gateway_blocks_production(settings_factory, tmp_path):
    """Gercek yazma yalniz dogrulanmis onay gecidiyle acilabilir."""
    settings = _prod(
        settings_factory,
        tmp_path,
        **{
            "security.auth_mode": "static_token",
            "sap.dry_run": False,
            "security.approval_gateway": "local",
        },
    )
    assert any("AGENT_APPROVAL_GATEWAY=local" in b for b in settings.production_blockers())


def test_memory_session_backend_blocks_production(settings_factory, tmp_path):
    settings = _prod(
        settings_factory,
        tmp_path,
        **{"security.auth_mode": "static_token", "state.session_backend": "memory"},
    )
    assert any("session_backend=memory" in b.lower() for b in settings.production_blockers())


def test_incomplete_oidc_blocks_production(settings_factory, tmp_path):
    settings = _prod(
        settings_factory,
        tmp_path,
        **{
            "security.auth_mode": "oidc",
            "security.oidc_issuer": "https://ias.example",
            "security.oidc_jwks_url": "https://ias.example/keys",
            "security.oidc_audience": "",
        },
    )
    assert any("AGENT_OIDC_AUDIENCE" in b for b in settings.production_blockers())


_SAFE_PRODUCTION = {
    "security.auth_mode": "static_token",
    "security.principals_file": "principals.json",
    "sap.backend": "odata",
    "sap.auth_mode": "destination",
    "sap.dry_run": True,
    "security.allowed_sap_hosts": ("s4.firma.test",),
    "state.session_backend": "sqlite",
    # Gizlilik ve risk kapilarinin guvenli uretim degerleri.
    "privacy.dlp_mode": "enforce",
    "privacy.pseudonymization_key_id": "kms://tenant-pseudonym-v1",
    "privacy.kms_key_id": "kms://agent-data-key-v1",
    "privacy.retention_sweep_seconds": 900,
    "cache.d3_enabled": False,
    "risk.scoring_mode": "enforce",
}


def test_safe_production_config_starts(settings_factory, tmp_path):
    settings = _prod(settings_factory, tmp_path, **_SAFE_PRODUCTION)
    settings.enforce_production_profile()  # exception atmamali
    assert settings.posture()["production_ready"] is True


def test_simulation_backend_blocks_production(settings_factory, tmp_path):
    """Uretimde mock backend calisamaz.

    En sinsi hata modu budur: sistem sorunsuz ayaga kalkar, cevaplar makul
    gorunur, ama veri uydurmadir ve kimse fark etmez. Yanlis veri gostermek
    hic veri gostermemekten daha tehlikelidir.
    """
    settings = _prod(settings_factory, tmp_path, **{**_SAFE_PRODUCTION, "sap.backend": "mock"})
    blockers = settings.production_blockers()
    assert any("SAP_BACKEND=mock" in b for b in blockers), blockers
    with pytest.raises(UnsafeProductionConfig):
        settings.enforce_production_profile()


# --- Gizlilik ve risk kapilari ---------------------------------------------
@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"privacy.dlp_mode": "report"}, "AGENT_DLP_MODE"),
        ({"privacy.pseudonymization_key_id": ""}, "AGENT_PSEUDONYMIZATION_KEY_ID"),
        ({"privacy.kms_key_id": ""}, "AGENT_KMS_KEY_ID"),
        ({"privacy.retention_sweep_seconds": 0}, "AGENT_RETENTION_SWEEP_SECONDS"),
        ({"cache.d3_enabled": True}, "AGENT_D3_CACHE_ENABLED"),
        ({"risk.scoring_mode": "report"}, "AGENT_RISK_SCORING_MODE"),
    ],
)
def test_privacy_and_risk_gates_block_production(
    settings_factory, tmp_path, monkeypatch, override, needle
):
    monkeypatch.delenv("AGENT_PSEUDONYMIZATION_SECRET", raising=False)
    settings = _prod(settings_factory, tmp_path, **{**_SAFE_PRODUCTION, **override})
    blockers = settings.production_blockers()
    assert any(needle in b for b in blockers), blockers
    with pytest.raises(UnsafeProductionConfig):
        settings.enforce_production_profile()


def test_production_posture_reports_privacy_stance(settings_factory, tmp_path):
    posture = _prod(settings_factory, tmp_path, **_SAFE_PRODUCTION).posture()
    assert posture["dlp_mode"] == "enforce"
    assert posture["risk_scoring_mode"] == "enforce"
    # Uretimde siniflandirilmamis alan fail-closed davranisiyla D3 kabul edilir.
    assert posture["strict_unknown_fields"] is True


def test_development_profile_only_warns(settings_factory, tmp_path):
    """Gelistirmede ayni kombinasyon servisi durdurmaz, yalniz raporlanir."""
    settings = settings_factory(tmp_path, **{"security.auth_mode": "none"})
    settings.enforce_production_profile()  # exception atmamali
    assert settings.is_production is False
    assert settings.posture()["production_ready"] is False
    assert settings.posture()["production_blockers"]


def test_posture_summarizes_configuration(settings_factory, tmp_path):
    posture = settings_factory(tmp_path).posture()
    for key in ("app_env", "auth_mode", "dry_run", "approval_gateway", "session_backend"):
        assert key in posture


# --- Onay gecidi secimi -----------------------------------------------------
def test_gateway_factory_returns_local_by_default(settings_factory, tmp_path):
    settings = settings_factory(tmp_path)
    store = ApprovalStore(get_state_db(tmp_path / "s.sqlite3"))
    assert isinstance(build_approval_gateway(settings, store), LocalApprovalGateway)


def test_local_gateway_requires_an_approver(settings_factory, tmp_path):
    from robotics_agent.core import ApprovalError

    store = ApprovalStore(get_state_db(tmp_path / "s.sqlite3"))
    gateway = LocalApprovalGateway(store)
    request = ApprovalRequest(
        tool="sap_pr_submit", payload={"items": []}, tenant="100",
        requested_by="ali", subject_line="x", diff=[],
    )
    task = gateway.request(request)
    with pytest.raises(ApprovalError):
        gateway.complete(task_id=task["task_id"], approvers=[], request=request)


# --- BPA dogrulamasi --------------------------------------------------------
def _bpa_gateway(tmp_path, handler) -> BPAApprovalGateway:
    store = ApprovalStore(get_state_db(tmp_path / "s.sqlite3"))
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="https://bpa.test")
    return BPAApprovalGateway(
        store,
        base_url="https://bpa.test",
        token_provider=lambda: "token",
        definition_id="pr-approval",
        client=client,
    )


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        tool="sap_pr_submit",
        payload={"items": [{"material_id": "X", "quantity": 1}]},
        tenant="100",
        requested_by="ali@firma.test",
        subject_line="PR onayi",
        diff=[],
        max_value=50_000,
    )


def test_bpa_reads_approver_identity_from_workflow(tmp_path):
    """Onaylayan kimligi cagiricidan degil dogrulanmis BPA kaydindan alinir."""
    request = _request()
    from robotics_agent.core.approvals import payload_hash

    def handler(http_request: httpx.Request) -> httpx.Response:
        if "task-instances" in str(http_request.url):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "task-1",
                        "status": "COMPLETED",
                        "decision": "approved",
                        "processedBy": "gercek-onaylayan@firma.test",
                        "recipientGroups": ["APPROVER"],
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "id": "wf-1",
                "status": "COMPLETED",
                "context": {"payloadSha256": payload_hash(request.payload), "tenant": "100"},
            },
        )

    gateway = _bpa_gateway(tmp_path, handler)
    # Cagirici baska bir kimlik bildirse bile yok sayilir.
    liar = ActorContext(subject="sahte@firma.test", tenant="100", roles=("APPROVER",))
    record = gateway.complete(task_id="wf-1", approvers=[liar], request=request)

    assert [a.subject for a in record.approvers] == ["gercek-onaylayan@firma.test"]
    assert record.scope["verified_by"] == "sap_bpa"


def test_bpa_rejects_completed_but_not_approved_workflow(tmp_path):
    """COMPLETED olmasi 'onaylandi' demek degildir."""
    request = _request()
    from robotics_agent.core.approvals import payload_hash

    def handler(http_request: httpx.Request) -> httpx.Response:
        if "task-instances" in str(http_request.url):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "task-1",
                        "status": "COMPLETED",
                        "decision": "rejected",
                        "processedBy": "onaylayan@firma.test",
                    }
                ],
            )
        return httpx.Response(
            200,
            json={
                "id": "wf-1",
                "status": "COMPLETED",
                "context": {"payloadSha256": payload_hash(request.payload)},
            },
        )

    gateway = _bpa_gateway(tmp_path, handler)
    with pytest.raises(SAPError) as exc:
        gateway.complete(task_id="wf-1", request=request)
    assert exc.value.code == "BPA_NOT_APPROVED"


def test_bpa_rejects_payload_mismatch(tmp_path):
    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "wf-1",
                "status": "COMPLETED",
                "context": {"payloadSha256": "baska-bir-hash"},
            },
        )

    gateway = _bpa_gateway(tmp_path, handler)
    with pytest.raises(SAPError) as exc:
        gateway.complete(task_id="wf-1", request=_request())
    assert exc.value.code == "BPA_PAYLOAD_MISMATCH"


def test_bpa_rejects_running_workflow(tmp_path):
    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "wf-1", "status": "RUNNING", "context": {}})

    gateway = _bpa_gateway(tmp_path, handler)
    with pytest.raises(SAPError) as exc:
        gateway.complete(task_id="wf-1", request=_request())
    assert exc.value.code == "BPA_NOT_COMPLETED"


def test_bpa_rejects_tenant_mismatch(tmp_path):
    request = _request()
    from robotics_agent.core.approvals import payload_hash

    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "wf-1",
                "status": "COMPLETED",
                "context": {
                    "payloadSha256": payload_hash(request.payload),
                    "tenant": "999",
                },
            },
        )

    gateway = _bpa_gateway(tmp_path, handler)
    with pytest.raises(SAPError) as exc:
        gateway.complete(task_id="wf-1", request=request)
    assert exc.value.code == "BPA_TENANT_MISMATCH"


def test_bpa_rejects_task_without_approver_identity(tmp_path):
    request = _request()
    from robotics_agent.core.approvals import payload_hash

    def handler(http_request: httpx.Request) -> httpx.Response:
        if "task-instances" in str(http_request.url):
            return httpx.Response(
                200, json=[{"id": "task-1", "status": "COMPLETED", "decision": "approved"}]
            )
        return httpx.Response(
            200,
            json={
                "id": "wf-1",
                "status": "COMPLETED",
                "context": {"payloadSha256": payload_hash(request.payload)},
            },
        )

    gateway = _bpa_gateway(tmp_path, handler)
    with pytest.raises(SAPError) as exc:
        gateway.complete(task_id="wf-1", request=request)
    assert exc.value.code == "BPA_APPROVER_UNKNOWN"
