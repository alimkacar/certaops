"""Operator arayuzu: kimlik dogrulama kapisi, kapsam kontrolu ve CSP.

Arayuz **hicbir yeni yetki tanimlamaz**. Buradaki testlerin isi bunu
dogrulamak: arayuzden gorulen sey `curl` ile gorulenle ayni olmali, ve
arayuz kapaliyken tek bir uc nokta bile cevap vermemeli.
"""

from __future__ import annotations

import importlib
import json
import logging

import pytest
from fastapi.testclient import TestClient

from robotics_agent.channels.auth import hash_token

TOKENS = {
    "auditor": "tok-ui-auditor-secret",
    "viewer": "tok-ui-viewer-secret",
}


@pytest.fixture
def principals_file(tmp_path):
    path = tmp_path / "principals.json"
    path.write_text(
        json.dumps(
            {
                "principals": [
                    {
                        "token_sha256": hash_token(TOKENS["auditor"]),
                        "subject": "denetci@firma.test",
                        "tenant": "100",
                        "roles": ["AUDITOR"],
                    },
                    {
                        "token_sha256": hash_token(TOKENS["viewer"]),
                        "subject": "okur@firma.test",
                        "tenant": "100",
                        "roles": ["VIEWER"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _base_env(monkeypatch, tmp_path, principals_file):
    monkeypatch.setenv("AGENT_AUTH_MODE", "static_token")
    monkeypatch.setenv("AGENT_PRINCIPALS_FILE", str(principals_file))
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("SAP_BACKEND", "mock")
    monkeypatch.setenv("SAP_ALLOWED_HOSTS", "s4.firma.test")
    monkeypatch.setenv("AGENT_SESSION_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_DLP_MODE", "enforce")
    monkeypatch.setenv("AGENT_RISK_SCORING_MODE", "enforce")
    monkeypatch.setenv("AGENT_PSEUDONYMIZATION_KEY_ID", "kms://test-pseudonym-v1")
    monkeypatch.setenv("AGENT_KMS_KEY_ID", "kms://test-data-key-v1")
    monkeypatch.setenv("AGENT_D3_CACHE_ENABLED", "false")


def _reload_api():
    import robotics_agent.config as config_module

    config_module._settings = None  # noqa: SLF001 - test icin singleton sifirlama
    return importlib.reload(importlib.import_module("robotics_agent.channels.api"))


@pytest.fixture
def client(tmp_path, principals_file, monkeypatch):
    """Arayuzu acik bir servis. `with` kullanilir: lifespan loglamayi kurar."""
    _base_env(monkeypatch, tmp_path, principals_file)
    monkeypatch.setenv("AGENT_UI_ENABLED", "true")
    api = _reload_api()
    with TestClient(api.app) as test_client:
        yield test_client


@pytest.fixture
def disabled_client(tmp_path, principals_file, monkeypatch):
    _base_env(monkeypatch, tmp_path, principals_file)
    monkeypatch.setenv("AGENT_UI_ENABLED", "false")
    api = _reload_api()
    with TestClient(api.app) as test_client:
        yield test_client


def auth(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[role]}"}


# --- Statik kabuk ----------------------------------------------------------
def test_root_redirects_to_the_console(client):
    """Servisin adresini yazan operator JSON bir 404 ile karsilasmamali."""
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui"


def test_root_reaches_the_console_after_redirect(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_ui_shell_is_served_without_a_token(client):
    """Token girecegi ekrani gormek icin token istenemez.

    Kabuk hicbir veri tasimaz; veri tasiyan her uc ayrica dogrulanir.
    """
    response = client.get("/ui")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "certaops.token" not in response.text  # token yalnizca istemcide uretilir


def test_ui_responses_carry_a_strict_csp(client):
    for path in ("/ui", "/ui/assets/app.js"):
        headers = client.get(path).headers
        csp = headers["content-security-policy"]
        assert "default-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["cache-control"] == "no-store"


def test_ui_assets_are_served(client):
    assert client.get("/ui/assets/styles.css").status_code == 200
    assert client.get("/ui/assets/app.js").status_code == 200


# --- Yapilandirma ucu ------------------------------------------------------
def test_ui_config_requires_authentication(client):
    assert client.get("/ui/config").status_code == 401


def test_ui_config_reports_actor_scopes(client):
    body = client.get("/ui/config", headers=auth("auditor")).json()
    assert body["actor"]["subject"] == "denetci@firma.test"
    assert body["can_read_audit"] is True
    assert body["mode"] == "simulation"


def test_ui_config_marks_viewer_without_audit_scope(client):
    body = client.get("/ui/config", headers=auth("viewer")).json()
    assert body["can_read_audit"] is False


# --- MCP baglanti teshisi --------------------------------------------------
def test_mcp_status_requires_authentication(client):
    assert client.get("/ui/mcp").status_code == 401
    assert client.post("/ui/mcp/test").status_code == 401


def test_mcp_status_is_read_only_and_contains_no_secrets(client):
    response = client.get("/ui/mcp", headers=auth("viewer"))
    assert response.status_code == 200
    body = response.json()

    assert body["server_name"] == "certaops"
    assert body["transport"] == "stdio"
    assert body["ui_channel"] == "http_api"
    assert body["ui_uses_mcp"] is False
    assert body["security"]["write_tools_exposed"] is False
    assert body["security"]["secrets_in_response"] is False
    assert body["client_config"]["mcpServers"]["certaops"]["env"]["SAP_READ_ONLY"] == "true"
    assert "sap_pr_submit" not in body["tools"]["names"]

    serialized = json.dumps(body)
    assert "sk-test-not-used" not in serialized
    for forbidden in ("SAP_PASSWORD", "SAP_API_KEY", "SAP_OAUTH_CLIENT_SECRET"):
        assert forbidden not in serialized


def test_mcp_probe_endpoint_runs_initialize_and_list_contract(client, monkeypatch):
    async def fake_probe(_settings):
        return {
            "ok": True,
            "server_name": "certaops",
            "server_version": "test",
            "protocol_version": "2025-11-25",
            "transport": "stdio",
            "duration_ms": 12,
            "tool_count": 2,
            "tools": ["sap_connection_health", "sap_search_materials"],
            "write_tools_exposed": False,
            "write_tools": [],
            "read_only_forced": True,
            "sap_calls": 0,
        }

    monkeypatch.setattr("robotics_agent.channels.ui.probe_stdio", fake_probe)
    response = client.post("/ui/mcp/test", headers=auth("viewer"))

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["read_only_forced"] is True
    assert body["sap_calls"] == 0
    assert body["write_tools_exposed"] is False


# --- Denetim ---------------------------------------------------------------
def test_audit_recent_requires_audit_scope(client):
    response = client.get("/audit/recent", headers=auth("viewer"))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "MISSING_SCOPE"


def test_audit_recent_is_scoped_to_the_actor_tenant(client):
    body = client.get("/audit/recent", headers=auth("auditor")).json()
    assert body["tenant"] == "100"
    assert body["chain"]["valid"] is True
    assert all(entry.get("tenant", "100") == "100" for entry in body["entries"])


# --- Loglar ----------------------------------------------------------------
def test_logs_require_audit_scope(client):
    assert client.get("/logs", headers=auth("viewer")).status_code == 403


def test_logs_return_masked_buffer_entries(client):
    logging.getLogger("robotics_agent.test").warning("musteri ali@firma.test giris yapti")
    body = client.get("/logs?level=WARNING", headers=auth("auditor")).json()
    assert body["masked"] is True
    messages = [entry["message"] for entry in body["entries"]]
    assert any("a***@firma.test" in m for m in messages)
    assert not any("ali@firma.test" in m for m in messages)


def test_logs_carry_the_request_correlation_id(client):
    """Middleware'de baglanan correlation ID istegin loglarina gecmeli."""
    client.get("/tools", headers={**auth("auditor"), "X-Correlation-ID": "corr-ui-test"})
    body = client.get("/logs?level=DEBUG&limit=200", headers=auth("auditor")).json()
    assert any(entry.get("channel") == "api" for entry in body["entries"])


def test_invalid_log_level_is_rejected(client):
    response = client.get("/logs?level=TRACE", headers=auth("auditor"))
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "INVALID_LOG_LEVEL"


def test_log_limit_is_capped_by_settings(client):
    body = client.get("/logs?limit=1000", headers=auth("auditor")).json()
    assert body["limit"] <= 200


# --- Kapali arayuz ---------------------------------------------------------
def test_disabled_ui_registers_no_endpoints(disabled_client):
    """Kapatilmis bir ozellik 403 degil 404 dondurmeli.

    403, ozelligin var oldugunu ve yalnizca yetkinin eksik oldugunu soyler.
    Kapaliysa disaridan varligi hic anlasilmamali.
    """
    for path in ("/", "/ui", "/ui/config", "/ui/mcp", "/ui/mcp/test", "/logs", "/audit/recent"):
        response = disabled_client.get(path, headers=auth("auditor"), follow_redirects=False)
        assert response.status_code == 404, path


def test_disabled_ui_keeps_the_rest_of_the_api(disabled_client):
    assert disabled_client.get("/tools", headers=auth("auditor")).status_code == 200
