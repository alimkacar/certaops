"""baslat.command'in mod/rol secim sozlesmesi."""

from __future__ import annotations

import builtins
import sys

import pytest

import baslat


@pytest.fixture(autouse=True)
def _temiz_baslat_ortami(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["baslat.py"])
    monkeypatch.delenv("SAP", raising=False)
    monkeypatch.delenv("CERTAOPS_ROLE", raising=False)


def test_etkilesimli_menu_canli_sap_ve_satinalmaci_secer(monkeypatch):
    cevaplar = iter(["2", "2"])
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(cevaplar))

    assert baslat.baslangic_secimleri(etkilesimli=True) == (True, "SATINALMACI")


def test_etkilesimli_menu_enter_ile_guvenli_varsayilanlari_secer(monkeypatch):
    cevaplar = iter(["", ""])
    monkeypatch.setattr(builtins, "input", lambda _prompt="": next(cevaplar))

    assert baslat.baslangic_secimleri(etkilesimli=True) == (False, "DENETCI")


def test_argumanlar_menuyu_atlayabilir(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["baslat.py", "--sap", "--rol", "onaylayici"]
    )

    assert baslat.baslangic_secimleri(etkilesimli=False) == (True, "ONAYLAYICI")


def test_rol_esittir_bicimi_desteklenir(monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["baslat.py", "--sim", "--rol=satinalmaci"]
    )

    assert baslat.baslangic_secimleri(etkilesimli=False) == (False, "SATINALMACI")


def test_celisken_mod_bayraklari_reddedilir(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["baslat.py", "--sap", "--sim"])

    with pytest.raises(ValueError, match="Ayni anda"):
        baslat.baslangic_secimleri(etkilesimli=False)


def test_help_servisi_baslatmadan_cikar(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["baslat.py", "--help"])

    assert baslat.main() == 0
    assert "Kullanım:" in capsys.readouterr().out


def test_simulasyon_secimi_miras_kalan_canli_backend_degerini_ezer(monkeypatch):
    monkeypatch.setenv("SAP_BACKEND", "odata")
    monkeypatch.setenv("SAP_ALLOWED_HOSTS", "gercek-sap.example")
    monkeypatch.setenv("SAP_READ_ONLY", "false")
    monkeypatch.setenv("SAP_DRY_RUN", "false")

    env, _sohbet = baslat.ayarlari_hazirla(canli=False)

    assert env["SAP_BACKEND"] == "mock"
    assert env["SAP_ALLOWED_HOSTS"] == "localhost"
    assert env["SAP_READ_ONLY"] == "true"
    assert env["SAP_DRY_RUN"] == "true"


def test_secilen_rol_stdio_mcp_actoruna_da_aktarilir(monkeypatch):
    env, _sohbet = baslat.ayarlari_hazirla(canli=False, rol_anahtari="SATINALMACI")

    assert env["AGENT_LOCAL_SUBJECT"] == "satinalmaci@local"
    assert env["AGENT_LOCAL_ROLES"] == "PURCHASER"


def test_envdeki_acik_anthropic_secimi_gemini_anahtarina_yenilmez(monkeypatch):
    """Iki anahtar varken MODEL_PROVIDER secimi korunmalidir."""
    dosya = {
        "MODEL_PROVIDER": "anthropic",
        "GEMINI_API_KEY": "gemini-var",
        "ANTHROPIC_API_KEY": "anthropic-var",
    }
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        baslat, "env_dosyasindan_oku", lambda ad: dosya.get(ad, "")
    )
    monkeypatch.setattr(baslat, "anahtar_var", lambda ad: bool(dosya.get(ad)))

    env, sohbet = baslat.ayarlari_hazirla(canli=False)

    assert sohbet is True
    assert env["MODEL_PROVIDER"] == "anthropic"


def test_secilen_saglayicinin_kendi_anahtari_zorunludur(monkeypatch):
    """Gemini anahtari Anthropic seciminin eksik anahtarini gizleyemez."""
    dosya = {
        "MODEL_PROVIDER": "anthropic",
        "GEMINI_API_KEY": "gemini-var",
    }
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        baslat, "env_dosyasindan_oku", lambda ad: dosya.get(ad, "")
    )
    monkeypatch.setattr(baslat, "anahtar_var", lambda ad: bool(dosya.get(ad)))

    env, sohbet = baslat.ayarlari_hazirla(canli=False)

    assert sohbet is False
    assert env["MODEL_PROVIDER"] == "anthropic"


def test_ozet_yalniz_secilen_rolun_tokenini_gosterir(capsys):
    tokenlar = {
        "CERTAOPS_TOKEN_DENETCI": "tok-denetci",
        "CERTAOPS_TOKEN_SATINALMACI": "tok-satinalmaci",
        "CERTAOPS_TOKEN_ONAYLAYICI": "tok-onaylayici",
    }

    baslat.ozet_yaz(
        "http://127.0.0.1:8000/ui",
        tokenlar,
        sohbet=True,
        pano=False,
        rol_anahtari="SATINALMACI",
    )

    cikti = capsys.readouterr().out
    assert "tok-satinalmaci" in cikti
    assert "tok-denetci" not in cikti
    assert "tok-onaylayici" not in cikti
