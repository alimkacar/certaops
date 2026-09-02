"""Bulgu defteri: tanımlı ama devrede olmayan güvenlik kontrolleri.

Ortak sınıf: **kapı yazılmış, ama kimse ondan geçmiyor.** Bir ayar `true`
yapılabiliyor ama hiçbir yerde okunmuyor; bir gate fonksiyonu test edilmiş
ama `src/` içinde çağıranı yok; bir tip kontrolü doğru görünüyor ama akış
değişince ölü dala dönüşmüş.

Bu testler kapıların **gerçekten akışta** olduğunu sabitler.
"""

from __future__ import annotations

import base64

import pytest

from robotics_agent.security_at_rest import (
    ENVELOPE_PREFIX,
    AtRestConfigError,
    RecordCipher,
    decrypt_if_needed,
    load_key,
    maybe_cipher,
)


def _key() -> bytes:
    return b"0123456789abcdef0123456789abcdef"


# --- 1. Şifreleme ayarı "açık ama etkisiz" olamaz --------------------------
def test_sifreleme_acik_ama_anahtar_yoksa_baslamaz(monkeypatch):
    """İşlevsiz bir güvenlik ayarı, olmayan bir ayardan daha kötüdür.

    `AGENT_EVIDENCE_ENCRYPTION=true` yapan operatör kayıtların diskte şifreli
    durduğunu sanıyordu; ayar hiçbir yerde okunmuyordu. Artık fail-closed.
    """
    monkeypatch.delenv("AGENT_AT_REST_KEY", raising=False)
    with pytest.raises(AtRestConfigError, match="AGENT_AT_REST_KEY"):
        maybe_cipher(True, purpose="evidence")


def test_sifreleme_kapaliyken_sifreleyici_kurulmaz(monkeypatch):
    monkeypatch.delenv("AGENT_AT_REST_KEY", raising=False)
    assert maybe_cipher(False, purpose="evidence") is None


def test_config_validate_eksik_anahtari_bildirir(monkeypatch):
    """Sorun çalışma zamanında değil BAŞLANGIÇTA görünmeli."""
    from robotics_agent.config import PrivacySettings

    monkeypatch.delenv("AGENT_AT_REST_KEY", raising=False)
    monkeypatch.setenv("AGENT_EVIDENCE_ENCRYPTION", "true")
    problems = PrivacySettings().validate()
    assert any("AGENT_AT_REST_KEY" in p for p in problems)


def test_audit_sifrelemesi_de_anahtarsiz_baslamaz(monkeypatch):
    from robotics_agent.config import PrivacySettings

    monkeypatch.delenv("AGENT_AT_REST_KEY", raising=False)
    monkeypatch.setenv("AGENT_EVIDENCE_ENCRYPTION", "false")
    monkeypatch.setenv("AGENT_SESSION_ENCRYPTION", "false")
    monkeypatch.setenv("AGENT_AUDIT_ENCRYPTION", "true")

    problems = PrivacySettings().validate()

    assert any("AGENT_AT_REST_KEY" in p for p in problems)


def test_gidis_donus_sifreleme_calisir():
    cipher = RecordCipher(_key(), purpose="evidence")
    blob = cipher.encrypt('{"payload": {"vendor": "0010002"}}')
    assert blob.startswith(ENVELOPE_PREFIX)
    assert "0010002" not in blob, "duz metin zarfin icinde gorunuyor"
    assert cipher.decrypt(blob) == '{"payload": {"vendor": "0010002"}}'


def test_amac_baglama_depolar_arasi_tasimayi_engeller():
    """Kanıt deposundan alınan gövde oturum deposunda çözülememeli (AAD)."""
    evidence = RecordCipher(_key(), purpose="evidence")
    session = RecordCipher(_key(), purpose="session")
    blob = evidence.encrypt("gizli")
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        session.decrypt(blob)


def test_eski_duz_metin_kayitlar_okunmaya_devam_eder():
    """Şifreleme sonradan açıldığında depodaki eski kayıtlar erişilebilir kalır."""
    cipher = RecordCipher(_key(), purpose="evidence")
    assert cipher.decrypt('{"eski": "duz metin"}') == '{"eski": "duz metin"}'


def test_sifreli_kayit_anahtarsiz_sessizce_gecilmez():
    """Anahtar kaybı "veri yok" gibi görünmemeli."""
    blob = RecordCipher(_key(), purpose="evidence").encrypt("x")
    with pytest.raises(AtRestConfigError, match="sifreleme kapali"):
        decrypt_if_needed(None, blob)


def test_anahtar_hem_base64_hem_hex_kabul_eder(monkeypatch):
    monkeypatch.setenv("AGENT_AT_REST_KEY", base64.b64encode(_key()).decode())
    assert load_key() == _key()
    monkeypatch.setenv("AGENT_AT_REST_KEY", _key().hex())
    assert load_key() == _key()


def test_yanlis_uzunluktaki_anahtar_reddedilir(monkeypatch):
    monkeypatch.setenv("AGENT_AT_REST_KEY", base64.b64encode(b"kisa").decode())
    with pytest.raises(AtRestConfigError):
        load_key()


def test_evidence_deposu_diske_sifreli_yazar(tmp_path):
    """Uçtan uca: SQLite dosyasında düz metin iş verisi kalmamalı."""
    from robotics_agent.contracts import ActorContext, Evidence, SQLiteEvidenceStore
    from robotics_agent.core import get_state_db

    db = get_state_db(tmp_path / "ev.sqlite3")
    actor = ActorContext(subject="a@firma.test", tenant="100", auth_method="test")
    store = SQLiteEvidenceStore(db, cipher=RecordCipher(_key(), purpose="evidence"))

    handle = store.put(
        {"vendor_name": "GIZLI-TEDARIKCI-AS"},
        actor=actor,
        tool="sap_material_360",
        evidence=Evidence(source_system="S4", source_api="test"),
    )

    raw = (tmp_path / "ev.sqlite3").read_bytes()
    assert b"GIZLI-TEDARIKCI-AS" not in raw, "is verisi diske duz metin yazildi"
    # Sahibi yine okuyabilmeli: sifreleme erisimi degil DEPOLAMAYI degistirir.
    assert store.get(handle, actor=actor)["payload"] == {"vendor_name": "GIZLI-TEDARIKCI-AS"}


def test_audit_govdesi_ve_mirror_diske_sifreli_yazilir(tmp_path):
    """DLP yalniz gorunumde degil, kalici audit kaydindan ONCE uygulanir."""
    from robotics_agent.contracts import ActorContext, ExecutionContext
    from robotics_agent.core import AuditLedger, get_state_db

    db_path = tmp_path / "audit.sqlite3"
    mirror_path = tmp_path / "audit.jsonl"
    actor = ActorContext(
        subject="GIZLI-AUDIT-KULLANICISI@firma.test",
        tenant="100",
        auth_method="test",
    )
    execution = ExecutionContext(actor=actor, system_alias="S4-TEST")
    ledger = AuditLedger(
        get_state_db(db_path),
        mirror_path=mirror_path,
        cipher=RecordCipher(_key(), purpose="audit"),
    )

    ledger.append(
        "tool.completed",
        execution=execution,
        tool="sap_supplier_invoice_status",
        detail={"vendor_name": "GIZLI-AUDIT-TEDARIKCISI-AS"},
    )

    row = get_state_db(db_path).query_one("SELECT body_json FROM audit_entries LIMIT 1")
    assert row is not None and row["body_json"].startswith(ENVELOPE_PREFIX)
    persisted = b"".join(
        path.read_bytes() for path in tmp_path.glob("audit.sqlite3*") if path.is_file()
    ) + mirror_path.read_bytes()
    assert b"GIZLI-AUDIT-KULLANICISI" not in persisted
    assert b"GIZLI-AUDIT-TEDARIKCISI" not in persisted
    assert ledger.recent()[0]["detail"]["vendor_name"] == "GIZLI-AUDIT-TEDARIKCISI-AS"
    assert ledger.verify()["valid"] is True


# --- 2. Geri yüklenen konuşma geçmişi gerçekten temizleniyor mu? -----------
def test_disaridan_atanan_gecmis_guvenilmez_isaretlenir():
    """Güven KÖKEN meselesidir, tip meselesi değil.

    Önceki kontrol `isinstance(message, ModelMessage)` idi. Oturum kaydı
    `messages_from_dicts()` ile ModelMessage olarak geri yüklenmeye
    başlayınca temizleme dalı tamamen ölü koda döndü.
    """
    from certaops.providers import ModelMessage
    from certaops.runtime import SAPAgentRuntime

    runtime = SAPAgentRuntime.__new__(SAPAgentRuntime)
    runtime._messages = []
    runtime._untrusted_before = 0

    SAPAgentRuntime.messages.fset(
        runtime, [ModelMessage(role="user", text="Authorization: Bearer sk-gizli-token")]
    )
    assert runtime._untrusted_before == 1, "yuklenen gecmis guvenilir sayildi"

    runtime._messages.append(ModelMessage(role="assistant", text="cevap"))
    assert runtime._untrusted_before == 1, "kendi urettigimiz mesaj esigi kaydirmamali"


def test_scrub_model_message_icini_temizler():
    """`_scrub` ModelMessage'ı tanımazsa garanti kâğıt üstünde kalır."""
    from certaops.providers import ModelMessage
    from certaops.runtime.agent import _scrub
    from robotics_agent.config import get_settings
    from robotics_agent.contracts import ActorContext
    from robotics_agent.privacy import build_dlp_engine

    settings = get_settings()
    actor = ActorContext(subject="a@firma.test", tenant="100", auth_method="test")
    message = ModelMessage(role="user", text="token: Bearer sk-cok-gizli-bir-deger-123456")

    cleaned = _scrub(
        message, actor=actor, settings=settings, dlp=build_dlp_engine(settings)
    )
    assert isinstance(cleaned, ModelMessage)
    assert "sk-cok-gizli-bir-deger-123456" not in cleaned.text


# --- 3. `detail=full` hacim kapısı ----------------------------------------
def test_full_detay_gecerli_amac_ister(settings, purchaser):
    """Alan maskesi DLP'de kapılıydı ama HACİM kapısızdı: `page_limit`
    `detail=full` için sayfa boyunu beş katına çıkarıyordu ve `resolve_detail`
    actor'u hiç görmez."""
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import ToolContext
    from robotics_agent.tools.registry import _gate_detail_level

    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)
    args, reason = _gate_detail_level({"detail": "full", "po_id": "1"}, ctx)
    assert args["detail"] == "standard"
    assert "purpose_code" in reason


def test_gecerli_amacla_full_detay_gecer(settings, purchaser):
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import ToolContext
    from robotics_agent.tools.registry import _gate_detail_level

    ctx = ToolContext(
        settings=settings,
        sap=build_backend(settings),
        actor=purchaser,
        purpose="procurement_operations",
    )
    args, reason = _gate_detail_level({"detail": "full"}, ctx)
    assert args["detail"] == "full"
    assert reason == ""


def test_standard_istek_kapiya_takilmaz(settings, purchaser):
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import ToolContext
    from robotics_agent.tools.registry import _gate_detail_level

    ctx = ToolContext(settings=settings, sap=build_backend(settings), actor=purchaser)
    args, reason = _gate_detail_level({"detail": "standard"}, ctx)
    assert args["detail"] == "standard"
    assert reason == ""


# --- 4. Bekleyen ayar değişikliğini herkes iptal edememeli ----------------
def test_yetkisiz_kullanici_bekleyen_ayari_iptal_edemez(tmp_path, monkeypatch):
    """Bu bir veri sızıntısı değil, KONTROL REDDİdir: yöneticinin uygulamaya
    çalıştığı bir sertleştirme ayarı tekrar tekrar iptal edilebilirdi."""
    from robotics_agent.contracts import ActorContext
    from robotics_agent.runtime_config import ConfigRefused
    from robotics_agent.runtime_config.service import ConfigService

    monkeypatch.setenv("AGENT_STATE_DIR", str(tmp_path))
    okur = ActorContext(subject="okur@firma.test", tenant="100", roles=("VIEWER",))

    service = ConfigService.__new__(ConfigService)
    service._pending = lambda: {  # noqa: SLF001 - kapiyi izole sinamak icin
        "AGENT_DLP_MODE": {
            "change_id": "chg-1",
            "key": "AGENT_DLP_MODE",
            "requested_by": "yonetici@firma.test",
            "value": "enforce",
        }
    }
    with pytest.raises(ConfigRefused) as hata:
        service.cancel("chg-1", actor=okur)
    assert hata.value.code == "MISSING_SCOPE"
