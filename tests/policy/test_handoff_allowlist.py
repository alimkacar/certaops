"""Devir (handoff) allowlist butunlugu.

`HandoffEnvelope.to_dict()` iki kapi uygular: alan allowlist'i ve DLP
`sink="handoff"`. Bu dosya birinci kapinin **eksiksiz** oldugunu korur.

Neden gerekli: allowlist'te tanimsiz bir agent cifti fail-closed yedege duser
ve is nesnesi kimliklerini sessizce dusurur. Hata uretmez, log yazmaz - yalniz
sonraki agent baglamsiz kalir ve neden oldugunu kimse anlamaz. Bu tam olarak
bir kez yasandi: allowlist 7 cift tanimliyken katalog 19 cift bildiriyordu.
"""

from __future__ import annotations

import pytest

from robotics_agent.core import agent_catalogue, handoff_from_turn
from robotics_agent.privacy import HANDOFF_FIELD_ALLOWLIST, handoff_allowlist


def _declared_pairs() -> list[tuple[str, str]]:
    return sorted(
        {
            (spec["agent"], target)
            for spec in agent_catalogue()
            for target in spec.get("handoff_targets", [])
        }
    )


def test_bildirilen_her_devir_cifti_allowlistte_tanimli():
    """Katalogdaki her `handoff_targets` girdisinin karsiligi olmali."""
    eksik = [pair for pair in _declared_pairs() if pair not in HANDOFF_FIELD_ALLOWLIST]
    assert eksik == [], (
        "Allowlist'te tanimsiz devir cifti var; bu ciftler fail-closed yedege "
        f"duser ve is nesnesi kimliklerini sessizce dusurur: {eksik}"
    )


@pytest.mark.parametrize("pair", _declared_pairs())
def test_bildirilen_ciftler_is_nesnesi_kimligi_tasiyabilir(pair):
    """Devirin ise yaramasi icin belge kimlikleri gecmeli."""
    assert "business_objects" in handoff_allowlist(*pair)


def test_tanimsiz_cift_fail_closed_daralir():
    """Bilinmeyen cift icin is nesnesi kimligi bile gecmemeli."""
    daraltilmis = handoff_allowlist("bilinmeyen_a", "bilinmeyen_b")
    assert "business_objects" not in daraltilmis
    assert "summary" in daraltilmis, "ozet ve evidence handle'i her zaman gecer"


def test_allowlist_disi_alan_zarfta_tasinmaz():
    """Alan boyutu asil korumadir: listede olmayan alan devirde gitmez."""
    envelope = handoff_from_turn(
        from_agent="bilinmeyen_a", to_agent="bilinmeyen_b",
        objective="hedef", correlation_id="c1", text="ozet",
        tool_calls=[], needs_review=False,
    )
    payload = envelope.to_dict()
    assert "business_objects" not in payload
    assert payload["dropped_fields"] == ["business_objects"], (
        "dusen alan acikca raporlanmali; sessizce kaybolmamali"
    )


def test_devirdeki_serbest_metin_dlpden_gecer(settings, purchaser):
    """DLP `sink=handoff`: D3 dusurulur, D2 maskelenir."""
    from robotics_agent.privacy import build_dlp_engine

    IBAN = "TR330006100519786457841326"
    envelope = handoff_from_turn(
        from_agent="procurement", to_agent="finance",
        objective="odeme kontrolu", correlation_id="c1",
        text=f"Tedarikci IBAN {IBAN}, iletisim satinalma@acme.com",
        tool_calls=[], needs_review=False,
    )
    payload = envelope.to_dict(dlp=build_dlp_engine(settings), actor=purchaser)
    assert IBAN not in payload["summary"]
    assert "satinalma@acme.com" not in payload["summary"]
    assert "Tedarikci" in payload["summary"], "is baglami korunmali"
