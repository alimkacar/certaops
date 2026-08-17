"""Alan bazli veri erisim politikasi.

Tool erisimi (RBAC) ile **alan erisimi** ayri kararlardir. Bir kullanicinin
`sap_purchase_order_360` cagirma yetkisi olmasi, o siparisin tedarikci
e-postasini gormeye yetkisi oldugu anlamina gelmez. Bu modul su soruyu
cevaplar:

    "Bu actor, bu alani, bu hedefe (model/istemci/log/handoff/export),
     bu detay seviyesinde ve bu amacla gorebilir mi?"

Karar bes degerden biridir: allow / mask / tokenize / drop / deny.

Onemli tasarim kurali: ayni tool farkli roller icin
farkli projeksiyon dondurur ve `full` detay ek kapsam + amac kodu ister.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from ..contracts.actor import (
    SCOPE_DATA_CONFIDENTIAL,
    SCOPE_DATA_RESTRICTED,
    SCOPE_EXPORT_CONFIDENTIAL,
    ActorContext,
)
from .classification import DataClass, DataPolicy, is_personal_field

__all__ = [
    "HANDOFF_FIELD_ALLOWLIST",
    "PURPOSE_CODES",
    "FieldAccessPolicy",
    "PrivacyAction",
    "Sink",
    "handoff_allowlist",
]

# Verinin gidecegi hedef. Ayni deger farkli hedeflerde farkli islem gorur:
# fiyat modele gidebilir ama merkezi loga asla yazilmaz.
Sink = Literal["model", "client", "log", "handoff", "export"]

# Kabul edilen isleme amaclari (`purpose_code`). Serbest metin kabul
# edilmez: amac kodu audit'te aranabilir olmali ve modelin uydurdugu bir
# gerekce yetki uretmemeli.
PURPOSE_CODES: frozenset[str] = frozenset(
    {
        "procurement_operations",
        "invoice_resolution",
        "supplier_management",
        "project_controlling",
        "audit_review",
        "incident_investigation",
    }
)


class PrivacyAction(str, Enum):
    """DLP/alan politikasi karari."""

    ALLOW = "allow"
    MASK = "mask"
    TOKENIZE = "tokenize"
    DROP = "drop"
    DENY = "deny"

    @property
    def blocks_request(self) -> bool:
        return self is PrivacyAction.DENY

    @property
    def modifies_value(self) -> bool:
        return self in {PrivacyAction.MASK, PrivacyAction.TOKENIZE, PrivacyAction.DROP}


# Agent cifti bazinda handoff allowlist'i.
# Handoff zarfinda tasinabilecek alan adlari; burada olmayan alan handoff'a
# hic yazilmaz. `objective`/`summary` serbest metindir ve ayrica DLP'den gecer.
_BASE_HANDOFF_FIELDS = frozenset(
    {"objective", "correlation_id", "evidence_ids", "warnings", "needs_review", "summary"}
)

# Is domaini agent'lari. Aralarinda **is nesnesi kimligi** (malzeme, PO, WBS -
# hepsi D1) tasinabilir; tutar, fiyat ve kisisel veri tasinamaz.
_BUSINESS_AGENTS = ("master_data", "supply_chain", "procurement", "finance")

# `platform` teshis/mutabakat agent'idir. Is verisi hakkinda tahmin yapmaz ama
# `sap_reconcile_execution` ve audit sorgusu icin BELGE KIMLIGINE ihtiyaci
# vardir; bu yuzden is nesnesi kimlikleri iki yonde de tasinir.
_DIAGNOSTIC_AGENT = "platform"

_WITH_OBJECTS = _BASE_HANDOFF_FIELDS | {"business_objects"}

# Tablo `core.agent_catalogue()` icindeki `handoff_targets` bildirimlerinin
# tamamini kapsamak ZORUNDADIR. Kapsamayan bir cift fail-closed yedege duser ve
# is nesnesi kimligini sessizce dusurur; bu, cok-ajanli akisi bozar ama hicbir
# hata uretmez. `tests/policy/test_handoff_allowlist.py` bu butunlugu korur.
#
# Not: cift boyutu su an ayrim yapmiyor (tum ciftler ayni alan kumesini
# aliyor). Koruma **alan boyutundadir**: zarfa yeni bir alan eklendiginde,
# burada acikca izin verilmedikce hicbir devirde tasinmaz. Cift boyutu ileride
# bir akisi daraltmak gerektiginde hazir duruyor.
HANDOFF_FIELD_ALLOWLIST: dict[tuple[str, str], frozenset[str]] = {
    # Is domaini <-> is domaini
    **{
        (source, target): _WITH_OBJECTS
        for source in _BUSINESS_AGENTS
        for target in _BUSINESS_AGENTS
        if source != target
    },
    # Teshis agent'i <-> is domaini (her iki yon)
    **{(_DIAGNOSTIC_AGENT, target): _WITH_OBJECTS for target in _BUSINESS_AGENTS},
    **{(source, _DIAGNOSTIC_AGENT): _WITH_OBJECTS for source in _BUSINESS_AGENTS},
}


def handoff_allowlist(from_agent: str, to_agent: str) -> frozenset[str]:
    """Bu agent cifti icin tasinabilecek handoff alanlari.

    Tanimlanmamis cift icin **daraltilmis** taban set doner (fail-closed):
    is nesnesi kimlikleri bile gecmez, yalniz ozet ve evidence handle'i gecer.
    """
    return HANDOFF_FIELD_ALLOWLIST.get((from_agent, to_agent), _BASE_HANDOFF_FIELDS)


@dataclass(frozen=True)
class FieldAccessPolicy:
    """Actor + veri sinifi + hedef -> islem karari.

    `strict_unknown` uretim profilinde True olur: `DataPolicy` tarafindan
    siniflandirilmamis alan D3 kabul edilir.
    """

    strict_unknown: bool = False

    # --- Ana karar ---------------------------------------------------------
    def decide(
        self,
        *,
        actor: ActorContext,
        field_name: str,
        data_class: DataClass,
        sink: Sink,
        policy: DataPolicy | None = None,
        detail: str = "standard",
        purpose: str = "",
        personal: bool | None = None,
    ) -> PrivacyAction:
        if data_class.level <= DataClass.D1.level:
            return PrivacyAction.ALLOW
        if data_class is DataClass.D2:
            return self._decide_confidential(
                actor=actor, field_name=field_name, sink=sink, policy=policy,
                detail=detail, purpose=purpose,
                personal=is_personal_field(field_name) if personal is None else personal,
            )
        return self._decide_restricted(
            actor=actor, sink=sink, detail=detail, purpose=purpose
        )

    def _decide_confidential(
        self,
        *,
        actor: ActorContext,
        field_name: str,
        sink: Sink,
        policy: DataPolicy | None,
        detail: str,
        purpose: str,
        personal: bool,
    ) -> PrivacyAction:
        # Merkezi log hicbir kosulda ticari/kisisel veri almaz.
        if sink == "log":
            return PrivacyAction.MASK
        # Handoff yalniz tanimli alanlari tasir; ticari deger tasimaz.
        if sink == "handoff":
            return PrivacyAction.MASK
        if not actor.has_scope(SCOPE_DATA_CONFIDENTIAL):
            return PrivacyAction.MASK
        if sink == "export":
            export_scope = (policy.export_scope if policy else "") or SCOPE_EXPORT_CONFIDENTIAL
            if not actor.has_scope(export_scope):
                return PrivacyAction.DENY
            return PrivacyAction.ALLOW
        if sink == "model":
            allowlisted = policy is not None and policy.is_model_allowed(
                field_name, DataClass.D2
            )
            # Kisisel veri modele **veri minimizasyonu** geregi gitmez: model
            # fiyati hesaplamak icin gorur, tedarikcinin e-postasini gormesi
            # karara katki saglamaz. Tool acikca izin verirse gecer.
            if personal and not (policy is not None and policy.model_allowed and allowlisted):
                return PrivacyAction.MASK
            if policy is not None and not allowlisted:
                # Tool `model_allowed` allowlist'i bildirmisse, disindaki D2
                # alanlar modele ham gitmez. Karar tool sozlesmesinde, promptta
                # degil.
                return PrivacyAction.MASK
        if detail == "full" and not _full_detail_granted(actor, purpose):
            return PrivacyAction.MASK
        return PrivacyAction.ALLOW

    def _decide_restricted(
        self, *, actor: ActorContext, sink: Sink, detail: str, purpose: str
    ) -> PrivacyAction:
        # D3 modele **hicbir kosulda** ham gitmez.
        if sink == "model":
            return PrivacyAction.TOKENIZE
        if sink == "log":
            return PrivacyAction.DROP
        if sink == "handoff":
            return PrivacyAction.DROP
        if sink == "export":
            if actor.has_scope(SCOPE_DATA_RESTRICTED) and actor.has_scope(
                SCOPE_EXPORT_CONFIDENTIAL
            ):
                # R4/D3 disa aktarim ayrica iki kisili kontrol ister;
                # o kontrol policy katmaninda uygulanir, burada kapi aciktir.
                return PrivacyAction.ALLOW
            return PrivacyAction.DENY
        # sink == "client": yalniz acik D3 kapsami + full detay + gecerli amac.
        if (
            actor.has_scope(SCOPE_DATA_RESTRICTED)
            and detail == "full"
            and purpose in PURPOSE_CODES
        ):
            return PrivacyAction.ALLOW
        return PrivacyAction.TOKENIZE

    # --- Detay seviyesi kapisi ---------------------------------------------
    def full_detail_blocker(self, actor: ActorContext, *, purpose: str) -> str | None:
        """`detail=full` istegi icin engel varsa gerekce dondurur."""
        if not actor.has_scope(SCOPE_DATA_CONFIDENTIAL):
            return (
                "full detay seviyesi icin " + SCOPE_DATA_CONFIDENTIAL + " kapsami gerekir; "
                "sonuc standard seviyeye dusuruldu."
            )
        if purpose not in PURPOSE_CODES:
            return (
                "full detay seviyesi gecerli bir purpose_code ister "
                f"({', '.join(sorted(PURPOSE_CODES))}); sonuc standard seviyeye dusuruldu."
            )
        return None

    def effective_detail(self, requested: str, actor: ActorContext, *, purpose: str) -> str:
        """Yetki yetersizse `full` istegini sessizce degil, **dusurerek** karsilar."""
        if requested != "full":
            return requested
        return "standard" if self.full_detail_blocker(actor, purpose=purpose) else "full"

    def to_dict(self) -> dict[str, Any]:
        return {"strict_unknown": self.strict_unknown}


def _full_detail_granted(actor: ActorContext, purpose: str) -> bool:
    return actor.has_scope(SCOPE_DATA_CONFIDENTIAL) and purpose in PURPOSE_CODES
