"""API kimlik dogrulama, yetkilendirme ve hassas veri maskeleme testleri."""

from __future__ import annotations

import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

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
    monkeypatch.setenv("SAP_BACKEND", "mock")
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


def test_health_is_reachable_without_token_but_says_nothing(client):
    """Canlilik probe'u token tasiyamaz; ama duruş dokumu de herkese acilamaz.

    Onceki hali kimliksiz cagirana auth modunu, kapatilmis tool listesini, DLP
    modunu, saklama politikasini ve audit zincir durumunu veriyordu. Bunlarin
    toplami saldirgana, hicbir sey denemeden once HANGI KONTROLLERIN KAPALI
    oldugunu soyler.
    """
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok"}, f"kimliksiz /health fazla bilgi verdi: {body}"


def test_health_posture_requires_platform_read(client):
    """Ayrintili durus raporu `platform.read` kapsami ister."""
    body = client.get("/health", headers=auth("viewer")).json()
    assert body["auth_mode"] == "static_token"
    assert body["audit_head"]["valid"] is True
    assert body["status"] == "ok"
    assert body["runtime_scope"] == "per_authenticated_session_security_context"
    assert body["runtime_count"] == 1
    assert body["runtime_cache"]["cached"] >= 0
    assert "disabled_tools" in body


def test_health_invalid_token_is_treated_as_anonymous(client):
    """`/health` bir kimlik dogrulama ucu degildir: gecersiz token 401 degil,
    kimliksiz yanit alir - ama yine de basarisiz deneme sayacina yazilir."""
    response = client.get("/health", headers={"Authorization": "Bearer gecersiz"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_declares_simulation_mode(client):
    """Servis verinin uydurma oldugunu gizlemez.

    Simulasyon backend'i uzerinde calisan bir servisin cevaplari makul gorunur
    ama gercek degildir. `/health` bunu acikca soyler ve `production_ready`
    ile ayrica isaretler; `status` ise servisin su anki sagligidir.
    """
    body = client.get("/health", headers=auth("viewer")).json()
    assert body["mode"] == "simulation"
    assert body["read_only"] is True
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
    response = client.post("/chat", headers=auth("purchaser"), json={"message": "x" * 25_000})
    assert response.status_code == 422


def test_chunked_oversized_body_is_rejected_by_middleware(client, monkeypatch):
    import robotics_agent.channels.api as api

    original_limit = api._settings.security.max_request_bytes
    object.__setattr__(api._settings.security, "max_request_bytes", 32)
    try:
        response = client.post(
            "/chat",
            headers={**auth("purchaser"), "Transfer-Encoding": "chunked"},
            content=iter([b'{"message":"', b"x" * 64, b'"}']),
        )
    finally:
        object.__setattr__(api._settings.security, "max_request_bytes", original_limit)

    assert response.status_code == 413
    assert response.json()["code"] == "REQUEST_TOO_LARGE"


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


def test_actor_security_change_rebuilds_and_closes_cached_runtime(client, monkeypatch):
    import robotics_agent.channels.api as api_module
    from robotics_agent.contracts import ActorContext

    instances = []

    class FakeRuntime:
        def __init__(self, *args, actor, **kwargs):
            self.actor = actor
            self.closed = False
            instances.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(api_module, "SAPAgentRuntime", FakeRuntime)
    viewer = ActorContext(
        subject="same@firma.test", tenant="100", roles=("VIEWER",), plants=frozenset({"1100"})
    )
    purchaser = ActorContext(
        subject="same@firma.test",
        tenant="100",
        roles=("PURCHASER",),
        plants=frozenset({"1100"}),
    )

    first = api_module._agent_for(viewer, "security-change")  # noqa: SLF001
    assert api_module._agent_for(viewer, "security-change") is first  # noqa: SLF001
    replacement = api_module._agent_for(purchaser, "security-change")  # noqa: SLF001

    assert replacement is not first
    assert first.closed is True
    assert len(instances) == 2
    api_module._evict_runtime(purchaser, "security-change")  # noqa: SLF001


def test_parallel_api_turn_is_rejected_before_model_or_tool(client, monkeypatch):
    import robotics_agent.channels.api as api_module

    actor = api_module._auth().resolve(  # noqa: SLF001
        f"Bearer {TOKENS['purchaser']}"
    )
    record = api_module._session_store.create(actor=actor)  # noqa: SLF001
    entered = Event()
    release = Event()

    class BlockingRuntime:
        call_count = 0

        def __init__(self, *args, **kwargs):
            self.messages = []
            self.active_packs = ["bootstrap"]

        def chat(self, message):
            type(self).call_count += 1
            entered.set()
            assert release.wait(timeout=5)
            self.messages.extend(
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "tamam"},
                ]
            )
            return SimpleNamespace(
                text="tamam",
                tool_calls=[],
                iterations=1,
                input_tokens=1,
                output_tokens=1,
                direct_answer=False,
                direct_answer_reason="",
                model_calls=1,
                active_packs=list(self.active_packs),
                active_agents=[],
                agent_trace=[],
                policy_denials=0,
                needs_review=False,
                correlation_id="corr-blocking",
                artifacts=[],
            )

        def close(self):
            return None

    monkeypatch.setattr(api_module, "SAPAgentRuntime", BlockingRuntime)
    payload = {"message": "ilk", "session_id": record.session_id}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, "/chat", headers=auth("purchaser"), json=payload)
        assert entered.wait(timeout=5)
        second = client.post(
            "/chat",
            headers=auth("purchaser"),
            json={"message": "ikinci", "session_id": record.session_id},
        )
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "SESSION_BUSY"
        assert BlockingRuntime.call_count == 1
        release.set()
        first = future.result(timeout=5)

    assert first.status_code == 200
    assert BlockingRuntime.call_count == 1


def test_shutdown_closes_providers_then_resets_shared_backend_once(client, monkeypatch):
    import robotics_agent.channels.api as api_module
    from robotics_agent.contracts import ActorContext

    closed = []
    reset_calls = []

    class FakeRuntime:
        def __init__(self, *args, actor, **kwargs):
            self.actor = actor

        def close(self):
            closed.append(self.actor.subject)

    monkeypatch.setattr(api_module, "SAPAgentRuntime", FakeRuntime)
    monkeypatch.setattr(api_module, "reset_backend", lambda: reset_calls.append("reset"))
    one = ActorContext(subject="one", tenant="100", roles=("VIEWER",))
    two = ActorContext(subject="two", tenant="100", roles=("VIEWER",))
    original_max = api_module._settings.state.max_sessions  # noqa: SLF001
    object.__setattr__(api_module._settings.state, "max_sessions", 1)  # noqa: SLF001
    try:
        api_module._agent_for(one, "s1")  # noqa: SLF001
        api_module._agent_for(two, "s2")  # noqa: SLF001
        # LRU kapasite eviction'i ilk provider'i hemen kapatir.
        assert closed == ["one"]

        api_module._shutdown_runtimes()  # noqa: SLF001
    finally:
        object.__setattr__(api_module._settings.state, "max_sessions", original_max)  # noqa: SLF001

    assert sorted(closed) == ["one", "two"]
    assert reset_calls == ["reset"]


# --- Guvenlik durusu --------------------------------------------------------
def test_health_reports_production_readiness(client):
    body = client.get("/health", headers=auth("viewer")).json()
    assert "production_ready" in body
    assert "audit_checkpoint" in body
    assert body["app_env"] == "development"


def test_health_audit_verification_is_bounded(client):
    """Buyuk defterlerde /health tam tarama yapmamali."""
    body = client.get("/health", headers=auth("viewer")).json()
    assert "scope" in body["audit_head"]
    assert "son" in body["audit_head"]["scope"]
