"""Public CertaOps API.

Yeni kod ``certaops.providers`` ve ``certaops.runtime`` altindadir. Eski
``robotics_agent`` namespace'i geriye donuk uyumluluk icin calismaya devam eder.

Import'lar bilerek TEMBEL: ``certaops.runtime`` modulu ``robotics_agent``i
import eder; bu modulun tepesinde de ayni import olsaydi dairesel bir
baglanti olusurdu.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

__all__ = [
    "SAPAgentRuntime",
    "SAPMultiAgent",
    "Settings",
    "build_provider",
    "get_settings",
    "setup_logging",
    "__version__",
]

_LAZY = {
    "Settings": ("robotics_agent.config", "Settings"),
    "get_settings": ("robotics_agent.config", "get_settings"),
    "setup_logging": ("robotics_agent.config", "setup_logging"),
    "SAPAgentRuntime": ("certaops.runtime", "SAPAgentRuntime"),
    "SAPMultiAgent": ("robotics_agent.compat_agent", "SAPMultiAgent"),
    "build_provider": ("certaops.providers", "build_provider"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'certaops' has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(target[0]), target[1])


def __dir__() -> list[str]:
    return sorted(__all__)
