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
    # Model saglayicisinin uretim degerleri: Vertex backend'i (kurumsal veri
    # isleme sozlesmesi) ve saglayici tarafinda saklama KAPALI.
    "model.provider": "gemini",
    "model.gemini_backend": "vertex",
    "model.google_cloud_project": "certaops-prod",
    "model.google_cloud_location": "europe-west4",
    "model.store_interactions": False,
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


# ---------------------------------------------------------------------------
# ECC backend'i uretim kapisinda odata ile ayni muameleyi gormeli.
#
# Regresyon kaydi: ECC backend'i eklendiginde `SAPSettings.validate()`
# guncellendi ama uretim profili `backend == "odata"` kontrolunde kaldi.
# Sonuc: SAP_BACKEND=ecc ile basic auth, kapali SSL ve bos egress allowlist
# uretimde sessizce gecebiliyordu. Gercek SAP baglantisi isteyen her backend
# ayni kapidan gecmeli.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("backend", ["odata", "ecc"])
def test_gercek_backend_uretimde_egress_allowlist_ister(
    settings_factory, tmp_path, backend
):
    settings = _prod(
        settings_factory, tmp_path,
        **{"sap.backend": backend, "sap.base_url": "https://sap.local",
           "sap.auth_mode": "oauth2", "sap.oauth_token_url": "https://t",
           "sap.oauth_client_id": "c", "sap.oauth_client_secret": "s",
           "security.allowed_sap_hosts": ()},
    )
    blockers = settings.production_blockers()
    assert any("ALLOWED_HOSTS" in b for b in blockers), (
        f"{backend}: egress allowlist zorunlulugu uygulanmadi"
    )


@pytest.mark.parametrize("backend", ["odata", "ecc"])
def test_gercek_backend_uretimde_basic_auth_kabul_etmez(
    settings_factory, tmp_path, backend
):
    settings = _prod(
        settings_factory, tmp_path,
        **{"sap.backend": backend, "sap.base_url": "https://sap.local",
           "sap.auth_mode": "basic", "sap.username": "u", "sap.password": "p",
           "security.allowed_sap_hosts": ("sap.local",)},
    )
    blockers = settings.production_blockers()
    assert any("basic" in b.lower() for b in blockers), (
        f"{backend}: uretimde basic auth engellenmedi"
    )


@pytest.mark.parametrize("backend", ["odata", "ecc"])
def test_gercek_backend_uretimde_ssl_dogrulamasi_zorunlu(
    settings_factory, tmp_path, backend
):
    settings = _prod(
        settings_factory, tmp_path,
        **{"sap.backend": backend, "sap.base_url": "https://sap.local",
           "sap.auth_mode": "oauth2", "sap.oauth_token_url": "https://t",
           "sap.oauth_client_id": "c", "sap.oauth_client_secret": "s",
           "sap.verify_ssl": False, "security.allowed_sap_hosts": ("sap.local",)},
    )
    blockers = settings.production_blockers()
    assert any("VERIFY_SSL" in b for b in blockers), (
        f"{backend}: SSL dogrulamasi kapaliyken uretim acildi"
    )


# --- Model saglayici kapilari -----------------------------------------------
def test_fake_provider_blocks_production(settings_factory, tmp_path):
    """Test saglayicisi uretimde calisamaz."""
    settings = _prod(settings_factory, tmp_path, **{**_SAFE_PRODUCTION, "model.provider": "fake"})
    blockers = settings.production_blockers()
    assert any("MODEL_PROVIDER=fake" in b for b in blockers)


def test_developer_backend_blocks_production(settings_factory, tmp_path):
    """Developer API uretimde SAP verisi icin onerilmez; Vertex istenir."""
    settings = _prod(
        settings_factory,
        tmp_path,
        **{
            **_SAFE_PRODUCTION,
            "model.gemini_backend": "developer",
            "model.gemini_api_key": "key",
        },
    )
    blockers = settings.production_blockers()
    assert any("GEMINI_BACKEND=developer" in b for b in blockers)


def test_provider_side_storage_blocks_production(settings_factory, tmp_path):
    """SAP verisi saglayici tarafinda kalici olarak saklanamaz."""
    settings = _prod(
        settings_factory, tmp_path, **{**_SAFE_PRODUCTION, "model.store_interactions": True}
    )
    blockers = settings.production_blockers()
    assert any("STORE_INTERACTIONS" in b for b in blockers)


def test_unconfigured_provider_blocks_production(settings_factory, tmp_path):
    settings = _prod(
        settings_factory,
        tmp_path,
        **{**_SAFE_PRODUCTION, "model.google_cloud_project": "", "model.google_cloud_location": ""},
    )
    blockers = settings.production_blockers()
    assert any("yapilandirilmamis" in b for b in blockers)


# --- Operator kapatma anahtari ---------------------------------------------
def test_disabled_tool_is_hidden_from_model_and_denied_when_called(monkeypatch):
    """Kapatma anahtari iki katmanli olmali.

    Modele gostermemek yetmez: model tool adini tahmin edip cagirabilir.
    Policy kapisi da reddetmelidir. Ve bu red, yetki/risk/onay
    degerlendirmesinden ONCE gelmelidir - olay sirasinda "kapali" karari
    tartisilmaz.
    """
    import json

    from robotics_agent.config import get_settings
    from robotics_agent.contracts import ActorContext
    from robotics_agent.core.router import domains_for_packs
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import (
        ToolContext,
        execute_tool,
        load_all_tools,
        visible_tool_names,
    )

    monkeypatch.setenv("AGENT_DISABLED_TOOLS", "sap_pr_submit")
    monkeypatch.setenv("SAP_BACKEND", "mock")
    settings = get_settings(reload=True)
    settings.ensure_dirs()
    load_all_tools()

    actor = ActorContext.local_operator(
        subject="ops@firma.test", tenant=settings.sap.tenant,
        roles=("PURCHASER", "APPROVER"),
        company_code=settings.sap.company_code, plant=settings.sap.plant,
        purchasing_org=settings.sap.purch_org,
    )

    visible = visible_tool_names(domains_for_packs(("bootstrap", "procurement_write")), actor)
    assert "sap_pr_submit" not in visible, "kapatilan tool modele gosterilmemeli"
    assert "sap_pr_prepare" in visible, "digerleri etkilenmemeli"

    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=actor)
    payload, is_error = execute_tool(
        "sap_pr_submit",
        {"items": [{"material_id": "X", "quantity": 1}], "idempotency_key": "k:v1"},
        ctx,
    )
    body = json.loads(payload)
    assert is_error
    assert body["denial_code"] == "TOOL_DISABLED"

    get_settings(reload=True)  # sonraki testler icin ortami geri al
