"""Calisma zamani platform yapilandirmasi.

Arayuzden degistirilebilen ayarlarin izin listesi, diskteki deposu ve
degisikligin gectigi kapi. Kimlik dogrulama, SAP kimlik bilgileri ve egress
allowlist bilerek bu paketin disinda: bunlar deployment kararidir ve bir
tarayici ucundan degistirilemez.
"""
from .registry import SECTION_LABELS, SETTABLE, SettingError, SettingSpec, coerce, spec_for
from .service import ChangeOutcome, ConfigRefused, ConfigService
from .store import (
    apply_overrides,
    overrides_path,
    pinned_by_environment,
    read_overrides,
    snapshot_process_env,
    write_overrides,
)

__all__ = [
    "SETTABLE",
    "SECTION_LABELS",
    "SettingError",
    "SettingSpec",
    "coerce",
    "spec_for",
    "ChangeOutcome",
    "ConfigRefused",
    "ConfigService",
    "apply_overrides",
    "overrides_path",
    "pinned_by_environment",
    "read_overrides",
    "snapshot_process_env",
    "write_overrides",
]
