"""Policy Decision Point: yurutmeden once zorunlu karar.

Temel kural **deny by default**'tur. `writes=True` gibi pasif metadata yerine
her tool cagrisi handler'a ulasmadan once burada degerlendirilir ve karar
audit'e yazilir.

Degerlendirme sirasi (fail-fast):
  1. Tool taninmiyor mu?
  2. Actor kimligi dogrulanmis mi?
  3. Gerekli kapsamlar (RBAC) var mi?
  4. Argumanlardaki organizasyon alanlari actor'un yetki alaninda mi (ABAC)?
  5. Risk seviyesi >= R3 icin yazma penceresi acik mi?
  6. Risk seviyesi >= R3 icin gecerli onay kaniti var mi (payload hash, expiry,
     nonce, SoD, R4'te cift onay)?
  7. Yukumlulukler (idempotency, read-after-write, diff gosterimi) uretilir.

Prompt icerigi bu kararlari degistiremez: karar yalnizca actor, tool sozlesmesi,
argumanlar ve onay deposundan uretilir.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from enum import Enum
from typing import Any, Protocol

from ..contracts import ActorContext, ExecutionContext, RiskTier
from ..privacy.classification import DataClass
from ..risk import (
    ImpactAssessment,
    ImpactProfile,
    ImpactSignals,
    RiskObligations,
    score_impact,
)
from .approvals import ApprovalError, ApprovalRecord, ApprovalStore

log = logging.getLogger(__name__)

# Argumanlarda gorulen organizasyon alanlari -> ActorContext kontrolu.
# Tarama **ic ice** yapilir: `items[*].plant` gibi alanlar da yakalanir. Yeni
# bir tool nested bir tesis alani eklerse ayrica bildirim yapmasi gerekmez;
# anahtar adi yeterlidir.
ORG_ARGUMENT_KEYS: dict[str, str] = {
    "plant": "plant",
    "plants": "plant",
    "source_plant": "plant",
    "target_plant": "plant",
    "supplying_plant": "plant",
    "receiving_plant": "plant",
    "company_code": "company_code",
    "company_codes": "company_code",
    "purchasing_org": "purchasing_org",
    "purchasing_organization": "purchasing_org",
    "purch_org": "purchasing_org",
}

# Organizasyon alanlarinin okunabilir adlari (ret gerekcesinde kullanilir).
_ORG_LABELS = {
    "plant": "Tesis",
    "company_code": "Sirket kodu",
    "purchasing_org": "Satinalma organizasyonu",
}

# Ic ice tarama derinligi siniri: kotu niyetli derin yapilar CPU yakmasin.
_MAX_SCAN_DEPTH = 8


@dataclass(frozen=True)
class OrgDefaults:
    """Handler'in arguman verilmediginde kullanacagi organizasyon degerleri.

    Policy bu degerleri de actor kapsamina karsi dogrular; aksi halde tesis
    alanini bos birakmak kapsam kontrolunu atlatmanin yolu olurdu.
    """

    plant: str = ""
    company_code: str = ""
    purchasing_org: str = ""

    def value_for(self, kind: str) -> str:
        return {
            "plant": self.plant,
            "company_code": self.company_code,
            "purchasing_org": self.purchasing_org,
        }[kind]

    @classmethod
    def from_settings(cls, settings: Any) -> OrgDefaults:
        cfg = settings.sap
        return cls(
            plant=cfg.plant, company_code=cfg.company_code, purchasing_org=cfg.purch_org
        )

# Yukumlulukler
OBLIGATION_IDEMPOTENCY = "idempotency_key"
OBLIGATION_READ_AFTER_WRITE = "read_after_write"
OBLIGATION_SHOW_DIFF = "show_diff"
OBLIGATION_SHOW_ASSUMPTIONS = "show_assumptions"
OBLIGATION_DRY_RUN_FORCED = "dry_run_forced"
OBLIGATION_DUAL_CONTROL = "dual_control"
OBLIGATION_CONSUME_APPROVAL = "consume_approval"
# approval_policy="threshold" iken tutar cagri aninda bilinmiyorsa, tool
# fiyatlandirdigi gercek tutari require_approval_for_value ile dogrulamak zorundadir.
OBLIGATION_VERIFY_VALUE = "verify_value_against_threshold"
# Runtime risk skorunun urettigi ek yukumlulukler.
OBLIGATION_MASK_SENSITIVE = "mask_sensitive_fields"
OBLIGATION_BLOCK_EXPORT = "block_export"
OBLIGATION_EXPLAIN_ESCALATION = "explain_risk_escalation"
# Runtime skoru bildirilen tabani asti: cagri, dogrulanmis tutarla yeniden
# degerlendirilmeden yazma yapamaz.
OBLIGATION_REASSESS_AFTER_PRICING = "reassess_impact_after_pricing"


class PolicyOutcome(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class ToolContract(Protocol):
    """Policy'nin tool'dan bekledigi minimum sozlesme (ToolSpec bunu karsilar)."""

    name: str
    risk_tier: RiskTier
    required_scopes: tuple[str, ...]
    approval_policy: str
    approve_scope: str
    idempotent: bool
    # Tool, arguman verilmediginde sistem varsayilani organizasyon degerlerini
    # kullaniyor mu? True ise varsayilanlar da actor kapsamina karsi denetlenir.
    applies_org_defaults: bool
    # Cagri aninda etki skoru hesaplamak icin statik profil.
    impact_profile: ImpactProfile


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    tool: str
    # Uygulanan seviye: `max(bildirilen, runtime)`. Runtime degerlendirme
    # bildirilen tabani asla dusuremez.
    risk_tier: RiskTier
    reasons: tuple[str, ...] = ()
    obligations: tuple[str, ...] = ()
    missing_scopes: tuple[str, ...] = ()
    denial_code: str = ""
    approval_id: str = ""
    approval: ApprovalRecord | None = None
    impact: ImpactAssessment | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is PolicyOutcome.ALLOW

    @property
    def declared_tier(self) -> RiskTier:
        return self.impact.declared_tier if self.impact is not None else self.risk_tier

    @property
    def escalated(self) -> bool:
        return self.impact is not None and self.impact.escalated

    def requires(self, obligation: str) -> bool:
        return obligation in self.obligations

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "outcome": self.outcome.value,
            "tool": self.tool,
            "risk_tier": self.risk_tier.value,
        }
        if self.reasons:
            payload["reasons"] = list(self.reasons)
        if self.obligations:
            payload["obligations"] = list(self.obligations)
        if self.missing_scopes:
            payload["missing_scopes"] = list(self.missing_scopes)
        if self.denial_code:
            payload["denial_code"] = self.denial_code
        if self.approval_id:
            payload["approval_id"] = self.approval_id
        if self.impact is not None:
            # Risk karari aciklanabilir boyutlarla audit'e yazilir.
            payload["impact"] = self.impact.to_dict()
        return payload

    def as_error(self) -> dict[str, Any]:
        """Modele/istemciye donen, yetki vermeyen ama yol gosteren hata govdesi."""
        return {
            "error": "Policy reddi: " + ("; ".join(self.reasons) or "yetkisiz islem."),
            "denial_code": self.denial_code or "POLICY_DENIED",
            "tool": self.tool,
            "risk_tier": self.risk_tier.value,
            "missing_scopes": list(self.missing_scopes),
            "remediation": _remediation_hint(self.denial_code),
        }


def _remediation_hint(code: str) -> str:
    return {
        "UNKNOWN_TOOL": "Tool adini kontrol edin; sap_list_domains ve /tools ile mevcut SAP yeteneklerini listeleyin.",
        "AUTH_REQUIRED": "Istek dogrulanmis bir kimlik tasimiyor. Oturum acin.",
        "MISSING_SCOPE": "Bu islem icin gereken is rolu atanmali. Yetki talebi SAP/BTP yoneticisine gider.",
        "ORG_SCOPE": "Argumandaki tesis/sirket kodu/satinalma organizasyonu yetki alaninizin disinda.",
        "WINDOW_CLOSED": "Yazma penceresi kapali. Pencere icinde tekrar deneyin veya istisna talep edin.",
        "APPROVAL_REQUIRED": "Once ilgili prepare tool'unu calistirin, onay alin ve approval_id ile tekrar cagirin.",
        "APPROVAL_INVALID": "Onay kaydi gecersiz. Guncel payload icin yeni onay alin.",
        "APPROVAL_CONSUMED": (
            "Bu onay bir yurutmede kullanildi. Yeni onay istemeden once "
            "sap_reconcile_execution ile islemin SAP'ta olusup olusmadigini dogrulayin; "
            "olusmussa tekrar yazma gerekmez."
        ),
        "DRY_RUN_LOCKED": "Ortam salt-simulasyon modunda. Gercek yazma icin SAP_DRY_RUN=false gerekir.",
    }.get(code, "Kararin gerekcesi audit kaydinda; yetkili ile birlikte inceleyin.")


def _parse_window(window: str) -> tuple[time, time] | None:
    if not window:
        return None
    try:
        start_raw, end_raw = window.split("-", 1)
        start_h, start_m = (int(x) for x in start_raw.strip().split(":"))
        end_h, end_m = (int(x) for x in end_raw.strip().split(":"))
        return time(start_h, start_m), time(end_h, end_m)
    except (ValueError, TypeError):
        log.warning("AGENT_WRITE_WINDOW cozumlenemedi: %r", window)
        return None


def _window_open(window: str, *, now: datetime | None = None) -> bool:
    parsed = _parse_window(window)
    if parsed is None:
        return True
    start, end = parsed
    current = (now or datetime.now(timezone.utc)).time()
    if start <= end:
        return start <= current <= end
    # Gece yarisini asan pencere (or. 22:00-06:00)
    return current >= start or current <= end


@dataclass
class PolicyDecisionPoint:
    """Tool cagrilarini degerlendiren merkezi karar noktasi."""

    approvals: ApprovalStore | None = None
    write_window: str = ""
    forced_dry_run: bool = True
    # Bu tutarin uzerindeki R3 islemleri approval_policy="threshold" olsa bile onay ister.
    approval_threshold: float = 25_000.0
    known_tools: frozenset[str] = field(default_factory=frozenset)
    # Handler'in arguman yoksa kullanacagi organizasyon degerleri de actor
    # kapsamina karsi denetlenir.
    org_defaults: OrgDefaults = field(default_factory=OrgDefaults)
    # enforce -> effective_tier uygulanir | report -> yalniz audit'e yazilir
    risk_mode: str = "enforce"

    # --- Ana degerlendirme --------------------------------------------------
    def evaluate(
        self,
        spec: ToolContract | None,
        arguments: Mapping[str, Any],
        execution: ExecutionContext,
        *,
        tool_name: str = "",
    ) -> PolicyDecision:
        name = spec.name if spec is not None else tool_name

        if spec is None:
            return PolicyDecision(
                outcome=PolicyOutcome.DENY,
                tool=name,
                risk_tier=RiskTier.R0,
                reasons=(f"Bilinmeyen tool: {name}",),
                denial_code="UNKNOWN_TOOL",
            )

        # 1b. Runtime etki degerlendirmesi.
        # Karar akisinin geri kalani `tier` uzerinden yurur; bu, bildirilen
        # taban ile runtime skorunun **maksimumu**dur.
        assessment = self.assess_impact(spec, arguments)
        tier = assessment.effective_tier if self.risk_mode == "enforce" else spec.risk_tier

        actor = execution.actor
        reasons: list[str] = []
        obligations: list[str] = []

        # 2. Kimlik
        if not actor.scopes:
            return PolicyDecision(
                outcome=PolicyOutcome.DENY,
                tool=name,
                risk_tier=tier,
                reasons=("Cagirici hicbir yetki kapsami tasimiyor.",),
                denial_code="AUTH_REQUIRED",
                impact=assessment,
            )

        # 3. RBAC
        missing = actor.missing_scopes(spec.required_scopes)
        if missing:
            return PolicyDecision(
                outcome=PolicyOutcome.DENY,
                tool=name,
                risk_tier=tier,
                reasons=(f"Eksik yetki kapsami: {', '.join(missing)}",),
                missing_scopes=missing,
                denial_code="MISSING_SCOPE",
                impact=assessment,
            )

        # 4. ABAC - organizasyon kapsami (ic ice alanlar + cozulmus varsayilanlar)
        org_violations = self._check_org_scope(actor, arguments, spec=spec)
        if org_violations:
            return PolicyDecision(
                outcome=PolicyOutcome.DENY,
                tool=name,
                risk_tier=tier,
                reasons=tuple(org_violations),
                denial_code="ORG_SCOPE",
                impact=assessment,
            )

        # 4b. Veri sinifina ve skora bagli gizlilik yukumlulukleri.
        obligations.extend(_privacy_obligations(assessment))
        if assessment.escalated:
            reasons.extend(assessment.reasons[-1:])

        # 5-6. Mutating akis
        if tier.requires_approval:
            if not _window_open(self.write_window):
                return PolicyDecision(
                    outcome=PolicyOutcome.DENY,
                    tool=name,
                    risk_tier=tier,
                    reasons=(f"Yazma penceresi kapali ({self.write_window}).",),
                    denial_code="WINDOW_CLOSED",
                    impact=assessment,
                )

            approval_id = str(arguments.get("approval_id") or "").strip()
            needs_approval = self._approval_required(spec, arguments)

            if needs_approval and not approval_id:
                return PolicyDecision(
                    outcome=PolicyOutcome.DENY,
                    tool=name,
                    risk_tier=tier,
                    reasons=(
                        "Bu islem dogrulanmis onay kaydi gerektirir. "
                        "Sohbette verilen 'evet' onay kaniti degildir.",
                    ),
                    denial_code="APPROVAL_REQUIRED",
                    impact=assessment,
                )

            record: ApprovalRecord | None = None
            if approval_id:
                if self.approvals is None:
                    return PolicyDecision(
                        outcome=PolicyOutcome.DENY,
                        tool=name,
                        risk_tier=tier,
                        reasons=("Onay deposu yapilandirilmamis; yazma reddedildi.",),
                        denial_code="APPROVAL_INVALID",
                        impact=assessment,
                    )
                approved_payload = _approval_payload(arguments)
                try:
                    record = self.approvals.validate(
                        approval_id,
                        tool=name,
                        payload=approved_payload,
                        actor=actor,
                        risk_tier=tier,
                        approve_scope=spec.approve_scope,
                    )
                except ApprovalError as exc:
                    # Tuketilmis onay ayri bir koda ayrilir: cagirici muhtemelen
                    # basarili bir yazmayi tekrar deniyor. Dogru cikis yolu yeni
                    # onay degil, mutabakattir.
                    code = (
                        "APPROVAL_CONSUMED"
                        if exc.code == "ALREADY_CONSUMED"
                        else "APPROVAL_INVALID"
                    )
                    return PolicyDecision(
                        outcome=PolicyOutcome.DENY,
                        tool=name,
                        risk_tier=tier,
                        reasons=(str(exc),),
                        denial_code=code,
                        approval_id=approval_id,
                        impact=assessment,
                    )
                obligations.append(OBLIGATION_CONSUME_APPROVAL)
                reasons.append(
                    f"Onay {approval_id} gecerli "
                    f"({', '.join(a.subject for a in record.approvers)})."
                )

            obligations.append(OBLIGATION_READ_AFTER_WRITE)
            if spec.approval_policy.lower() == "threshold" and record is None:
                obligations.append(OBLIGATION_VERIFY_VALUE)
            if spec.idempotent:
                obligations.append(OBLIGATION_IDEMPOTENCY)
            if tier.requires_dual_control:
                obligations.append(OBLIGATION_DUAL_CONTROL)
            # Tutar hala SAP'ten dogrulanmadiysa tool, fiyatlandirma sonrasi
            # `reassess()` cagirmak zorundadir.
            if _profile_of(spec).financial_fields and not assessment.value_verified:
                obligations.append(OBLIGATION_REASSESS_AFTER_PRICING)
            if self.forced_dry_run:
                obligations.append(OBLIGATION_DRY_RUN_FORCED)
                reasons.append("Ortam salt-simulasyon modunda; gercek yazma yapilmayacak.")

            return PolicyDecision(
                outcome=PolicyOutcome.ALLOW,
                tool=name,
                risk_tier=tier,
                reasons=tuple(reasons),
                obligations=tuple(obligations),
                approval_id=approval_id,
                approval=record,
                impact=assessment,
            )

        if tier is RiskTier.R2:
            obligations.append(OBLIGATION_SHOW_DIFF)
        if tier is RiskTier.R1:
            obligations.append(OBLIGATION_SHOW_ASSUMPTIONS)

        return PolicyDecision(
            outcome=PolicyOutcome.ALLOW,
            tool=name,
            risk_tier=tier,
            reasons=tuple(reasons),
            obligations=tuple(obligations),
            impact=assessment,
        )

    # --- Runtime etki degerlendirmesi ---------------------------------------
    def assess_impact(
        self, spec: ToolContract, arguments: Mapping[str, Any]
    ) -> ImpactAssessment:
        """Cagri oncesi etki skoru.

        Argumandan okunan tutar `value_verified=False` ile girer: skoru
        yukseltebilir ama dusuremez. Gercek dogrulama SAP fiyatlandirmasindan
        sonra `reassess()` ile yapilir.
        """
        profile = _profile_of(spec)
        data_class = _spec_data_class(spec)
        signals = ImpactSignals.from_arguments(profile, arguments, data_class=data_class)
        return score_impact(profile, signals, declared_tier=spec.risk_tier)

    def reassess(
        self,
        spec: ToolContract,
        decision: PolicyDecision,
        *,
        total_value: float,
        currency: str = "",
        record_count: int | None = None,
        external_commitment: bool | None = None,
    ) -> ImpactAssessment:
        """SAP fiyatlandirmasi sonrasi ikinci etki degerlendirmesi.

        Bu, esigi dusuk beyanla atlatma denemesinin kapandigi noktadir: tutar
        artik SAP'ten/prepare snapshot'indan gelir ve `value_verified=True`
        isaretlenir.
        """
        profile = _profile_of(spec)
        base = decision.impact
        signals = ImpactSignals(
            total_value=float(total_value),
            currency=currency,
            value_verified=True,
            record_count=record_count if record_count is not None else 0,
            org_units=1,
            data_class=base.data_class if base else _spec_data_class(spec),
            period_sensitive=profile.period_sensitive,
            external_commitment=external_commitment,
        )
        return score_impact(profile, signals, declared_tier=spec.risk_tier)

    def escalation_blocker(
        self, decision: PolicyDecision, assessment: ImpactAssessment
    ) -> str | None:
        """Yeniden degerlendirme mevcut onayi yetersiz birakiyor mu?

        Dogrulanmis tutarla R4'e cikan bir islem, elindeki tek onayli R3
        kaydiyla devam edemez; R4 iki ayri ve gecerli onaylayici gerektirir.
        """
        if self.risk_mode != "enforce":
            return None
        if assessment.effective_tier.level <= decision.risk_tier.level:
            return None
        if assessment.effective_tier is RiskTier.R4:
            record = decision.approval
            approvers = len({a.subject for a in record.approvers}) if record else 0
            if approvers < 2:
                return (
                    f"Dogrulanmis etki skoru {assessment.score} islemi "
                    f"{assessment.effective_tier.value} seviyesine yukseltti; "
                    f"iki ayri onaylayan gerekiyor (mevcut: {approvers})."
                )
        return (
            f"Dogrulanmis etki skoru {assessment.score} islemi "
            f"{decision.risk_tier.value} yerine {assessment.effective_tier.value} "
            "seviyesine yukseltti; yeni onay gerekiyor."
        )

    # --- Yardimcilar --------------------------------------------------------
    def _approval_required(self, spec: ToolContract, arguments: Mapping[str, Any]) -> bool:
        policy = (spec.approval_policy or "always").lower()
        if policy == "none":
            return False
        if policy in {"always", "dual"}:
            return True
        if policy == "threshold":
            declared = _declared_value(arguments)
            if declared is not None:
                return declared > self.approval_threshold
            # Tutar cagri aninda bilinmiyor (SAP fiyatlandirmasi sonra yapilir).
            # Burada onay ISTEMEK yerine ikinci kapiya devrediyoruz:
            # tool, fiyatlandirdigi gercek tutari `require_approval_for_value`
            # ile dogrular. Bu daha guvenlidir; modelin bildirdigi bir tutara
            # guvenmek esigin dusuk beyanla atlatilmasina acik kapi birakir.
            return False
        return True

    def _check_org_scope(
        self,
        actor: ActorContext,
        arguments: Mapping[str, Any],
        *,
        spec: ToolContract | None = None,
    ) -> list[str]:
        """Argumanlardaki ve varsayilanlardaki organizasyon degerlerini denetler.

        Iki fail-closed kapi:
          1. Ic ice tarama: `items[*].plant` gibi alanlar da yakalanir.
          2. Cozulmus varsayilanlar: tesis hic verilmemisse handler'in
             kullanacagi sistem varsayilani da kontrol edilir; aksi halde alani
             bos birakmak kapsam kontrolunu atlatmanin yolu olurdu.
        """
        violations: list[str] = []
        found: dict[str, set[str]] = {"plant": set(), "company_code": set(), "purchasing_org": set()}
        self._collect_org_values(arguments, found, path="", depth=0, violations=violations)

        for kind, values in found.items():
            for value in sorted(values):
                if not actor.permits(kind, value):
                    violations.append(
                        f"{_ORG_LABELS[kind]} '{value}' actor yetki alaninda degil."
                    )

        # Argumanda hic verilmemis alanlar icin sistem varsayilani devreye girer.
        if spec is not None and getattr(spec, "applies_org_defaults", False):
            for kind in ("plant", "company_code", "purchasing_org"):
                if found[kind]:
                    continue
                default_value = self.org_defaults.value_for(kind)
                if default_value and not actor.permits(kind, default_value):
                    violations.append(
                        f"{_ORG_LABELS[kind]} argumanda verilmedi; sistem varsayilani "
                        f"'{default_value}' actor yetki alaninda degil. Yetkili bir deger "
                        "acikca belirtin."
                    )
        return violations

    @classmethod
    def _collect_org_values(
        cls,
        node: Any,
        found: dict[str, set[str]],
        *,
        path: str,
        depth: int,
        violations: list[str],
    ) -> None:
        """Arguman agacini gezerek organizasyon alanlarini toplar."""
        if depth > _MAX_SCAN_DEPTH:
            violations.append(
                f"Arguman yapisi cok derin ({path}); organizasyon kapsami dogrulanamadi."
            )
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                kind = ORG_ARGUMENT_KEYS.get(str(key))
                child_path = f"{path}.{key}" if path else str(key)
                if kind is not None:
                    for item in cls._as_values(value):
                        found[kind].add(item)
                    continue
                cls._collect_org_values(
                    value, found, path=child_path, depth=depth + 1, violations=violations
                )
        elif isinstance(node, list | tuple | set):
            for index, item in enumerate(node):
                cls._collect_org_values(
                    item, found, path=f"{path}[{index}]", depth=depth + 1, violations=violations
                )

    @staticmethod
    def _as_values(value: Any) -> Iterable[str]:
        items = value if isinstance(value, list | tuple | set) else [value]
        return [str(i) for i in items if i not in (None, "")]

    def require_approval_for_value(
        self,
        decision: PolicyDecision,
        *,
        value: float,
        currency: str,
    ) -> str | None:
        """Fiyatlandirma sonrasi ikinci kapi.

        Model argumanda dusuk bir tutar bildirip esigi atlatmaya calisabilir.
        Tool, hazirlanan taslagin gercek tutarini bu fonksiyona verir; onay kaydi
        yoksa veya onaydaki `max_value` asiliyorsa gerekce dondurulur.
        """
        if value <= self.approval_threshold and decision.approval is None:
            return None
        if decision.approval is None:
            return (
                f"Dogrulanmis tutar {value:,.2f} {currency} onay esigini "
                f"({self.approval_threshold:,.2f} {currency}) asiyor; onay kaydi yok."
            )
        limit = (decision.approval.scope or {}).get("max_value")
        if limit is not None and value > float(limit):
            return (
                f"Dogrulanmis tutar {value:,.2f} {currency} onaydaki ust sinirin "
                f"({float(limit):,.2f} {currency}) uzerinde. Yeni onay gerekiyor."
            )
        return None


def _profile_of(spec: ToolContract) -> ImpactProfile:
    """Tool'un etki profili; bildirmemis eski sozlesmeler icin bos profil.

    Bos profil skoru 0 birakir, yani runtime yukseltmesi yapmaz. Bildirilen
    taban seviye zaten korunur (`effective_tier = max(...)`), bu yuzden eksik
    profil bir yetki bosluguna donusmez.
    """
    return getattr(spec, "impact_profile", None) or ImpactProfile()


def _spec_data_class(spec: ToolContract) -> DataClass:
    """Tool'un bildirdigi en yuksek veri sinifi.

    `data_policy` tasimayan (eski) bir sozlesme icin D1 kabul edilir; boylece
    gizlilik yukumlulukleri sessizce kapanmaz ama gecis de kirilmaz.
    """
    policy = getattr(spec, "data_policy", None)
    if policy is None:
        return DataClass.D1
    return policy.max_declared_class


def _privacy_obligations(assessment: ImpactAssessment) -> list[str]:
    """Operasyon riskinden ayri degerlendirilen veri sinifi yukumlulukleri."""
    derived = RiskObligations.derive(assessment)
    out: list[str] = []
    if derived.masking_required:
        out.append(OBLIGATION_MASK_SENSITIVE)
    if derived.export_blocked:
        out.append(OBLIGATION_BLOCK_EXPORT)
    if derived.justification_required:
        out.append(OBLIGATION_EXPLAIN_ESCALATION)
    return out


def _declared_value(arguments: Mapping[str, Any]) -> float | None:
    for key in ("total_value", "estimated_value", "amount", "net_value"):
        raw = arguments.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def approval_payload_for(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Onay hash'i hesaplanan kanonik payload.

    Hash'e girmeyen alanlar:
      - `approval_id`: onayin kendisi
      - `idempotency_key`: teknik retry guard, is icerigi degil
      - `detail`/`cursor`/`limit`/`include_evidence`: yalniz sunum kontrolu
      - deger olarak `None` veya bos metin tasiyan alanlar

    Bos degerlerin dusurulmesi zorunlu: bir tool `header_text=""` varsayilaniyla
    cagrildiginda ile hic verilmediginde is icerigi aynidir. Normalize edilmezse
    `*_prepare` ile `*_submit` farkli hash uretir ve gecerli onay reddedilir.
    """
    excluded = {"approval_id", "idempotency_key", "detail", "cursor", "limit", "include_evidence"}
    return {
        k: v
        for k, v in sorted(arguments.items())
        if k not in excluded and v is not None and v != ""
    }


# Geriye donuk ic kullanim adi
_approval_payload = approval_payload_for
