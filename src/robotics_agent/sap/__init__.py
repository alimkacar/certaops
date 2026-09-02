"""SAP entegrasyon katmani."""

from __future__ import annotations

import logging

from ..config import Settings, get_settings
from . import schema_cache
from .base import SAPBackend, SAPError

log = logging.getLogger(__name__)

_backend: SAPBackend | None = None


def build_backend(settings: Settings | None = None) -> SAPBackend:
    """SAP_BACKEND ayarina gore uygun backend'i uretir."""
    settings = settings or get_settings()
    kind = settings.sap.backend

    if kind == "odata":
        from .odata import ODataSAPBackend

        log.info("SAP backend: OData (%s, client %s)", settings.sap.base_url, settings.sap.client)
        return ODataSAPBackend(settings)

    if kind == "ecc":
        # ECC 6.0 EHP8: embedded SAP_GWFND 7.50 uzerinde Z-Gateway OData V2.
        # Portlar `odata` ile birebir aynidir; degisen yalniz servis manifesti
        # ve sorgu desenleridir. Tool/policy/audit/privacy katmanlari etkilenmez.
        from .ecc import ECCSAPBackend

        log.info("SAP backend: ECC (%s, client %s)", settings.sap.base_url, settings.sap.client)
        return ECCSAPBackend(settings)

    from .mock import MockSAPBackend

    log.info("SAP backend: mock (yerel veri seti)")
    return MockSAPBackend(settings)


def get_backend(settings: Settings | None = None) -> SAPBackend:
    """Surec genelinde tek backend ornegi."""
    global _backend
    if _backend is None:
        _backend = build_backend(settings)
    return _backend


def reset_backend() -> None:
    global _backend
    if _backend is not None:
        _backend.close()
    _backend = None
    # Sema onbellegi de dusurulur: baglanti ya da ayar degistiginde eski
    # sistemin `$metadata`si ve V4/V2 karari tasinmamali.
    schema_cache.clear()


__all__ = [
    "SAPBackend",
    "SAPError",
    "build_backend",
    "get_backend",
    "reset_backend",
    "schema_cache",
]
