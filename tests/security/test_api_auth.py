"""API kimlik dogrulama, yetkilendirme ve hassas veri maskeleme testleri."""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from robotics_agent.channels.auth import (
    AuthenticationError,
    Authenticator,
    RateLimiter,
    hash_token,
)
from robotics_agent.observability import mask_payload, mask_text, truncate_preview

TOKENS = {
    "purchaser": "tok-purchaser-secret",
    "approver": "tok-approver-secret",
    "viewer": "tok-viewer-secret",
    "auditor": "tok-auditor-secret",
}


@pytest.fixture
def principals_file(tmp_path):
    path = tmp_path / "principals.json"
    path.write_text(
        json.dumps(
            {
                "principals": [
                    {
                        "token_sha256": hash_token(TOKENS["purchaser"]),
                        "subject": "ali@firma.test",
                        "tenant": "100",
                        "roles": ["PURCHASER"],
                        "plants": ["1100"],
                        "purchasing_orgs": ["1000"],
                    },
                    {
                        "token_sha256": hash_token(TOKENS["approver"]),
                        "subject": "mehmet@firma.test",
                        "tenant": "100",
                        "roles": ["APPROVER"],
                        "plants": ["1100"],
                    },
                    {
                        "token_sha256": hash_token(TOKENS["viewer"]),
                        "subject": "okur@firma.test",
                        "tenant": "100",
                        "roles": ["VIEWER"],
                        "plants": ["2200"],
                    },
                    {
                        "token_sha256": hash_token(TOKENS["auditor"]),
                        "subject": "denetci@firma.test",
                        "tenant": "100",
                        "roles": ["AUDITOR"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def client(tmp_path, principals_file, monkeypatch):
    """Kimlik dogrulamasi acik bir API istemcisi."""
    monkeypatch.setenv("AGENT_AUTH_MODE", "static_token")
    monkeypatch.setenv("AGENT_PRINCIPALS_FILE", str(principals_file))
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("SAP_ALLOWED_HOSTS", "s4.firma.test")
    monkeypatch.setenv("AGENT_SESSION_BACKEND", "sqlite")
    # Gizlilik ve risk kapilari da guvenli uretim degerleriyle yapilandirilmis
    # olmali; aksi halde posture "degraded" doner.
    monkeypatch.setenv("AGENT_DLP_MODE", "enforce")
    monkeypatch.setenv("AGENT_RISK_SCORING_MODE", "enforce")
    monkeypatch.setenv("AGENT_PSEUDONYMIZATION_KEY_ID", "kms://test-pseudonym-v1")
    monkeypatch.setenv("AGENT_KMS_KEY_ID", "kms://test-data-key-v1")
    monkeypatch.setenv("AGENT_D3_CACHE_ENABLED", "false")

    import robotics_agent.config as config_module

    config_module._settings = None  # noqa: SLF001 - test icin singleton sifirlama
    api = importlib.reload(importlib.import_module("robotics_agent.channels.api"))
    return TestClient(api.app)


def auth(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[role]}"}


# --- Kimlik dogrulama ------------------------------------------------------
def test_request_without_token_is_rejected(client):
    assert client.get("/tools").status_code == 401


def test_unknown_token_is_rejected(client):
    response = client.get("/tools", headers={"Authorization": "Bearer bilinmeyen"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "INVALID_TOKEN"


def test_malformed_authorization_header_is_rejected(client):
    response = client.get("/tools", headers={"Authorization": TOKENS["purchaser"]})
    assert response.status_code == 401


def test_valid_token_resolves_actor(client):
    body = client.get("/tools", headers=auth("purchaser")).json()
    assert body["actor"]["subject"] == "ali@firma.test"
    assert body["actor"]["auth_method"] == "static_token"
    assert "1100" in body["actor"]["org_scope"]["plants"]


def test_health_is_public_and_reports_posture(client):
    body = client.get("/health").json()
    assert body["auth_mode"] == "static_token"
    assert body["audit_head"]["valid"] is True
    assert body["status"] == "ok"


def test_health_declares_simulation_mode(client):
    """Servis verinin uydurma oldugunu gizlemez.

    Simulasyon backend'i uzerinde calisan bir servisin cevaplari makul gorunur
    ama gercek degildir. `/health` bunu acikca soyler ve `production_ready`
    ile ayrica isaretler; `status` ise servisin su anki sagligidir.
    """
    body = client.get("/health").json()
    assert body["mode"] == "simulation"
    assert body["production_ready"] is False
    assert any("SAP_BACKEND=mock" in w for w in body["warnings"])


# --- Yetkilendirme ---------------------------------------------------------
def test_tool_visibility_depends_on_role(client):
    purchaser = client.get("/tools", headers=auth("purchaser")).json()
    viewer = client.get("/tools", headers=auth("viewer")).json()
    assert purchaser["visible_to_actor"] > viewer["visible_to_actor"]
    viewer_tools = {t["tool"] for t in viewer["tools"]}
    # Yazma tool'u okuyucuya hic gorunmez (token + saldiri yuzeyi).
    assert "sap_pr_submit" not in viewer_tools
    assert "sap_pr_prepare" not in viewer_tools


def test_purchaser_cannot_approve(client):
    response = client.post(
        "/approvals",
        headers=auth("purchaser"),
        json={"tool": "sap_pr_submit", "payload": {"items": []}},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "MISSING_APPROVE_SCOPE"


def test_approver_can_grant_and_record_is_retrievable(client):
    response = client.post(
        "/approvals",
        headers=auth("approver"),
        json={
            "tool": "sap_pr_submit",
            "payload": {"items": [{"material_id": "X", "quantity": 1}]},
            "requested_by": "ali@firma.test",
            "max_value": 5000,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["approval_id"].startswith("apr_")
    assert body["payload_sha256"]
    fetched = client.get(f"/approvals/{body['approval_id']}", headers=auth("approver")).json()
    assert fetched["tool"] == "sap_pr_submit"
    assert fetched["consumed"] is False


def test_self_approval_is_blocked(client):
    response = client.post(
        "/approvals",
        headers=auth("approver"),
        json={
            "tool": "sap_pr_submit",
            "payload": {"items": []},
            "requested_by": "mehmet@firma.test",
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "SOD_VIOLATION"


def test_approval_for_non_mutating_tool_is_rejected(client):
    response = client.post(
        "/approvals",
        headers=auth("approver"),
        json={"tool": "sap_search_materials", "payload": {}},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "APPROVAL_NOT_APPLICABLE"


def test_telemetry_requires_audit_scope(client):
    assert client.get("/telemetry", headers=auth("purchaser")).status_code == 403
    assert client.get("/telemetry", headers=auth("auditor")).status_code == 200


def test_sessions_are_tenant_scoped(client):
    body = client.get("/sessions", headers=auth("purchaser")).json()
    assert body["count"] == 0
    assert client.delete("/sessions/yok", headers=auth("purchaser")).status_code == 404


def test_empty_message_is_rejected_before_model_call(client):
    response = client.post("/chat", headers=auth("purchaser"), json={"message": "   "})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "EMPTY_MESSAGE"


def test_oversized_message_is_rejected_by_schema(client):
    response = client.post(
        "/chat", headers=auth("purchaser"), json={"message": "x" * 25_000}
    )
    assert response.status_code == 422


def test_correlation_id_is_echoed(client):
    response = client.get("/health", headers={"X-Correlation-ID": "corr-test-1"})
    assert response.headers["X-Correlation-ID"] == "corr-test-1"


# --- Authenticator birimi --------------------------------------------------
def test_authenticator_none_mode_returns_local_operator(settings_factory, tmp_path):
    settings = settings_factory(tmp_path, **{"security.auth_mode": "none"})
    actor = Authenticator(settings).resolve(None)
    assert actor.auth_method == "local"
    assert actor.scopes


def test_authenticator_reloads_principals_on_change(tmp_path, principals_file, settings_factory):
    settings = settings_factory(
        tmp_path,
        **{"security.auth_mode": "static_token", "security.principals_file": str(principals_file)},
    )
    authenticator = Authenticator(settings)
    assert authenticator.resolve(f"Bearer {TOKENS['viewer']}").subject == "okur@firma.test"

    principals_file.write_text(
        json.dumps(
            {
                "principals": [
                    {
                        "token_sha256": hash_token("yeni-token"),
                        "subject": "yeni@firma.test",
                        "tenant": "100",
                        "roles": ["VIEWER"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    # Eski token artik gecersiz, yenisi gecerli olmali.
    with pytest.raises(AuthenticationError):
        authenticator.resolve(f"Bearer {TOKENS['viewer']}")
    assert authenticator.resolve("Bearer yeni-token").subject == "yeni@firma.test"


def test_unknown_role_produces_no_scopes(tmp_path, settings_factory):
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps(
            {
                "principals": [
                    {
                        "token_sha256": hash_token("t"),
                        "subject": "x@firma.test",
                        "tenant": "100",
                        "roles": ["SUPER_ADMIN_UYDURMA"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = settings_factory(
        tmp_path, **{"security.auth_mode": "static_token", "security.principals_file": str(path)}
    )
    actor = Authenticator(settings).resolve("Bearer t")
    assert actor.scopes == frozenset()


# --- Rate limit ------------------------------------------------------------
def test_rate_limiter_blocks_after_limit():
    limiter = RateLimiter(per_minute=3)
    assert all(limiter.check("a")[0] for _ in range(3))
    allowed, remaining = limiter.check("a")
    assert not allowed and remaining == 0
    # Baska actor etkilenmez.
    assert limiter.check("b")[0]


# --- Maskeleme -------------------------------------------------------------
def test_secrets_are_masked_in_payloads():
    masked = mask_payload({"password": "s3cret", "SAP_TOKEN": "abc", "material_id": "ROB-1"})
    assert masked["password"] == "***"
    assert masked["SAP_TOKEN"] == "***"
    # Is verisi maskelenmez; maskelenirse karar verilemez.
    assert masked["material_id"] == "ROB-1"


def test_pii_patterns_are_masked():
    text = "Iletisim: ali.veli@firma.com, tel +90 532 111 22 33, IBAN TR330006100519786457841326"
    masked = mask_text(text)
    assert "ali.veli@firma.com" not in masked
    assert "TR330006100519786457841326" not in masked
    assert "a***@firma.com" in masked


def test_bearer_tokens_are_masked():
    assert "eyJhbGciOi" not in mask_text("Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5")


def test_preview_is_truncated_and_masked():
    preview = truncate_preview("x" * 100 + " ali@firma.com", limit=50)
    assert "ali@firma.com" not in preview
    assert "karakter kirpildi" in preview


# --- Oturum sahipligi ve kullanici izolasyonu ------------------------------
def test_sessions_are_listed_per_subject_not_per_tenant(client):
    """Ayni tenant'taki baska kullanicinin oturumu listede gorunmez."""
    purchaser_sessions = client.get("/sessions", headers=auth("purchaser")).json()
    approver_sessions = client.get("/sessions", headers=auth("approver")).json()
    assert purchaser_sessions["count"] == 0
    assert approver_sessions["count"] == 0


def test_other_users_session_id_cannot_be_claimed(client, monkeypatch):
    """Baskasinin session ID'siyle sohbet baslatmak 403 doner, kayit acmaz."""
    import robotics_agent.channels.api as api_module

    # Kullanici A icin bir oturum olustur.
    from robotics_agent.contracts import ActorContext

    owner = ActorContext(subject="ali@firma.test", tenant="100", roles=("PURCHASER",))
    record = api_module._session_store.create(actor=owner)  # noqa: SLF001

    response = client.post(
        "/chat",
        headers=auth("viewer"),
        json={"message": "merhaba", "session_id": record.session_id},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "SESSION_NOT_OWNED"


def test_other_users_session_cannot_be_deleted(client):
    import robotics_agent.channels.api as api_module
    from robotics_agent.contracts import ActorContext

    owner = ActorContext(subject="ali@firma.test", tenant="100", roles=("PURCHASER",))
    record = api_module._session_store.create(actor=owner)  # noqa: SLF001

    response = client.delete(f"/sessions/{record.session_id}", headers=auth("viewer"))
    assert response.status_code == 404
    # Sahibi icin hala var.
    assert api_module._session_store.load(record.session_id, actor=owner) is not None  # noqa: SLF001


# --- Guvenlik durusu --------------------------------------------------------
def test_health_reports_production_readiness(client):
    body = client.get("/health").json()
    assert "production_ready" in body
    assert "audit_checkpoint" in body
    assert body["app_env"] == "development"


def test_health_audit_verification_is_bounded(client):
    """Buyuk defterlerde /health tam tarama yapmamali."""
    body = client.get("/health").json()
    assert "scope" in body["audit_head"]
    assert "son" in body["audit_head"]["scope"]
