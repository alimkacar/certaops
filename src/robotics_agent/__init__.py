"""Internal runtime for CertaOps SAP agent tools.

Public nesneler tembel yuklenir. ``certaops.runtime`` kendi cekirdek
bilesenleri icin ``robotics_agent.config``i import eder; paket acilisinda
compatibility facade'ini eager import etmek iki namespace arasinda dongu
olusturur.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

__all__ = [
    "SAPAgentRuntime",
    "SAPDomainAgent",
    "SAPMultiAgent",
    "Settings",
    "get_settings",
    "setup_logging",
    "__version__",
]

_LAZY = {
    "SAPAgentRuntime": ("certaops.runtime", "SAPAgentRuntime"),
    "SAPDomainAgent": ("robotics_agent.compat_agent", "SAPDomainAgent"),
    "SAPMultiAgent": ("robotics_agent.compat_agent", "SAPMultiAgent"),
    "Settings": ("robotics_agent.config", "Settings"),
    "get_settings": ("robotics_agent.config", "get_settings"),
    "setup_logging": ("robotics_agent.config", "setup_logging"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'robotics_agent' has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(target[0]), target[1])


def __dir__() -> list[str]:
    return sorted(__all__)
