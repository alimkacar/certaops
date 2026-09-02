"""Arayuzden ayar degistirme yuzeyinin guvenlik sozlesmesi.

Bu dosyanin sabitledigi sey su: bir tarayici ucu, deployment'i guvensiz hale
GETIREMEZ. Uc bagimsiz savunma var ve her biri tek basina yeterli olmali:

  1. izin listesi   -- kimlik, SAP kimlik bilgisi, egress ve maskeleme
                       anahtarlari bu ucdan hic gorunmez
  2. mandal         -- yeni bir uretim engeli doguran degisiklik reddedilir
  3. iki kisi       -- sonuc doguran ayar ikinci bir kimligin onayini ister

Bir savunmayi kaldiran bir degisiklik bu dosyayi kirmali.
"""
from __future__ import annotations

import json
import os

import pytest

from robotics_agent.config import get_settings
from robotics_agent.contracts import ActorContext
from robotics_agent.contracts.actor import ROLE_SCOPES, SCOPE_PLATFORM_CONFIG
from robotics_agent.runtime_config import (
    SETTABLE,
    ConfigRefused,
    ConfigService,
    SettingError,
    overrides_path,
    read_overrides,
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SAP_BACKEND", "mock")
    monkeypatch.setenv("MODEL_PROVIDER", "fake")
    monkeypatch.delenv("APP_ENV", raising=False)
    # `apply_overrides()` degerleri `os.environ`e yaziyor ve orada kaliyor.
    # Uretimde surec temiz baslar; testte onceki bir vakanin birakti[gi]
    # deger bir sonrakini "zaten bu deger" diye sessizce gecirir.
    for anahtar in SETTABLE:
        monkeypatch.delenv(anahtar, raising=False)
    monkeypatch.setenv("SAP_BACKEND", "mock")
    settings = get_settings(reload=True)
    settings.ensure_dirs()
    yield settings
    get_settings(reload=True)


@pytest.fixture
def admin():
    return ActorContext(subject="admin@firma.test", tenant="100", roles=("PLATFORM_ADMIN",))


@pytest.fixture
def admin2():
    return ActorContext(subject="ikinci@firma.test", tenant="100", roles=("PLATFORM_ADMIN",))


@pytest.fixture
def purchaser():
    return ActorContext(subject="alici@firma.test", tenant="100", roles=("PURCHASER",))


# --- 0. Gorevler ayrimi -----------------------------------------------------
def test_ayar_kapsami_sap_yazmayla_ayni_rolde_bulusmaz():
    """Kapiyi gevsetebilen kisi, o kapinin ardina yazamamali.

    Bu bir test degil, bir sozlesme: ROLE_SCOPES'a `platform.config` ve bir
    yazma kapsamini ayni role koyan bir degisiklik burada durur.
    """
    yazma = {"sap.pr.write", "sap.po.write", "sap.pr.approve", "sap.po.approve"}
    ihlal = [
        rol for rol, kapsamlar in ROLE_SCOPES.items()
        if SCOPE_PLATFORM_CONFIG in kapsamlar and (kapsamlar & yazma)
    ]
    assert not ihlal, f"SoD ihlali: {ihlal} hem ayar degistirebiliyor hem SAP'a yaziyor"


# --- 1. Izin listesi --------------------------------------------------------
@pytest.mark.parametrize(
    "anahtar",
    [
        "AGENT_AUTH_MODE",          # kimlik dogrulamayi kapatabilirdi
        "AGENT_PRINCIPALS_FILE",    # baska bir principal dosyasi gosterebilirdi
        "AGENT_OIDC_ISSUER",
        "SAP_PASSWORD",             # sir yazan uc, sir sizdiran uctur
        "SAP_OAUTH_CLIENT_SECRET",
        "SAP_API_KEY",
        "SAP_ALLOWED_HOSTS",        # egress allowlist deployment karari
        "SAP_VERIFY_SSL",           # kapatilabilirse TLS anlamsiz
        "LOG_MASK",                 # maskeleme oldurucu anahtari
        "AGENT_MASK_PREVIEWS",
        "ANTHROPIC_API_KEY",
        "GEMINI_STORE_INTERACTIONS",
        "AGENT_KMS_KEY_ID",
    ],
)
def test_hassas_anahtarlar_izin_listesinde_yok(anahtar, env, admin):
    assert anahtar not in SETTABLE
    with pytest.raises(SettingError):
        ConfigService(env).propose(anahtar, "x", actor=admin)


def test_izin_listesi_hicbir_sir_tasimaz():
    """Listeye sir kokulu bir anahtar eklenirse burada yakalanir."""
    supheli = ("PASSWORD", "SECRET", "TOKEN", "API_KEY", "CLIENT_ID",
               "PRIVATE", "CREDENTIAL", "ALLOWED_HOSTS", "VERIFY_SSL",
               "AUTH_MODE", "MASK", "KMS", "PRINCIPALS")
    kirli = [k for k in SETTABLE if any(p in k.upper() for p in supheli)]
    assert not kirli, f"izin listesinde hassas anahtar: {kirli}"


def test_ayar_okuma_ucu_sir_dondurmez(env, admin):
    """Tabloda hicbir sir DEGERI bulunmamali.

    `production_blockers` metinleri "SECRET" kelimesini tavsiye olarak
    gecirebilir (ornegin "AGENT_PSEUDONYMIZATION_SECRET tanimli degil");
    bu bir sir degil, eksik yapilandirma uyarisidir. Bu yuzden tarama
    tavsiye metnine degil, ayar alanlarinin kendisine bakar.
    """
    tablo = ConfigService(env).describe(admin)
    govde = json.dumps(tablo["settings"]).lower()
    for iz in ("password", "secret", "api_key", "client_id", "token", "credential"):
        assert iz not in govde, f"ayar tablosunda '{iz}' gecti"


# --- 2. Deger duzeyi sinirlari ---------------------------------------------
def test_dlp_arayuzden_kapatilamaz(env, admin):
    assert "off" not in SETTABLE["AGENT_DLP_MODE"].choices
    with pytest.raises(SettingError, match="gecerli degerler"):
        ConfigService(env).propose("AGENT_DLP_MODE", "off", actor=admin)


def test_log_seviyesi_debug_secilemez(env, admin):
    """DEBUG, maskeleme kapsaminin dar oldugu bir hatta sir gorunurlugunu artirir."""
    assert "DEBUG" not in SETTABLE["LOG_LEVEL"].choices
    with pytest.raises(SettingError):
        ConfigService(env).propose("LOG_LEVEL", "DEBUG", actor=admin)


@pytest.mark.parametrize("deger", [99999, -1, "abc", "", None])
def test_sinir_disi_sayi_reddedilir(deger, env, admin):
    with pytest.raises(SettingError):
        ConfigService(env).propose("SAP_PAGE_SIZE", deger, actor=admin)


# --- 3. Kapsam --------------------------------------------------------------
def test_kapsamsiz_actor_degistiremez(env, purchaser):
    with pytest.raises(ConfigRefused) as exc:
        ConfigService(env).propose("LOG_LEVEL", "ERROR", actor=purchaser)
    assert exc.value.code == "MISSING_SCOPE"


def test_kimliksiz_istek_degistiremez(env):
    with pytest.raises(ConfigRefused) as exc:
        ConfigService(env).propose("LOG_LEVEL", "ERROR", actor=None)
    assert exc.value.code == "MISSING_SCOPE"


def test_kapsamsiz_actor_okuyabilir_ama_kilitli_gorur(env, purchaser):
    tablo = ConfigService(env).describe(purchaser)
    assert tablo["can_change"] is False
    assert all(alan["locked"] for alan in tablo["settings"])
    assert all(alan["locked_reason"] == "no_scope" for alan in tablo["settings"])


# --- 4. Mandal --------------------------------------------------------------
def test_mandal_yeni_uretim_engeli_doguran_degisikligi_reddeder(env, admin, monkeypatch):
    """`dry_run=false` + `approval_gateway=local` projenin kendi engeli.

    Mandal bu kombinasyonu, `APP_ENV` uretim olmasa bile reddeder: arayuz
    yapilandirmayi yalniz guvenli yonde degistirebilir.
    """
    monkeypatch.setenv("AGENT_APPROVAL_GATEWAY", "local")
    ayarlar = get_settings(reload=True)
    with pytest.raises(ConfigRefused) as exc:
        ConfigService(ayarlar).propose("SAP_DRY_RUN", False, actor=admin)
    assert exc.value.code == "WOULD_BREAK_PRODUCTION_GATE"
    assert exc.value.detail, "hangi engelin dogacagi soylenmeli"


def test_mandal_risk_skorunu_dusurmeyi_de_reddeder(env, admin):
    """Ikinci bagimsiz mandal vakasi.

    `AGENT_RISK_SCORING_MODE=report` runtime risk skorunu hesaplayip
    uygulamayan bir kip. Projenin kendi uretim engeli bunu yasakliyor ve
    mandal, izin listesinden bagimsiz olarak yakaliyor -- ayar listede,
    kapsam dogru, ama deger yapilandirmayi zayiflatiyor.
    """
    with pytest.raises(ConfigRefused) as exc:
        ConfigService(env).propose("AGENT_RISK_SCORING_MODE", "report", actor=admin)
    assert exc.value.code == "WOULD_BREAK_PRODUCTION_GATE"


def test_mandal_tek_yonlu_var_olan_engeli_kaldirmak_serbest(env, admin, monkeypatch):
    """Guvenli yone dogru degisiklik engellenmemeli."""
    monkeypatch.setenv("AGENT_DLP_MODE", "report")
    ayarlar = get_settings(reload=True)
    sonuc = ConfigService(ayarlar).propose("AGENT_DLP_MODE", "enforce", actor=admin)
    assert sonuc.status in {"pending_approval", "staged", "applied"}


def test_mandal_zararsiz_degisikligi_engellemez(env, admin):
    sonuc = ConfigService(env).propose("SAP_PAGE_SIZE", 150, actor=admin)
    assert sonuc.status == "staged"
    assert sonuc.restart_required is True


# --- 5. Iki kisi kurali -----------------------------------------------------
def test_sonuc_doguran_ayar_dogrudan_uygulanmaz(env, admin):
    sonuc = ConfigService(env).propose("AGENT_APPROVAL_GATEWAY", "bpa", actor=admin)
    assert sonuc.status == "pending_approval"
    assert sonuc.change_id
    # Bekleyen degisiklik heNUZ uygulanmadi.
    assert "AGENT_APPROVAL_GATEWAY" not in read_overrides()


def test_oneren_kendi_degisikligini_onaylayamaz(env, admin):
    svc = ConfigService(env)
    onerilen = svc.propose("AGENT_APPROVAL_GATEWAY", "bpa", actor=admin)
    with pytest.raises(ConfigRefused) as exc:
        svc.approve(onerilen.change_id, actor=admin)
    assert exc.value.code == "SOD_VIOLATION"


def test_kapsamsiz_kisi_onaylayamaz(env, admin, purchaser):
    svc = ConfigService(env)
    onerilen = svc.propose("AGENT_APPROVAL_GATEWAY", "bpa", actor=admin)
    with pytest.raises(ConfigRefused) as exc:
        svc.approve(onerilen.change_id, actor=purchaser)
    assert exc.value.code == "MISSING_SCOPE"


def test_ikinci_kimlik_onaylayinca_uygulanir(env, admin, admin2):
    svc = ConfigService(env)
    onerilen = svc.propose("AGENT_APPROVAL_GATEWAY", "bpa", actor=admin)
    sonuc = svc.approve(onerilen.change_id, actor=admin2)
    assert sonuc.status in {"staged", "applied"}
    depo = read_overrides()
    assert depo["AGENT_APPROVAL_GATEWAY"]["value"] == "bpa"
    # Iki kimlik de kayitta durmali: kim onerdi, kim onayladi.
    assert depo["AGENT_APPROVAL_GATEWAY"]["changed_by"] == admin.subject
    assert depo["AGENT_APPROVAL_GATEWAY"]["approved_by"] == admin2.subject


def test_bilinmeyen_degisiklik_onaylanamaz(env, admin2):
    with pytest.raises(ConfigRefused) as exc:
        ConfigService(env).approve("cfg_yok", actor=admin2)
    assert exc.value.code == "UNKNOWN_CHANGE"


# --- 6. Ortam onceligi ------------------------------------------------------
def test_dis_ortam_degiskeni_arayuzu_yener(env, admin, monkeypatch):
    """Kabuktan sabitlenmis bir ayar arayuzden degistirilemez.

    Operatorun son sozu her zaman kabuktadir: `SAP_DRY_RUN=true` ile
    baslatilmis bir surec, tarayicidan kuru calismadan cikarilamaz.
    """
    from robotics_agent.runtime_config import store

    monkeypatch.setattr(store, "_SHELL_ENV", frozenset({"SAP_PAGE_SIZE"}))
    with pytest.raises(ConfigRefused) as exc:
        ConfigService(env).propose("SAP_PAGE_SIZE", 200, actor=admin)
    assert exc.value.code == "ENV_PINNED"


def test_sabitlenmis_ayar_tabloda_isaretlenir(env, admin, monkeypatch):
    from robotics_agent.runtime_config import store

    monkeypatch.setattr(store, "_SHELL_ENV", frozenset({"SAP_PAGE_SIZE"}))
    tablo = ConfigService(env).describe(admin)
    alan = next(a for a in tablo["settings"] if a["key"] == "SAP_PAGE_SIZE")
    assert alan["locked"] is True
    assert alan["locked_reason"] == "env_pinned"


# --- 7. Depo --------------------------------------------------------------
def test_dosya_izni_dar(env, admin):
    ConfigService(env).propose("SAP_PAGE_SIZE", 150, actor=admin)
    assert overrides_path().stat().st_mode & 0o777 == 0o600


def test_izin_listesi_disindaki_anahtar_dosyadan_okunmaz(env, admin, tmp_path):
    """Dosya elle duzenlenmis olabilir; okuma da izin listesine tabidir."""
    ConfigService(env).propose("SAP_PAGE_SIZE", 150, actor=admin)
    yol = overrides_path()
    govde = json.loads(yol.read_text())
    govde["settings"]["AGENT_AUTH_MODE"] = {"value": "none"}
    govde["settings"]["SAP_PASSWORD"] = {"value": "sizinti"}
    yol.write_text(json.dumps(govde))

    okunan = read_overrides()
    assert "AGENT_AUTH_MODE" not in okunan
    assert "SAP_PASSWORD" not in okunan
    assert "SAP_PAGE_SIZE" in okunan


def test_elle_eklenen_anahtar_ortama_uygulanmaz(env, admin, monkeypatch):
    from robotics_agent.runtime_config.store import apply_overrides

    ConfigService(env).propose("SAP_PAGE_SIZE", 150, actor=admin)
    yol = overrides_path()
    govde = json.loads(yol.read_text())
    govde["settings"]["AGENT_AUTH_MODE"] = {"value": "none"}
    yol.write_text(json.dumps(govde))

    monkeypatch.delenv("AGENT_AUTH_MODE", raising=False)
    uygulanan = apply_overrides()
    assert "AGENT_AUTH_MODE" not in uygulanan
    assert os.environ.get("AGENT_AUTH_MODE") is None


def test_bozuk_dosya_servisi_durdurmaz(env, admin):
    overrides_path().parent.mkdir(parents=True, exist_ok=True)
    overrides_path().write_text("{ bu gecerli json degil")
    assert read_overrides() == {}
    # Servis yine calisir.
    assert ConfigService(env).describe(admin)["settings"]


# --- 8. Denetim -------------------------------------------------------------
def test_her_degisiklik_denetime_yazilir(env, admin):
    kayitlar = []

    class SahteDefter:
        def append(self, event, **kw):
            kayitlar.append((event, kw))

    svc = ConfigService(env, audit=SahteDefter())
    svc.propose("SAP_PAGE_SIZE", 150, actor=admin)
    olaylar = [e for e, _ in kayitlar]
    assert "platform.config.changed" in olaylar
    _, kw = next(k for k in kayitlar if k[0] == "platform.config.changed")
    assert kw["before"] == {"SAP_PAGE_SIZE": 100}
    assert kw["after"] == {"SAP_PAGE_SIZE": "150"}


def test_reddedilen_degisiklik_de_denetime_yazilir(env, purchaser):
    """Bir kapinin kac kez zorlandigi, kapinin calistigi kadar onemli."""
    kayitlar = []

    class SahteDefter:
        def append(self, event, **kw):
            kayitlar.append(event)

    svc = ConfigService(env, audit=SahteDefter())
    with pytest.raises(ConfigRefused):
        svc.propose("LOG_LEVEL", "ERROR", actor=purchaser)
    assert "platform.config.refused" in kayitlar
