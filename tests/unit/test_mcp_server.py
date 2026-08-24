"""MCP sunucu cephesinin yonetisim davranisi.

Bu testler MCP TASIMASINI kurmaz. Cephenin degeri tasimada degil, tasimanin
arkasindaki kapilarda: hangi tool bildiriliyor, adiyla cagrilirsa ne oluyor,
policy hala isliyor mu. `run_tool` ve `exposed_tool_names` bu yuzden tasima
katmanindan ayri tutulmustur.
"""

from __future__ import annotations

import builtins
import json

import pytest

from certaops.mcp_server import build_context, exposed_tool_names, main, run_isolated_tool, run_tool
from robotics_agent.config import get_settings
from robotics_agent.contracts import ActorContext
from robotics_agent.tools import load_all_tools
from robotics_agent.tools.registry import REGISTRY


@pytest.fixture
def mcp_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SAP_BACKEND", "mock")
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CERTAOPS_MCP_ALLOW_WRITE", raising=False)
    monkeypatch.setenv("AGENT_DISABLED_TOOLS", "")
    load_all_tools()
    settings = get_settings(reload=True)
    settings.ensure_dirs()
    actor = ActorContext.local_operator(
        subject="mcp@firma.test", tenant=settings.sap.tenant,
        roles=("PURCHASER", "APPROVER", "AUDITOR"),
        company_code=settings.sap.company_code, plant=settings.sap.plant,
        purchasing_org=settings.sap.purch_org,
    )
    yield settings, actor
    get_settings(reload=True)


def test_yazma_toollari_varsayilan_olarak_bildirilmez(mcp_env):
    """stdio'da istek basina kimlik yok; yazma varsayilan kapali olmali."""
    settings, actor = mcp_env
    names = exposed_tool_names(settings, actor)

    assert names, "hicbir tool bildirilmedi"
    assert all(not REGISTRY[n].risk_tier.is_mutating for n in names)
    assert "sap_pr_submit" not in names
    assert "sap_generate_report" not in names, "dosya olusturan tool salt-okunur degildir"
    assert "sap_pr_prepare" in names, "taslak hazirlama yazma degildir, kapanmamali"


def test_bildirilmeyen_yazma_toolu_adiyla_cagrilinca_da_reddedilir(mcp_env):
    """Gorunurluk filtresi tek basina yeterli degildir.

    Istemci tool adini tahmin edip dogrudan cagirabilir; kapi cagri aninda da
    tutmalidir.
    """
    settings, actor = mcp_env
    ctx = build_context(settings, actor)

    payload, is_error = run_tool(
        "sap_pr_submit",
        {"items": [{"material_id": "X", "quantity": 1}], "idempotency_key": "mcp:test:v1"},
        ctx,
    )

    assert is_error
    assert json.loads(payload)["denial_code"] == "WRITE_DISABLED_ON_MCP"


def test_bildirilmeyen_dosya_toolu_adiyla_cagrilinca_da_reddedilir(mcp_env):
    settings, actor = mcp_env
    ctx = build_context(settings, actor)

    payload, is_error = run_tool(
        "sap_generate_report", {"title": "MCP", "format": "xlsx"}, ctx
    )

    assert is_error
    assert json.loads(payload)["denial_code"] == "WRITE_DISABLED_ON_MCP"


def test_izole_cagri_backend_kapatir(mcp_env, monkeypatch):
    settings, actor = mcp_env
    ctx = build_context(settings, actor)
    closed: list[bool] = []
    original_close = ctx.sap.close
    ctx.sap.close = lambda: (closed.append(True), original_close())[1]
    monkeypatch.setattr("certaops.mcp_server.build_context", lambda *_: ctx)

    payload, is_error = run_isolated_tool(
        settings, actor, "sap_search_materials", {"limit": 1}
    )

    assert not is_error, payload
    assert closed == [True]


def test_bayrak_acikken_yazma_toolu_bildirilir(mcp_env, monkeypatch):
    settings, actor = mcp_env
    monkeypatch.setenv("CERTAOPS_MCP_ALLOW_WRITE", "1")

    names = exposed_tool_names(settings, actor)

    assert "sap_pr_submit" in names


def test_bayrak_acik_olsa_bile_onay_kapisi_calisir(mcp_env, monkeypatch):
    """Yazma bayragi tool'u GORUNUR kilar; yazma iznini VERMEZ.

    Onay tutar esigine baglidir (`SAP_APPROVAL_THRESHOLD`): esigin ustundeki
    bir talep dogrulanmis onay kaydi olmadan gecmemelidir. MCP cephesi bu
    kapiyi zayiflatmaz.
    """
    settings, actor = mcp_env
    monkeypatch.setenv("CERTAOPS_MCP_ALLOW_WRITE", "1")
    monkeypatch.setenv("SAP_DRY_RUN", "false")
    settings = get_settings(reload=True)
    settings.ensure_dirs()
    ctx = build_context(settings, actor)

    # Esigi asacak kadar buyuk bir talep: onay kaydi zorunlu hale gelir.
    payload, is_error = run_tool(
        "sap_pr_submit",
        {"items": [{"material_id": "SFT-SCN-270", "quantity": 500}],
         "idempotency_key": "mcp:onaysiz:v1"},
        ctx,
    )

    assert is_error, "esigin ustunde onaysiz yazma gecmemeli"
    code = json.loads(payload).get("denial_code", "")
    assert code != "WRITE_DISABLED_ON_MCP", "bu sefer MCP bayragi degil, ONAY kapisi tutmali"
    assert code, f"reddin gerekcesi bildirilmeli, gelen: {payload[:200]}"


def test_esik_altindaki_talep_onay_istemez(mcp_env, monkeypatch):
    """Onay evrensel degil, ORANTILI: kucuk talep insani beklemez.

    Bu davranisi sabitlemek onemli - aksi halde biri "her yazma onay ister"
    varsayimiyla esigi kaldirabilir.
    """
    settings, actor = mcp_env
    monkeypatch.setenv("CERTAOPS_MCP_ALLOW_WRITE", "1")
    monkeypatch.setenv("SAP_DRY_RUN", "false")
    settings = get_settings(reload=True)
    settings.ensure_dirs()
    ctx = build_context(settings, actor)

    payload, is_error = run_tool(
        "sap_pr_submit",
        {"items": [{"material_id": "SFT-SCN-270", "quantity": 1}],
         "idempotency_key": "mcp:kucuk:v1"},
        ctx,
    )

    assert not is_error, payload[:300]
    assert json.loads(payload)["write_status"] == "created"


def test_operator_kapatma_anahtari_mcp_cephesinde_de_gecerli(mcp_env, monkeypatch):
    """AGENT_DISABLED_TOOLS her cephede ayni etkiyi yapmali."""
    settings, actor = mcp_env
    monkeypatch.setenv("AGENT_DISABLED_TOOLS", "sap_material_360")
    settings = get_settings(reload=True)

    names = exposed_tool_names(settings, actor)
    assert "sap_material_360" not in names
    assert "sap_search_materials" in names

    ctx = build_context(settings, actor)
    payload, is_error = run_tool("sap_material_360", {"material_id": "SFT-SCN-270"}, ctx)
    assert is_error
    assert json.loads(payload)["denial_code"] == "TOOL_DISABLED"


def test_mcp_extra_eksikse_temiz_kurulum_mesaji_verir(monkeypatch, capsys):
    """Eksik opsiyonel paket uzun bir async traceback uretmemeli."""
    real_import = builtins.__import__

    def reject_mcp(name, *args, **kwargs):
        if name == "mcp":
            raise ModuleNotFoundError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_mcp)

    assert main([]) == 2
    assert "pip install -e '.[mcp]'" in capsys.readouterr().err


def test_okuma_cagrisi_policy_ve_dlp_den_geciyor(mcp_env):
    """Cephe `execute_tool`u atlamiyor: sonuc evidence/audit uretmis olmali."""
    settings, actor = mcp_env
    ctx = build_context(settings, actor)

    payload, is_error = run_tool("sap_search_materials", {"limit": 3}, ctx)

    assert not is_error
    body = json.loads(payload)
    assert body, "bos sonuc"
    # execute_tool her cagriyi denetim defterine yazar.
    assert ctx.audit is not None
    assert ctx.execution is not None


def test_bilinmeyen_tool_temiz_hata_dondurur(mcp_env):
    settings, actor = mcp_env
    ctx = build_context(settings, actor)

    payload, is_error = run_tool("sap_hayali_tool", {}, ctx)

    assert is_error
    assert json.loads(payload)["denial_code"] == "UNKNOWN_TOOL"
