"""Ayar uclarinin HTTP sozlesmesi.

Servis katmani ayri sinaniyor cunku kapinin dogru calismasi yetmez: yanlis
bir durum kodu ya da govdesi, arayuzun "reddedildi"yi "basarili" gostermesine
yol acar. Burada dogrulanan sey, kapinin karari ile istemcinin gordugu sey
arasinda fark olmadigi.

Arayuz KAPALIYKEN bu uclarin 404 dondurmesi de burada: kapatilmis bir yonetim
yuzeyi 403 dondurmemeli, cunku 403 varligini ele verir.
"""
from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from robotics_agent.channels.auth import hash_token

TOKENS = {
    "admin": "tok-cfg-admin-secret",
    "admin2": "tok-cfg-admin2-secret",
    "purchaser": "tok-cfg-purchaser-secret",
}


@pytest.fixture
def principals_file(tmp_path):
    path = tmp_path / "principals.json"
    path.write_text(
        json.dumps(
            {
                "principals": [
                    {
                        "token_sha256": hash_token(TOKENS["admin"]),
                        "subject": "admin@firma.test",
                        "tenant": "100",
                        "roles": ["PLATFORM_ADMIN"],
                    },
                    {
                        "token_sha256": hash_token(TOKENS["admin2"]),
                        "subject": "ikinci@firma.test",
                        "tenant": "100",
                        "roles": ["PLATFORM_ADMIN"],
                    },
                    {
                        "token_sha256": hash_token(TOKENS["purchaser"]),
                        "subject": "alici@firma.test",
                        "tenant": "100",
                        "roles": ["PURCHASER"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _env(monkeypatch, tmp_path, principals_file):
    monkeypatch.setenv("AGENT_AUTH_MODE", "static_token")
    monkeypatch.setenv("AGENT_PRINCIPALS_FILE", str(principals_file))
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("SAP_BACKEND", "mock")
    monkeypatch.setenv("SAP_ALLOWED_HOSTS", "s4.firma.test")
    monkeypatch.setenv("AGENT_PSEUDONYMIZATION_KEY_ID", "kms://test-v1")
    monkeypatch.setenv("AGENT_KMS_KEY_ID", "kms://test-data-v1")
    # Ayarlarin kabuktan gelmis sayilmamasi icin: `_SHELL_ENV` modul yuklenirken
    # alindi, bu degiskenler orada yok.
    from robotics_agent.runtime_config import SETTABLE

    for anahtar in SETTABLE:
        monkeypatch.delenv(anahtar, raising=False)
    monkeypatch.setenv("SAP_BACKEND", "mock")


def _reload_api():
    import robotics_agent.config as config_module

    config_module._settings = None  # noqa: SLF001
    return importlib.reload(importlib.import_module("robotics_agent.channels.api"))


@pytest.fixture
def client(tmp_path, principals_file, monkeypatch):
    _env(monkeypatch, tmp_path, principals_file)
    monkeypatch.setenv("AGENT_UI_ENABLED", "true")
    api = _reload_api()
    with TestClient(api.app) as c:
        yield c


@pytest.fixture
def disabled_client(tmp_path, principals_file, monkeypatch):
    _env(monkeypatch, tmp_path, principals_file)
    monkeypatch.setenv("AGENT_UI_ENABLED", "false")
    api = _reload_api()
    with TestClient(api.app) as c:
        yield c


def auth(rol: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[rol]}"}


# --- Kimlik ve kapsam -------------------------------------------------------
def test_kimliksiz_istek_401(client):
    assert client.get("/ui/settings").status_code == 401
    assert client.post("/ui/settings", json={"key": "LOG_LEVEL", "value": "ERROR"}).status_code == 401


def test_kapsamsiz_actor_okuyabilir_degistiremez(client):
    okuma = client.get("/ui/settings", headers=auth("purchaser"))
    assert okuma.status_code == 200
    assert okuma.json()["can_change"] is False

    yazma = client.post(
        "/ui/settings", json={"key": "LOG_LEVEL", "value": "ERROR"},
        headers=auth("purchaser"),
    )
    assert yazma.status_code == 403
    assert yazma.json()["detail"]["code"] == "MISSING_SCOPE"


def test_admin_okuyabilir_ve_degistirebilir(client):
    okuma = client.get("/ui/settings", headers=auth("admin"))
    assert okuma.status_code == 200
    govde = okuma.json()
    assert govde["can_change"] is True
    assert govde["required_scope"] == "platform.config"
    assert len(govde["settings"]) >= 10


# --- Arayuz kapaliyken ------------------------------------------------------
def test_arayuz_kapaliyken_404(disabled_client):
    """Kapatilmis bir yonetim yuzeyi varligini ele vermemeli."""
    assert disabled_client.get("/ui/settings", headers=auth("admin")).status_code == 404
    assert disabled_client.post(
        "/ui/settings", json={"key": "LOG_LEVEL", "value": "ERROR"},
        headers=auth("admin"),
    ).status_code == 404


# --- Izin listesi ve deger sinirlari ----------------------------------------
@pytest.mark.parametrize(
    "anahtar,deger",
    [
        ("AGENT_AUTH_MODE", "none"),
        ("SAP_PASSWORD", "sizinti"),
        ("SAP_ALLOWED_HOSTS", "*"),
        ("LOG_MASK", "false"),
        ("ANTHROPIC_API_KEY", "sk-kotu"),
    ],
)
def test_izin_listesi_disi_anahtar_400(anahtar, deger, client):
    yanit = client.post(
        "/ui/settings", json={"key": anahtar, "value": deger}, headers=auth("admin")
    )
    assert yanit.status_code == 400
    assert yanit.json()["detail"]["code"] == "INVALID_SETTING"


def test_yasak_deger_400(client):
    yanit = client.post(
        "/ui/settings", json={"key": "AGENT_DLP_MODE", "value": "off"},
        headers=auth("admin"),
    )
    assert yanit.status_code == 400


def test_sinir_disi_deger_400(client):
    yanit = client.post(
        "/ui/settings", json={"key": "SAP_PAGE_SIZE", "value": 99999},
        headers=auth("admin"),
    )
    assert yanit.status_code == 400


# --- Mandal -----------------------------------------------------------------
def test_uretim_kapisini_kiran_degisiklik_409(client):
    """Mandal HTTP'de de goze gorunur bir cakisma olarak doner."""
    yanit = client.post(
        "/ui/settings", json={"key": "AGENT_RISK_SCORING_MODE", "value": "report"},
        headers=auth("admin"),
    )
    assert yanit.status_code == 409
    govde = yanit.json()["detail"]
    assert govde["code"] == "WOULD_BREAK_PRODUCTION_GATE"
    assert govde["detail"], "hangi engelin dogacagi istemciye soylenmeli"


# --- Rutin degisiklik -------------------------------------------------------
def test_rutin_degisiklik_uygulanir(client):
    yanit = client.post(
        "/ui/settings", json={"key": "SAP_PAGE_SIZE", "value": 150},
        headers=auth("admin"),
    )
    assert yanit.status_code == 200
    govde = yanit.json()
    assert govde["status"] == "staged"
    assert govde["restart_required"] is True
    assert govde["before"] == 100
    assert govde["after"] == "150"

    # Okuma ucu degisikligi gosterir.
    tablo = client.get("/ui/settings", headers=auth("admin")).json()
    alan = next(a for a in tablo["settings"] if a["key"] == "SAP_PAGE_SIZE")
    assert alan["overridden"] is True
    assert alan["changed_by"] == "admin@firma.test"


def test_canli_ayar_aninda_uygulanir(client):
    yanit = client.post(
        "/ui/settings", json={"key": "LOG_LEVEL", "value": "ERROR"},
        headers=auth("admin"),
    )
    assert yanit.status_code == 200
    assert yanit.json()["status"] == "applied"
    assert yanit.json()["restart_required"] is False


# --- Iki kisi kurali --------------------------------------------------------
def test_sonuc_doguran_ayar_onay_bekler(client):
    yanit = client.post(
        "/ui/settings", json={"key": "AGENT_APPROVAL_GATEWAY", "value": "bpa",
                              "reason": "Uretime hazirlik"},
        headers=auth("admin"),
    )
    assert yanit.status_code == 200
    govde = yanit.json()
    assert govde["status"] == "pending_approval"
    assert govde["change_id"]

    tablo = client.get("/ui/settings", headers=auth("admin")).json()
    assert len(tablo["pending"]) == 1
    assert tablo["pending"][0]["requested_by"] == "admin@firma.test"
    assert tablo["pending"][0]["reason"] == "Uretime hazirlik"


def test_oneren_onaylayamaz_403(client):
    onerilen = client.post(
        "/ui/settings", json={"key": "AGENT_APPROVAL_GATEWAY", "value": "bpa"},
        headers=auth("admin"),
    ).json()
    yanit = client.post(
        "/ui/settings/approve", json={"change_id": onerilen["change_id"]},
        headers=auth("admin"),
    )
    assert yanit.status_code == 403
    assert yanit.json()["detail"]["code"] == "SOD_VIOLATION"


def test_kapsamsiz_onaylayamaz_403(client):
    onerilen = client.post(
        "/ui/settings", json={"key": "AGENT_APPROVAL_GATEWAY", "value": "bpa"},
        headers=auth("admin"),
    ).json()
    yanit = client.post(
        "/ui/settings/approve", json={"change_id": onerilen["change_id"]},
        headers=auth("purchaser"),
    )
    assert yanit.status_code == 403
    assert yanit.json()["detail"]["code"] == "MISSING_SCOPE"


def test_ikinci_kimlik_onaylar(client):
    onerilen = client.post(
        "/ui/settings", json={"key": "AGENT_APPROVAL_GATEWAY", "value": "bpa"},
        headers=auth("admin"),
    ).json()
    yanit = client.post(
        "/ui/settings/approve", json={"change_id": onerilen["change_id"]},
        headers=auth("admin2"),
    )
    assert yanit.status_code == 200
    assert yanit.json()["status"] in {"staged", "applied"}

    tablo = client.get("/ui/settings", headers=auth("admin")).json()
    assert tablo["pending"] == []
    alan = next(a for a in tablo["settings"] if a["key"] == "AGENT_APPROVAL_GATEWAY")
    assert alan["overridden"] is True


def test_bilinmeyen_change_id_404(client):
    yanit = client.post(
        "/ui/settings/approve", json={"change_id": "cfg_yok"}, headers=auth("admin2")
    )
    assert yanit.status_code == 404


def test_geri_cekilen_degisiklik_kaybolur(client):
    onerilen = client.post(
        "/ui/settings", json={"key": "AGENT_APPROVAL_GATEWAY", "value": "bpa"},
        headers=auth("admin"),
    ).json()
    assert client.post(
        "/ui/settings/cancel", json={"change_id": onerilen["change_id"]},
        headers=auth("admin"),
    ).status_code == 200
    assert client.get("/ui/settings", headers=auth("admin")).json()["pending"] == []


# --- Sizinti ----------------------------------------------------------------
def test_okuma_ucu_sir_dondurmez(client):
    govde = json.dumps(client.get("/ui/settings", headers=auth("admin")).json()["settings"])
    for iz in ("password", "secret", "api_key", "token", "sk-test"):
        assert iz not in govde.lower()


def test_guvenlik_basliklari_korunur(client):
    """Ayar uclari da arayuzun CSP/nosniff sozlesmesinden gecmeli."""
    yanit = client.get("/ui/settings", headers=auth("admin"))
    assert yanit.headers.get("X-Content-Type-Options") == "nosniff"
