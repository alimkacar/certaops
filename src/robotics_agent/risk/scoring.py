"""Cagri anindaki 0-100 etki skoru ve `effective_tier` hesabi.

Alti boyut toplanir, 100 ile sinirlanir ve seviyeye cevrilir:

    Eylem etkisi              0-60   read=0 ... bulk/destructive=60
    Finansal maruziyet        0-20   SAP'te dogrulanan tutar
    Kayit/organizasyon genisligi 0-10
    Geri alinabilirlik        0-15
    Harici taahhut/donem      0-10
    Ayricalikli ana veri      0-10

Iki kural bu modulun varlik sebebidir:

  1. **`effective_tier = max(declared, runtime)`.** Runtime degerlendirme
     gelistiricinin bildirdigi taban riski asla dusuremez.
  2. **Tutar SAP'ten dogrulanir.** Modelin argumanda bildirdigi tutar
     `verified=False` isaretiyle girer ve *skoru dusurmek icin kullanilamaz*;
     yalnizca yukseltebilir. Dusuk tutar beyaniyla onay atlatma denemesi bu
     yuzden basarisiz olur.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..contracts.actor import RiskTier
from ..privacy.classification import DataClass
from .impact import ImpactProfile, MutationKind, Reversibility

__all__ = [
    "SCORE_BANDS",
    "ImpactAssessment",
    "ImpactSignals",
    "score_impact",
    "tier_for_score",
]

# Skor -> runtime risk seviyesi.
SCORE_BANDS: tuple[tuple[int, RiskTier], ...] = (
    (9, RiskTier.R0),
    (24, RiskTier.R1),
    (44, RiskTier.R2),
    (69, RiskTier.R3),
    (100, RiskTier.R4),
)

# Finansal maruziyet esikleri (para birimi bazinda normalize edilmez; kurumsal
# kurulumda tek raporlama para birimine cevrilmelidir).
_VALUE_BANDS: tuple[tuple[float, int], ...] = (
    (1_000, 2),
    (10_000, 6),
    (50_000, 10),
    (250_000, 15),
    (1_000_000, 18),
    (float("inf"), 20),
)

_RECORD_BANDS: tuple[tuple[int, int], ...] = (
    (1, 0),
    (10, 2),
    (50, 4),
    (200, 6),
    (1_000, 8),
    (10**9, 10),
)


def tier_for_score(score: int) -> RiskTier:
    for ceiling, tier in SCORE_BANDS:
        if score <= ceiling:
            return tier
    return RiskTier.R4


@dataclass
class ImpactSignals:
    """Cagri anindaki olculen degerler.

    `value_verified` kritik alandir: tutarin SAP'ten mi yoksa cagiricinin
    beyanindan mi geldigini soyler. Dogrulanmamis tutar skoru yukseltebilir
    ama **dusuremez**; dogrulanmamis dusuk bir beyan hicbir puan indirimi
    saglamaz.
    """

    total_value: float = 0.0
    currency: str = ""
    value_verified: bool = False
    record_count: int = 0
    org_units: int = 0
    data_class: DataClass = DataClass.D1
    period_sensitive: bool = False
    external_commitment: bool | None = None
    privileged_master_data: bool | None = None
    notes: tuple[str, ...] = ()

    @classmethod
    def from_arguments(
        cls,
        profile: ImpactProfile,
        arguments: Mapping[str, Any],
        *,
        data_class: DataClass = DataClass.D1,
    ) -> ImpactSignals:
        """Argumanlardan **on** sinyal cikarir (yalniz yukari yonlu).

        Argumandaki tutar `value_verified=False` ile isaretlenir. Kayit sayisi
        ise argumandan guvenle okunabilir: kalem listesinin uzunlugu modelin
        beyani degil, istegin kendisidir.
        """
        declared_value = 0.0
        for name in profile.financial_fields or ("total_value", "estimated_value", "amount"):
            raw = arguments.get(name)
            if raw is None:
                continue
            try:
                declared_value = max(declared_value, float(raw))
            except (TypeError, ValueError):
                continue

        count = 0
        if profile.record_count_field:
            raw_count = arguments.get(profile.record_count_field)
            if isinstance(raw_count, list | tuple):
                count = len(raw_count)
            else:
                try:
                    count = int(raw_count or 0)
                except (TypeError, ValueError):
                    count = 0
        if not count:
            for candidate in ("items", "requests", "lines", "material_ids"):
                value = arguments.get(candidate)
                if isinstance(value, list | tuple):
                    count = max(count, len(value))

        return cls(
            total_value=declared_value,
            value_verified=False,
            record_count=count,
            org_units=_count_org_units(arguments),
            data_class=data_class,
            period_sensitive=profile.period_sensitive,
        )

    def verified_with(
        self, *, total_value: float, currency: str = "", record_count: int | None = None
    ) -> ImpactSignals:
        """SAP fiyatlandirmasi sonrasi dogrulanmis etki sinyali uretir."""
        return ImpactSignals(
            total_value=max(float(total_value), self.total_value),
            currency=currency or self.currency,
            value_verified=True,
            record_count=record_count if record_count is not None else self.record_count,
            org_units=self.org_units,
            data_class=self.data_class,
            period_sensitive=self.period_sensitive,
            external_commitment=self.external_commitment,
            privileged_master_data=self.privileged_master_data,
            notes=self.notes,
        )


@dataclass(frozen=True)
class ImpactAssessment:
    """Boyutlariyla birlikte aciklanabilir risk karari.

    Skor tek basina yeterli degildir: audit'te hangi boyutun kac puan
    getirdigi gorulmelidir, aksi halde karar itiraz edilemez olur.
    """

    score: int
    runtime_tier: RiskTier
    declared_tier: RiskTier
    effective_tier: RiskTier
    dimensions: tuple[tuple[str, int], ...] = ()
    reasons: tuple[str, ...] = ()
    value_verified: bool = False
    data_class: DataClass = DataClass.D1

    @property
    def escalated(self) -> bool:
        return self.effective_tier.level > self.declared_tier.level

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "runtime_tier": self.runtime_tier.value,
            "declared_tier": self.declared_tier.value,
            "effective_tier": self.effective_tier.value,
            "escalated": self.escalated,
            "value_verified": self.value_verified,
            "data_class": self.data_class.value,
            "dimensions": {name: points for name, points in self.dimensions},
            "reasons": list(self.reasons),
        }


def score_impact(
    profile: ImpactProfile,
    signals: ImpactSignals,
    *,
    declared_tier: RiskTier,
) -> ImpactAssessment:
    """Alti boyutu toplar ve `effective_tier` uretir."""
    dimensions: list[tuple[str, int]] = []
    reasons: list[str] = []

    # 1. Eylem etkisi (0-60)
    action = profile.mutation.action_points
    dimensions.append(("action", action))
    if action:
        reasons.append(f"Eylem tipi '{profile.mutation.value}' ({action} puan).")

    # 2. Finansal maruziyet (0-20)
    financial = _value_points(signals.total_value)
    if financial:
        dimensions.append(("financial", financial))
        source = "SAP'te dogrulanan" if signals.value_verified else "beyan edilen (dogrulanmamis)"
        reasons.append(
            f"{source} tutar {signals.total_value:,.2f} {signals.currency or ''}".rstrip()
            + f" ({financial} puan)."
        )
        if not signals.value_verified:
            reasons.append(
                "Tutar SAP'ten dogrulanmadi; skor yalniz yukari yonlu kullanildi."
            )
    else:
        dimensions.append(("financial", 0))

    # 3. Kayit ve organizasyon genisligi (0-10)
    breadth = min(10, _record_points(signals.record_count) + max(0, signals.org_units - 1) * 2)
    dimensions.append(("breadth", breadth))
    if breadth:
        reasons.append(
            f"{signals.record_count} kayit / {max(1, signals.org_units)} organizasyon birimi "
            f"({breadth} puan)."
        )

    # 4. Geri alinabilirlik (0-15)
    reversibility = profile.reversible.points
    dimensions.append(("reversibility", reversibility))
    if reversibility:
        reasons.append(
            f"Geri alinabilirlik '{profile.reversible.value}' ({reversibility} puan)."
        )

    # 5. Harici taahhut / donem etkisi (0-10)
    external = bool(
        signals.external_commitment
        if signals.external_commitment is not None
        else profile.external_commitment
    )
    commitment = (6 if external else 0) + (4 if signals.period_sensitive else 0)
    dimensions.append(("commitment", min(10, commitment)))
    if external:
        reasons.append("Islem ucuncu tarafa taahhut uretiyor (6 puan).")
    if signals.period_sensitive:
        reasons.append("Kapanis donemi/yasal belge etkisi (4 puan).")

    # 6. Ayricalikli ana veri (0-10)
    privileged = bool(
        signals.privileged_master_data
        if signals.privileged_master_data is not None
        else profile.privileged_master_data
    )
    master = 10 if privileged else 0
    dimensions.append(("master_data", master))
    if privileged:
        reasons.append("Ayricalikli ana veri (tedarikci/banka/yetki) etkisi (10 puan).")

    score = min(100, sum(points for _, points in dimensions))
    runtime_tier = tier_for_score(score)

    # Bulk ve geri donussuz islemler en az R4'tur.
    forces_r4 = (
        profile.mutation.forces_max_tier or profile.reversible is Reversibility.IRREVERSIBLE
    )
    if forces_r4 and runtime_tier.level < RiskTier.R4.level:
        runtime_tier = RiskTier.R4
        reasons.append("Bulk/geri donussuz islem taban kurali geregi R4'e yukseltildi.")

    effective = declared_tier if declared_tier.level >= runtime_tier.level else runtime_tier
    if effective.level > declared_tier.level:
        reasons.append(
            f"Bildirilen {declared_tier.value} runtime degerlendirmesiyle "
            f"{effective.value} seviyesine yukseltildi."
        )

    return ImpactAssessment(
        score=score,
        runtime_tier=runtime_tier,
        declared_tier=declared_tier,
        effective_tier=effective,
        dimensions=tuple(dimensions),
        reasons=tuple(reasons),
        value_verified=signals.value_verified,
        data_class=signals.data_class,
    )


# --- Yardimcilar -----------------------------------------------------------
def _value_points(value: float) -> int:
    if value <= 0:
        return 0
    for ceiling, points in _VALUE_BANDS:
        if value <= ceiling:
            return points
    return 20


def _record_points(count: int) -> int:
    if count <= 0:
        return 0
    for ceiling, points in _RECORD_BANDS:
        if count <= ceiling:
            return points
    return 10


_ORG_KEYS = ("plant", "plants", "company_code", "company_codes", "purchasing_org")


def _count_org_units(arguments: Mapping[str, Any], *, depth: int = 0) -> int:
    """Argumanlarda gecen farkli organizasyon degeri sayisi.

    Policy zaten her degeri yetki alanina karsi denetler; buradaki sayim
    **genislik** olcusudur: dort tesise ayni anda dokunan bir islem tek
    tesise dokunandan risklidir.
    """
    if depth > 6:
        return 0
    found: set[str] = set()

    def _walk(node: Any, level: int) -> None:
        if level > 6:
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key in _ORG_KEYS:
                    items = value if isinstance(value, list | tuple | set) else [value]
                    found.update(str(i) for i in items if i not in (None, ""))
                else:
                    _walk(value, level + 1)
        elif isinstance(node, list | tuple):
            for item in node:
                _walk(item, level + 1)

    _walk(arguments, depth)
    return len(found)


@dataclass
class RiskObligations:
    """Etki skoru ve veri sinifinin urettigi ek yukumlulukler.

    Veri sinifi operasyon riskine karistirilmaz: salt okunur bir R0
    tool D3 veri okuyabilir; bu durumda yazma onayi gerekmez ama maskeleme ve
    export kontrolleri zorunlu olur.
    """

    dual_control: bool = False
    export_blocked: bool = False
    masking_required: bool = False
    justification_required: bool = False
    notes: list[str] = field(default_factory=list)

    @classmethod
    def derive(cls, assessment: ImpactAssessment) -> RiskObligations:
        obligations = cls()
        if assessment.effective_tier is RiskTier.R4:
            obligations.dual_control = True
            obligations.notes.append("R4: iki ayri onaylayan zorunlu.")
        if assessment.data_class.level >= DataClass.D2.level:
            obligations.masking_required = True
        if assessment.data_class is DataClass.D3:
            obligations.export_blocked = True
            obligations.notes.append(
                "D3 veri: export ayri kapsam ve iki kisili kontrol ister."
            )
        if assessment.escalated:
            obligations.justification_required = True
            obligations.notes.append(
                "Risk runtime'da yukseltildi; gerekce audit kaydinda tutulur."
            )
        return obligations

    def to_dict(self) -> dict[str, Any]:
        return {
            "dual_control": self.dual_control,
            "export_blocked": self.export_blocked,
            "masking_required": self.masking_required,
            "justification_required": self.justification_required,
            "notes": list(self.notes),
        }


# Geriye donuk kolaylik: `MutationKind`/`Reversibility` bu modulden de gorunur.
__all__ += ["MutationKind", "Reversibility", "RiskObligations"]
