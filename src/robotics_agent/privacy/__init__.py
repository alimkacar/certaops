"""Model-oncesi veri gizliligi katmani.

Dort sorumluluk, dort modul:

    classification   D0-D3 seviyeleri, SAP alan envanteri, tool `DataPolicy`si
    field_policy     "kim, hangi alani, nereye, hangi amacla gorebilir"
    dlp              tek merkezi motor: allow/mask/tokenize/drop/deny
    pseudonymization tenant'a ozgu, geri cozulemez HMAC takma kimlik
    retention        saklama tablosu ve periyodik purge job

Tasarim ilkesi: veri politikasi **LLM cagrisindan once** uygulanir.
Modelin "bu alani gormezden gel" talimatiyla degil, kodla zorlanir.
"""

from .classification import (
    FIELD_CLASS_INVENTORY,
    DataClass,
    DataPolicy,
    classify_field,
    max_class,
    walk_fields,
)
from .dlp import DLPEngine, DLPFinding, DLPResult, build_dlp_engine
from .field_policy import (
    HANDOFF_FIELD_ALLOWLIST,
    PURPOSE_CODES,
    FieldAccessPolicy,
    PrivacyAction,
    Sink,
    handoff_allowlist,
)
from .pseudonymization import Pseudonymizer, get_pseudonymizer, reset_pseudonymizer_cache
from .retention import RETENTION_POLICY, PurgeReport, RetentionRule, RetentionSweeper

__all__ = [
    "FIELD_CLASS_INVENTORY",
    "HANDOFF_FIELD_ALLOWLIST",
    "PURPOSE_CODES",
    "RETENTION_POLICY",
    "DLPEngine",
    "DLPFinding",
    "DLPResult",
    "DataClass",
    "DataPolicy",
    "FieldAccessPolicy",
    "PrivacyAction",
    "Pseudonymizer",
    "PurgeReport",
    "RetentionRule",
    "RetentionSweeper",
    "Sink",
    "build_dlp_engine",
    "classify_field",
    "get_pseudonymizer",
    "handoff_allowlist",
    "max_class",
    "reset_pseudonymizer_cache",
    "walk_fields",
]
