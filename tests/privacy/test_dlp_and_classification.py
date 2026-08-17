"""Veri siniflandirma, alan yetkisi ve DLP guvenlik testleri.

Kontrol edilen davranislar:
  - alan-duyarli siniflandirma ve fail-closed bilinmeyen alan
  - alan bazli yetki: ayni tool farkli role farkli projeksiyon doner
  - D3'un modele, loga ve handoff'a hicbir kosulda ham gitmemesi
  - tenant'a ozgu HMAC takma kimlik ve capraz-tenant korelasyon kirilmasi
  - serbest metinde PII, Unicode obfuscation ve yanlis pozitif dengesi
"""

from __future__ import annotations

import pytest

from robotics_agent.contracts import (
    SCOPE_DATA_CONFIDENTIAL,
    SCOPE_DATA_RESTRICTED,
    SCOPE_EXPORT_CONFIDENTIAL,
    ActorContext,
)
from robotics_agent.privacy import (
    DataClass,
    DataPolicy,
    DLPEngine,
    FieldAccessPolicy,
    PrivacyAction,
    Pseudonymizer,
    classify_field,
    handoff_allowlist,
)


@pytest.fixture
def pseudonymizer() -> Pseudonymizer:
    return Pseudonymizer(secret=b"test-secret-key-for-privacy", key_id="test-v1")


@pytest.fixture
def engine(pseudonymizer) -> DLPEngine:
    return DLPEngine(field_policy=FieldAccessPolicy(), pseudonymizer=pseudonymizer)


@pytest.fixture
def strict_engine(pseudonymizer) -> DLPEngine:
    """Uretim profili: siniflandirilmamis alan D3 kabul edilir."""
    return DLPEngine(
        field_policy=FieldAccessPolicy(strict_unknown=True), pseudonymizer=pseudonymizer
    )


def _actor(*roles: str, tenant: str = "100", extra: frozenset[str] = frozenset()) -> ActorContext:
    return ActorContext(
        subject="kullanici@firma.test",
        tenant=tenant,
        roles=roles,
        explicit_scopes=extra,
        auth_method="test",
    )


# --- Siniflandirma ---------------------------------------------------------
@pytest.mark.parametrize(
    ("field_name", "expected"),
    [
        ("currency", DataClass.D0),
        ("material_id", DataClass.D1),
        ("plant", DataClass.D1),
        ("po_id", DataClass.D1),
        ("net_price", DataClass.D2),
        ("supplier_email", DataClass.D2),
        ("vendor_id", DataClass.D2),
        ("iban", DataClass.D3),
        ("supplier_bank_account", DataClass.D3),
        ("tax_number", DataClass.D3),
    ],
)
def test_field_inventory_classifies_sap_fields(field_name, expected):
    assert classify_field(field_name) is expected


def test_tool_policy_cannot_downgrade_central_classification():
    """Bir tool IBAN'i D1 ilan ederek merkezi DLP siniflandirmasini atlatamaz."""
    policy = DataPolicy(fields={"iban": DataClass.D1})
    problems = policy.validate()
    assert problems and "iban" in problems[0]


def test_unknown_field_is_d3_in_strict_mode():
    policy = DataPolicy(default_class=DataClass.D1)
    assert policy.classify("bizim_ozel_alan", strict=False) is DataClass.D1
    assert policy.classify("bizim_ozel_alan", strict=True) is DataClass.D3


def test_strict_mode_tokenizes_unclassified_field_for_model(strict_engine):
    """Fail-closed: bilinmeyen alan uretimde modele ham gitmez."""
    result = strict_engine.apply(
        {"ozel_hesaplama_sonucu": "gizli-deger"},
        actor=_actor("PURCHASER"),
        sink="model",
        policy=DataPolicy(),
    )
    assert result.payload["ozel_hesaplama_sonucu"].startswith("px_")


# --- Alan bazli yetki ------------------------------------------------------
def test_same_payload_yields_different_projection_per_role(engine):
    """Ayni tool sonucu, alan yetkisine gore farkli role farkli projeksiyon verir."""
    payload = {"po_id": "4500018821", "net_price": 1265.0, "quantity": 24}
    policy = DataPolicy()

    purchaser = engine.apply(payload, actor=_actor("PURCHASER"), sink="model", policy=policy)
    viewer = engine.apply(payload, actor=_actor("VIEWER"), sink="model", policy=policy)

    # Satinalmaci ticari veriyi gorur; salt gozlemci gormez.
    assert purchaser.payload["net_price"] == 1265.0
    assert viewer.payload["net_price"] == "***"
    # Is verisi her iki rolde de korunur; maskeleme karari kullanilamaz kilmaz.
    assert purchaser.payload["quantity"] == viewer.payload["quantity"] == 24
    assert purchaser.payload["po_id"] == viewer.payload["po_id"] == "4500018821"


def test_personal_data_is_not_sent_to_the_model_by_default(engine):
    """Model fiyati gorur, tedarikcinin e-postasini gormez (veri minimizasyonu)."""
    result = engine.apply(
        {"net_price": 1265.0, "supplier_email": "satis@tedarikci.test"},
        actor=_actor("PURCHASER"),
        sink="model",
        policy=DataPolicy(),
    )
    assert result.payload["net_price"] == 1265.0
    assert result.payload["supplier_email"] == "***"


def test_full_detail_requires_scope_and_purpose_code():
    policy = FieldAccessPolicy()
    purchaser = _actor("PURCHASER")
    assert policy.full_detail_blocker(purchaser, purpose="") is not None
    assert policy.effective_detail("full", purchaser, purpose="") == "standard"
    assert policy.effective_detail("full", purchaser, purpose="procurement_operations") == "full"
    # Uydurulmus bir amac kodu yetki uretmez.
    assert policy.effective_detail("full", purchaser, purpose="cunku_gerekiyor") == "standard"


# --- D3 kacis yollari ------------------------------------------------------
@pytest.mark.parametrize("sink", ["model", "log", "handoff", "client"])
def test_restricted_data_never_leaves_raw(engine, sink):
    """D3 veri model, log, handoff veya istemci hedeflerine ham gitmez."""
    iban = "DE89370400440532013000"
    result = engine.apply(
        {"supplier_iban": iban},
        actor=_actor("PURCHASER"),
        sink=sink,
        policy=DataPolicy(fields={"supplier_iban": DataClass.D3}),
    )
    assert iban not in str(result.payload)


def test_restricted_export_is_denied_without_both_scopes(engine):
    policy = DataPolicy(fields={"supplier_iban": DataClass.D3})
    denied = engine.apply(
        {"supplier_iban": "DE89370400440532013000"},
        actor=_actor("BUYER_LEAD"),
        sink="export",
        policy=policy,
    )
    assert denied.denied

    allowed = engine.apply(
        {"supplier_iban": "DE89370400440532013000"},
        actor=_actor(
            "BUYER_LEAD",
            extra=frozenset({SCOPE_DATA_RESTRICTED, SCOPE_EXPORT_CONFIDENTIAL}),
        ),
        sink="export",
        policy=policy,
    )
    assert not allowed.denied


def test_confidential_export_denied_without_export_scope(engine):
    result = engine.apply(
        {"net_price": 1265.0},
        actor=_actor("PURCHASER"),  # sap.export.confidential tasimaz
        sink="export",
        policy=DataPolicy(),
    )
    assert result.denied


def test_secret_key_names_are_masked_regardless_of_value(engine):
    result = engine.apply(
        {"sap_password": "hunter2", "client_secret": "abc", "authorization": "Bearer x"},
        actor=_actor("PURCHASER"),
        sink="model",
        policy=DataPolicy(),
    )
    assert set(result.payload.values()) == {"***"}


def test_secret_matching_is_token_based_not_substring(engine):
    """`likely_missing_authorizations` bir sir degildir; maskelenmemeli."""
    result = engine.apply(
        {"likely_missing_authorizations": ["M_BANF_BSA", "M_EINK_FRG"]},
        actor=_actor("PURCHASER"),
        sink="model",
        policy=DataPolicy(),
    )
    assert result.payload["likely_missing_authorizations"] == ["M_BANF_BSA", "M_EINK_FRG"]


# --- Takma kimliklestirme --------------------------------------------------
def test_pseudonym_is_deterministic_within_a_tenant(pseudonymizer):
    first = pseudonymizer.token("DE89370400440532013000", tenant="100")
    second = pseudonymizer.token("DE89370400440532013000", tenant="100")
    assert first == second and first.startswith("px_")


def test_pseudonym_does_not_correlate_across_tenants(pseudonymizer):
    """Ayni IBAN iki tenant'ta farkli token uretir (capraz-tenant korelasyon yok)."""
    assert pseudonymizer.token("DE89370400440532013000", tenant="100") != pseudonymizer.token(
        "DE89370400440532013000", tenant="200"
    )


def test_pseudonymizer_has_no_reverse_lookup(pseudonymizer):
    """Takma kimligi geri cozecek bir arayuz model katmaninda bulunmaz."""
    assert not hasattr(pseudonymizer, "resolve")
    assert not hasattr(pseudonymizer, "decode")


def test_empty_values_are_not_tokenized(pseudonymizer):
    assert pseudonymizer.token("", tenant="100") == ""
    assert pseudonymizer.token(None, tenant="100") == ""


# --- Serbest metin ---------------------------------------------------------
def test_free_text_pii_is_detected(engine):
    result = engine.apply_text(
        "Iletisim satis@tedarikci.test, IBAN DE89 3704 0044 0532 0130 00",
        actor=_actor("VIEWER"),
        sink="model",
    )
    assert "satis@tedarikci.test" not in result.payload
    assert "DE89 3704 0044 0532 0130 00" not in result.payload


def test_unicode_obfuscation_is_stripped_before_detection(engine):
    """Gorunmez Unicode karakterleriyle gizlenmis IBAN yine yakalanir."""
    hidden = "IBAN DE89​3704​0044​0532​0130​00"
    result = engine.apply_text(hidden, actor=_actor("VIEWER"), sink="model")
    assert "​" not in result.payload
    assert "DE893704004405320130" not in result.payload


@pytest.mark.parametrize(
    ("value", "field_name"),
    [
        ("4500018821", "po_id"),          # 10 haneli PO numarasi
        ("5105600231", "invoice_id"),     # 10 haneli fatura numarasi
        ("R-2026-014-1", "wbs_element"),  # tireli proje kodu
        ("5000241190", "material_document"),
    ],
)
def test_sap_document_numbers_are_not_false_positives(engine, value, field_name):
    """SAP belge numaralari vergi/telefon/kart sanilip bozulmamali."""
    result = engine.apply(
        {field_name: value}, actor=_actor("PURCHASER"), sink="model", policy=DataPolicy()
    )
    assert result.payload[field_name] == value


def test_tax_number_is_detected_with_context(engine):
    result = engine.apply_text(
        "Tedarikci vergi no 1234567890 olarak kayitli.",
        actor=_actor("PURCHASER"),
        sink="model",
    )
    assert "1234567890" not in result.payload


def test_card_number_requires_luhn_validity(engine):
    valid = engine.apply_text("Kart 4111 1111 1111 1111", actor=_actor("VIEWER"), sink="model")
    assert "4111 1111 1111 1111" not in valid.payload
    # Luhn'u gecmeyen uzun sayi (parti/seri numarasi) bozulmamali.
    invalid = engine.apply_text(
        "Parti 1234567890123456", actor=_actor("PURCHASER"), sink="model"
    )
    assert "1234567890123456" in invalid.payload


# --- Log ve handoff --------------------------------------------------------
def test_logs_never_receive_commercial_or_personal_data(engine):
    result = engine.apply(
        {"net_price": 1265.0, "supplier_email": "a@b.test", "po_id": "4500018821"},
        actor=_actor("BUYER_LEAD", extra=frozenset({SCOPE_DATA_CONFIDENTIAL})),
        sink="log",
        policy=DataPolicy(),
    )
    assert result.payload["net_price"] == "***"
    assert result.payload["supplier_email"] == "***"
    # Is nesnesi kimligi korelasyon icin gereklidir ve D1'dir.
    assert result.payload["po_id"] == "4500018821"


def test_handoff_allowlist_is_fail_closed_for_unknown_pairs():
    known = handoff_allowlist("supply_chain", "procurement")
    # Ornek DEGISTI: eskiden ("finance", "master_data") kullaniliyordu, ama o
    # cift `agent_catalogue()` icinde `handoff_targets` olarak bildirilmis
    # gecerli bir akistir. Test farkinda olmadan allowlist'in EKSIK olmasina
    # dayaniyordu; allowlist tamamlaninca yanlis alarm uretti. Gercekten
    # tanimsiz bir cift kullanilir.
    unknown = handoff_allowlist("bilinmeyen_kaynak", "bilinmeyen_hedef")
    assert "business_objects" in known
    assert "business_objects" not in unknown
    assert {"summary", "evidence_ids", "correlation_id"} <= unknown


# --- Motor modlari ---------------------------------------------------------
def test_report_mode_finds_but_does_not_modify(pseudonymizer):
    engine = DLPEngine(
        field_policy=FieldAccessPolicy(), pseudonymizer=pseudonymizer, mode="report"
    )
    payload = {"supplier_iban": "DE89370400440532013000"}
    result = engine.apply(
        payload, actor=_actor("VIEWER"), sink="model",
        policy=DataPolicy(fields={"supplier_iban": DataClass.D3}),
    )
    assert result.findings  # bulgu uretilir
    assert result.payload["supplier_iban"] == "DE89370400440532013000"  # payload degismez


def test_findings_never_contain_the_value(engine):
    """DLP bulgulari loglanabilir ancak hassas degerin kendisini iceremez."""
    result = engine.apply(
        {"supplier_iban": "DE89370400440532013000"},
        actor=_actor("VIEWER"),
        sink="model",
        policy=DataPolicy(fields={"supplier_iban": DataClass.D3}),
    )
    serialized = str([f.to_dict() for f in result.findings])
    assert "DE89370400440532013000" not in serialized
    assert result.summary()["max_class"] == "D3"


def test_privacy_action_semantics():
    assert PrivacyAction.DENY.blocks_request
    assert PrivacyAction.TOKENIZE.modifies_value
    assert not PrivacyAction.ALLOW.modifies_value
