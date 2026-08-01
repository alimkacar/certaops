"""Cagri aninda dogrulanan sinyallerle dinamik risk modeli.

Tek bir risk etiketi yerine uc ayri eksen:

    operation_tier       Tool'un statik R0-R4 tabani (`ToolSpec.risk_tier`).
    data_classification  D0-D3 veri hassasiyeti (`privacy` paketi).
    impact_score         Cagri aninda hesaplanan 0-100 etki skoru (bu paket).

Veri gizliligi risk seviyesine karistirilmaz: salt okunur bir R0 tool D3 veri
okuyabilir. O durumda yazma onayi gerekmez ama maskeleme ve export kontrolleri
zorunlu olur.
"""

from .impact import COMPUTE_ONLY, READ_ONLY, ImpactProfile, MutationKind, Reversibility
from .scoring import (
    SCORE_BANDS,
    ImpactAssessment,
    ImpactSignals,
    RiskObligations,
    score_impact,
    tier_for_score,
)

__all__ = [
    "COMPUTE_ONLY",
    "READ_ONLY",
    "SCORE_BANDS",
    "ImpactAssessment",
    "ImpactProfile",
    "ImpactSignals",
    "MutationKind",
    "Reversibility",
    "RiskObligations",
    "score_impact",
    "tier_for_score",
]
