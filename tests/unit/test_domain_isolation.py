"""Tek runtime mimarisinde domain izolasyonu.

Bu dosya eskiden ayri calisan agent'lari test ediyordu. Mimari degisti:
domain'ler artik calisma zamani nesnesi degil, **metadata** (prompt parcasi +
tool pack + iterasyon butcesi + erisim kapsami). Test edilen degismez ayni
kaldi: bir domain digerinin tool'unu goremez ve yetkisiz actor yazma
paketini acamaz.
"""

from __future__ import annotations

import pytest

from certaops.providers import FakeModelProvider
from certaops.providers.fake import reply
from certaops.runtime import SAPAgentRuntime
from certaops.runtime.profiles import (
    DOMAIN_PROFILES,
    iteration_budget_for,
    profile_catalogue,
    profiles_for_packs,
)
from robotics_agent.core import plan_agents
from robotics_agent.core.router import BOOTSTRAP_PACK, domains_for_packs
from robotics_agent.tools import load_all_tools, visible_tool_names


def _runtime(settings, actor, script=None):
    return SAPAgentRuntime(
        settings, provider=FakeModelProvider(script or [reply("ok")]), actor=actor
    )


def _tools_for(profile_key: str, actor) -> set[str]:
    load_all_tools()
    profile = DOMAIN_PROFILES[profile_key]
    return set(visible_tool_names(domains_for_packs(profile.packs), actor))


# --- Domain izolasyonu (artik pack metadata'si uzerinden) -------------------
def test_domainler_yalniz_kendi_tool_setini_gorur(purchaser):
    master = _tools_for("master_data", purchaser)
    procurement = _tools_for("procurement", purchaser)
    finance = _tools_for("finance", purchaser)

    assert "sap_material_360" in master
    assert "sap_pr_submit" not in master
    assert "sap_pr_submit" in procurement
    assert "sap_generate_report" in finance
    assert "sap_pr_submit" not in finance


def test_yetkisiz_actor_yazma_packini_acamaz(viewer):
    plan = plan_agents("satinalma talebi olustur", viewer)
    assert "procurement_write" not in plan.routing.packs


def test_yetkisiz_actor_icin_yazma_toolu_hic_gorunmez(viewer):
    assert "sap_pr_submit" not in _tools_for("procurement", viewer)


# --- Profil metadata'si ------------------------------------------------------
def test_her_domain_prompt_pack_ve_butce_tasir():
    for profile in DOMAIN_PROFILES.values():
        assert profile.mission.strip(), profile.key
        assert BOOTSTRAP_PACK in profile.packs
        assert profile.domain_packs, "her domain en az bir domain pack'i tanimlamali"
        assert profile.iteration_budget > 0


def test_cok_domainli_pack_kumesi_en_genis_butceyi_alir():
    """Tek dongu tum domainlerin isini yapar; en dar butceye sikismaz."""
    budget = iteration_budget_for(("bootstrap", "master_data", "procurement_write"))
    assert budget == max(
        DOMAIN_PROFILES["master_data"].iteration_budget,
        DOMAIN_PROFILES["procurement"].iteration_budget,
    )


def test_domain_bulunamazsa_platform_profiline_duser():
    assert profiles_for_packs((BOOTSTRAP_PACK,))[0].key == "platform"


# --- Health / katalog --------------------------------------------------------
def test_health_tek_runtime_mimarisini_bildirir(settings, purchaser):
    runtime = _runtime(settings, purchaser)
    health = runtime.health()
    assert health["architecture"] == "certaops-single-runtime"
    assert {row["domain"] for row in health["domains"]} == set(DOMAIN_PROFILES)
    # Saglayici ve model gorunur, API anahtari GORUNMEZ.
    assert set(health["model"]) >= {"provider", "model"}
    assert "api_key" not in str(health["model"]).lower()


def test_domain_katalogu_ayri_agent_iddiasi_tasimaz():
    catalogue = profile_catalogue()
    assert catalogue
    for row in catalogue:
        assert "domain" in row
        assert "agent" not in row
        assert "handoff_targets" not in row


# --- Public paket ------------------------------------------------------------
def test_public_paket_yeni_adlari_disari_verir():
    import certaops
    from certaops.runtime import SAPAgentRuntime as RuntimeClass

    assert certaops.SAPAgentRuntime is RuntimeClass
    assert certaops.build_provider is not None


def test_eski_facade_calisir_ve_deprecation_uyarir(settings, purchaser):
    from robotics_agent.compat_agent import SAPDomainAgent, SAPMultiAgent

    with pytest.warns(DeprecationWarning):
        legacy = SAPMultiAgent(
            settings, provider=FakeModelProvider([reply("ok")]), actor=purchaser
        )
    assert isinstance(legacy, SAPAgentRuntime)
    with pytest.warns(DeprecationWarning):
        single = SAPDomainAgent(
            settings, provider=FakeModelProvider([reply("ok")]), actor=purchaser
        )
    assert isinstance(single, SAPAgentRuntime)


def test_cok_domainli_istek_tek_model_cagrisi_yapar(settings, purchaser):
    """Mimari degisikligin olculebilir sonucu."""
    provider = FakeModelProvider([reply("birlesik ozet")])
    runtime = SAPAgentRuntime(settings, provider=provider, actor=purchaser)
    turn = runtime.chat(
        "HD-GEAR-CSF25-100 stok durumu ve R-2026-014 proje maliyeti raporla"
    )
    assert len(turn.active_domains) >= 2
    assert provider.call_count == 1
    assert turn.model_calls == 1
