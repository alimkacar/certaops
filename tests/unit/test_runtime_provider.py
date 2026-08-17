"""Tek runtime + saglayici-bagimsiz model katmaninin davranis testleri.

Hicbiri canli API anahtari istemez: ``FakeModelProvider`` gercek runtime'i
surer. Test edilen sey model kalitesi degil, **guvenlik ve maliyet
degismezleri**:

  * modelin onerdigi her cagri `execute_tool` uzerinden gecer,
  * yetkisiz/bilinmeyen tool fail-closed reddedilir,
  * cok domainli istek tek provider dongusu kullanir,
  * ayni call_id iki kez gelirse mutating tool bir kez calisir,
  * D2 maskelenmeden, D3 hicbir zaman saglayiciya gitmez.
"""

from __future__ import annotations

import json

import pytest

from certaops.providers import (
    FakeModelProvider,
    ModelRequest,
    ModelTimeoutError,
)
from certaops.providers.fake import reply, tool_call
from certaops.runtime import SAPAgentRuntime
from certaops.runtime.profiles import DOMAIN_PROFILES, profiles_for_packs


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SAP_BACKEND", "mock")
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENT_DIRECT_ANSWERS", "true")
    monkeypatch.setenv("SAP_DRY_RUN", "true")
    from robotics_agent.config import get_settings

    return get_settings(reload=True)


def build(env, script, *, roles=("VIEWER", "PURCHASER"), **kwargs):
    from robotics_agent.contracts import ActorContext

    provider = FakeModelProvider(script)
    actor = ActorContext.local_operator(
        subject="test-user", tenant=env.sap.tenant, roles=roles
    )
    runtime = SAPAgentRuntime(env, provider=provider, actor=actor, **kwargs)
    return runtime, provider


# --- 1. Direct-answer yolu saglayiciyi cagirmaz -----------------------------
def test_basit_stok_sorgusu_saglayiciyi_cagirmaz(env):
    runtime, provider = build(env, [])
    turn = runtime.chat("HD-GEAR-CSF25-100 stok durumu")
    assert turn.direct_answer is True
    assert turn.model_calls == 0
    assert provider.call_count == 0, "Gemini/saglayici cagrilmamaliydi"


# --- 2. Cok domainli istek TEK provider dongusu kullanir --------------------
def test_cok_domainli_istek_tek_runtime_kullanir(env):
    """Eskiden her domain ayri agent + ayri LLM turu demekti."""
    runtime, provider = build(env, [reply("Ozet hazir.")])
    turn = runtime.chat(
        "HD-GEAR-CSF25-100 icin stok durumu ve R-2026-014 projesinin maliyet durumu"
    )
    assert len(turn.active_domains) > 1, "birden fazla domain acilmali"
    assert provider.call_count == 1, "cok domain icin tek saglayici cagrisi"
    assert turn.model_calls == 1


def test_domain_ayrimi_prompt_ve_pack_olarak_korunur(env):
    runtime, provider = build(env, [reply("ok")])
    runtime.chat("stok ve proje maliyeti")
    request = provider.last_request
    assert "SAP domainleri" in request.system
    # Acik domainlerin gorevleri TEK prompt'ta birlesir.
    opened = profiles_for_packs(runtime.active_packs)
    assert len(opened) >= 1
    for profile in opened:
        assert profile.title in request.system
        assert profile.mission[:40] in request.system
    # Acik olmayan bir domainin gorevi prompt'a sizmaz.
    closed = set(DOMAIN_PROFILES) - {p.key for p in opened}
    for key in closed:
        assert DOMAIN_PROFILES[key].mission[:40] not in request.system


# --- 3. Yetkisiz tool modele HIC gosterilmez --------------------------------
def test_yetkisiz_pr_submit_modele_gosterilmez(env):
    runtime, provider = build(env, [reply("ok")], roles=("VIEWER",))
    runtime.chat("HD-GEAR-CSF25-100 icin satinalma talebi olustur")
    offered = provider.offered_tools()
    assert "sap_pr_submit" not in offered
    assert "sap_pr_prepare" not in offered


def test_yetkili_kullaniciya_pr_submit_gosterilir(env):
    runtime, provider = build(env, [reply("ok")], roles=("PURCHASER",))
    runtime.chat("HD-GEAR-CSF25-100 icin satinalma talebi hazirla")
    assert "sap_pr_prepare" in provider.offered_tools()


# --- 4. Bilinmeyen tool fail-closed -----------------------------------------
def test_bilinmeyen_tool_fail_closed_reddedilir(env):
    runtime, provider = build(
        env,
        [tool_call("sap_delete_everything", {"x": 1}), reply("anladim")],
    )
    turn = runtime.chat("stok durumu ve tedarikci karsilastirmasi")
    assert turn.tool_calls[0].is_error is True
    body = json.loads(turn.tool_calls[0].result)
    assert body["denial_code"] == "TOOL_NOT_AVAILABLE"
    # Yetki envanteri sizdirilmaz.
    assert "sap_" not in body["error"]


def test_gorunmeyen_ama_kayitli_tool_da_reddedilir(env):
    """VIEWER icin gorunmeyen bir tool'u model onerirse calistirilmaz."""
    runtime, provider = build(
        env, [tool_call("sap_pr_submit", {"items": [], "idempotency_key": "k"}), reply("ok")],
        roles=("VIEWER",),
    )
    turn = runtime.chat("talep olustur ve gonder")
    assert turn.tool_calls[0].is_error is True
    assert json.loads(turn.tool_calls[0].result)["denial_code"] == "TOOL_NOT_AVAILABLE"


# --- 5/6. Otomatik yurutme yok; her cagri execute_tool'dan gecer ------------
def test_tum_cagriler_execute_tool_uzerinden_gecer(env, monkeypatch):
    seen: list[str] = []
    import certaops.runtime.agent as runtime_module

    original = runtime_module.execute_tool

    def spy(name, arguments, ctx):
        seen.append(name)
        return original(name, arguments, ctx)

    monkeypatch.setattr(runtime_module, "execute_tool", spy)
    runtime, _ = build(
        env,
        [tool_call("sap_material_360", {"material_id": "HD-GEAR-CSF25-100"}), reply("ok")],
    )
    runtime.chat("malzeme detayi ve tedarikci karsilastirmasi ver")
    assert seen == ["sap_material_360"]


def test_saglayiciya_cagrilabilir_nesne_gonderilmez(env):
    """Function declaration'lar saf semadir; SDK bir handler goremez."""
    runtime, provider = build(env, [reply("ok")])
    runtime.chat("stok durumu ve proje maliyeti")
    for declaration in provider.last_request.functions:
        assert isinstance(declaration.parameters, dict)
        assert not callable(getattr(declaration, "handler", None))
        assert not callable(declaration), "declaration cagrilabilir olmamali"


# --- 7/8. DLP: D2 maskelenir, D3 saglayiciya gitmez -------------------------
def test_tool_sonuclari_dlp_den_gecerek_saglayiciya_gider(env):
    runtime, provider = build(
        env,
        [tool_call("sap_compare_vendors", {"material_id": "HD-GEAR-CSF25-100", "quantity": 2}),
         reply("ozet")],
    )
    runtime.chat("tedarikci karsilastirmasi ve proje etkisi")
    sent = "\n".join(provider.sent_function_results())
    assert sent, "tool sonucu saglayiciya gonderilmeliydi"
    # D3 sinifi degerler (IBAN/vergi no) ham halde gecemez.
    assert "TR33" not in sent
    assert "@" not in sent or "e-posta" in sent.lower()


def test_d3_veri_saglayiciya_gonderilmez(env, monkeypatch):
    """DLP reddi tool seviyesinde olur; ham D3 hicbir kosulda modele gitmez."""
    import certaops.runtime.agent as runtime_module

    original = runtime_module.execute_tool
    leaked = {"iban": "TR330006100519786457841326", "tax_no": "1234567890"}

    def fake_execute(name, arguments, ctx):
        payload, is_error = original(name, arguments, ctx)
        body = json.loads(payload)
        assert "TR330006100519786457841326" not in json.dumps(body, ensure_ascii=False)
        return payload, is_error

    monkeypatch.setattr(runtime_module, "execute_tool", fake_execute)
    runtime, provider = build(
        env,
        [tool_call("sap_material_360", {"material_id": "HD-GEAR-CSF25-100"}), reply("ok")],
    )
    runtime.chat("malzeme detayi ve tedarikci skorlari")
    sent = "\n".join(provider.sent_function_results())
    assert leaked["iban"] not in sent


# --- 9. Ayni call_id iki kez -> tool bir kez calisir ------------------------
def test_ayni_call_id_mutating_toolu_bir_kez_calistirir(env, monkeypatch):
    calls: list[str] = []
    import certaops.runtime.agent as runtime_module

    original = runtime_module.execute_tool

    def spy(name, arguments, ctx):
        calls.append(name)
        return original(name, arguments, ctx)

    monkeypatch.setattr(runtime_module, "execute_tool", spy)

    args = {
        "items": [{"material_id": "HD-GEAR-CSF25-100", "quantity": 1}],
        "idempotency_key": "test:dedup:v1",
    }
    runtime, _ = build(
        env,
        [
            tool_call("sap_pr_submit", args, call_id="same-call-1"),
            tool_call("sap_pr_submit", args, call_id="same-call-1"),
            reply("bitti"),
        ],
        roles=("PURCHASER",),
    )
    runtime.chat("satinalma talebi olustur ve maliyet etkisini yaz")
    assert calls.count("sap_pr_submit") == 1, "ayni call_id tekrar calistirilmamali"


def test_mutating_tool_ayni_turda_iki_farkli_call_id_ile_de_tekrarlanmaz(env, monkeypatch):
    calls: list[str] = []
    import certaops.runtime.agent as runtime_module

    original = runtime_module.execute_tool

    def spy(name, arguments, ctx):
        calls.append(name)
        return original(name, arguments, ctx)

    monkeypatch.setattr(runtime_module, "execute_tool", spy)
    args = {
        "items": [{"material_id": "HD-GEAR-CSF25-100", "quantity": 1}],
        "idempotency_key": "test:dedup:v2",
    }

    def script(_request: ModelRequest):
        if len(calls) == 0:
            return tool_call("sap_pr_submit", args, call_id="c1")
        if len(calls) == 1:
            return tool_call("sap_pr_submit", args, call_id="c2")
        return reply("bitti")

    runtime, _ = build(env, script, roles=("PURCHASER",))
    runtime.chat("satinalma talebi olustur ve proje etkisini de yaz")
    assert calls.count("sap_pr_submit") == 1


# --- 10. Saglayici timeout'u SAP islemini tekrarlamaz -----------------------
def test_saglayici_timeouti_sap_yazmasini_tekrarlamaz(env, monkeypatch):
    calls: list[str] = []
    import certaops.runtime.agent as runtime_module

    original = runtime_module.execute_tool

    def spy(name, arguments, ctx):
        calls.append(name)
        return original(name, arguments, ctx)

    monkeypatch.setattr(runtime_module, "execute_tool", spy)

    args = {
        "items": [{"material_id": "HD-GEAR-CSF25-100", "quantity": 1}],
        "idempotency_key": "test:timeout:v1",
    }
    provider = FakeModelProvider(
        [tool_call("sap_pr_submit", args)],
        raise_on=ModelTimeoutError("gecikme", provider="fake"),
        raise_after=1,
    )
    from robotics_agent.contracts import ActorContext

    runtime = SAPAgentRuntime(
        env,
        provider=provider,
        actor=ActorContext.local_operator(
            subject="u", tenant=env.sap.tenant, roles=("PURCHASER",)
        ),
    )
    turn = runtime.chat("satinalma talebi olustur ve proje maliyet etkisini de yaz")
    assert calls.count("sap_pr_submit") == 1, "timeout tool'u tekrarlamamali"
    assert turn.needs_review is True
    assert turn.stop_reason == "provider_error"


# --- 12. API/CLI temel alanlari korunur -------------------------------------
def test_turn_geriye_donuk_alanlari_korur(env):
    runtime, _ = build(env, [reply("ok")])
    turn = runtime.chat("stok ve proje maliyeti")
    for attribute in (
        "text", "tool_calls", "input_tokens", "output_tokens", "iterations",
        "active_packs", "policy_denials", "needs_review", "correlation_id",
        "artifacts", "direct_answer", "model_calls",
    ):
        assert hasattr(turn, attribute), attribute
    # Eski adlar ozellik olarak korunur.
    assert turn.active_agents == turn.active_domains
    assert turn.agent_trace == turn.domain_trace


def test_multi_agent_facade_calisir_ve_uyarir(env):
    from robotics_agent.agent import SAPMultiAgent

    with pytest.warns(DeprecationWarning):
        agent = SAPMultiAgent(env, provider=FakeModelProvider([reply("ok")]))
    turn = agent.chat("stok durumu ve proje maliyeti")
    assert turn.text == "ok"
    assert agent.active_agents


# --- Butce ve compaction ----------------------------------------------------
def test_yalniz_secilmis_tool_semalari_gonderilir(env):
    runtime, provider = build(env, [reply("ok")])
    runtime.chat("baglanti saglik kontrolu yap ve yetenekleri listele")
    offered = set(provider.offered_tools())
    everything = set(runtime.describe_tools() and [t["name"] for t in runtime.describe_tools()])
    assert offered < everything, "tum tool'lar degil, yalniz secilenler gonderilmeli"


# --- ToolSpec saglayici-bagimsiz declaration uretir -------------------------
def test_toolspec_saglayici_bagimsiz_declaration_uretir():
    from certaops.providers import FunctionDeclaration
    from robotics_agent.tools import load_all_tools
    from robotics_agent.tools.registry import REGISTRY

    load_all_tools()
    spec = REGISTRY["sap_stock_overview"]
    declaration = spec.to_function_declaration()
    assert isinstance(declaration, FunctionDeclaration)
    assert declaration.name == "sap_stock_overview"
    assert declaration.parameters["type"] == "object"
    # Saglayiciya ozgu anahtar YOK.
    assert "input_schema" not in declaration.to_dict()
    assert "cache_control" not in str(declaration.to_dict())


def test_core_runtime_saglayici_sdk_tipine_baglanmaz():
    """Core runtime modulu hicbir vendor SDK'sini import etmez."""
    import ast
    import pathlib

    source = pathlib.Path("src/certaops/runtime/agent.py").read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    for vendor in ("anthropic", "google", "google.genai", "openai"):
        assert not any(m == vendor or m.startswith(vendor + ".") for m in imported), vendor


def test_streaming_desteklemeyen_saglayici_normal_moda_duser(env):
    """`on_text` kabul etmeyen bir saglayici akisi bozmaz."""
    runtime, provider = build(env, [reply("duz yanit")])
    chunks: list[str] = []
    turn = runtime.chat("stok ve proje maliyeti", on_text=chunks.append)
    assert turn.text == "duz yanit"
    assert chunks == ["duz yanit"]
