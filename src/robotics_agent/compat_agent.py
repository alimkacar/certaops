"""Geriye donuk uyumluluk facade'i.

.. deprecated:: 0.2.0
   Bu modul yeni ``certaops.runtime.SAPAgentRuntime``e yonlendirir.

Neden degisti
-------------
Onceki tasarimda her SAP domaini ayri bir ``SAPDomainAgent`` ornegiydi:
kendi model istemcisi, kendi konusma gecmisi, kendi LLM cagrisi. Cok
domainli bir istek uc ayri model turu calistirip sonuclari model DISINDA
birlestiriyordu. Bu, ayni isi yapmak icin N kat model maliyeti ve agent'lar
arasi ``HandoffEnvelope`` yolu seklinde ayri bir guvenlik yuzeyi demekti.

Domain ayrimi kaybolmadi: ``certaops.runtime.profiles`` icinde prompt
parcasi, tool pack'i, iterasyon butcesi ve erisim kapsami olarak yasiyor.
Tek runtime yetkili pack'lerin birlesimini hesaplar ve **tek** model
dongusu calistirir.

Gecis
-----
Eski::

    from robotics_agent.compat_agent import SAPMultiAgent
    agent = SAPMultiAgent()

Yeni::

    from certaops.runtime import SAPAgentRuntime
    runtime = SAPAgentRuntime()

Iki sinif da ayni ``chat(...) -> AgentTurn`` sozlesmesini sunar.
"""

from __future__ import annotations

import logging
import warnings
from typing import Any

from certaops.runtime import AgentTurn, SAPAgentRuntime, ToolCall

log = logging.getLogger(__name__)

__all__ = ["AgentTurn", "SAPAgentRuntime", "SAPDomainAgent", "SAPMultiAgent", "ToolCall"]

_DEPRECATION = (
    "{name} artik tek SAPAgentRuntime'a yonlendiren bir facade'dir. "
    "Yeni kod `from certaops.runtime import SAPAgentRuntime` kullanmalidir."
)


class SAPMultiAgent(SAPAgentRuntime):
    """.. deprecated:: 0.2.0 ``certaops.runtime.SAPAgentRuntime`` kullanin.

    Ayri calisan agent'lar artik yok; bu sinif tek runtime'in ustunde ince
    bir kabuktur. Eski alan adlari (``active_agents``, ``agent_trace``)
    ``AgentTurn`` uzerinde ozellik olarak korunur.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            _DEPRECATION.format(name="SAPMultiAgent"),
            DeprecationWarning,
            stacklevel=2,
        )
        # Eski imzada bulunan, artik saglayici adaptorune ait olan
        # parametreler sessizce yutulur ki mevcut cagirici kirilmasin.
        for legacy in ("stream", "use_prompt_cache", "two_stage_routing", "agent_spec"):
            kwargs.pop(legacy, None)
        super().__init__(*args, **kwargs)

    @property
    def active_agents(self) -> list[str]:
        """Deprecated: ayri agent'lar yok; acik domainleri dondurur."""
        from certaops.runtime.profiles import profiles_for_packs

        return [p.key for p in profiles_for_packs(self.active_packs)]


class SAPDomainAgent(SAPAgentRuntime):
    """.. deprecated:: 0.2.0 ``certaops.runtime.SAPAgentRuntime`` kullanin.

    Eskiden tek bir domaine kilitlenmis agent'ti. Artik domain secimi
    deterministik router'a aittir; bu sinif yalnizca eski cagiricilarin
    calismaya devam etmesi icin vardir.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        warnings.warn(
            _DEPRECATION.format(name="SAPDomainAgent"),
            DeprecationWarning,
            stacklevel=2,
        )
        for legacy in ("stream", "use_prompt_cache", "two_stage_routing", "agent_spec"):
            kwargs.pop(legacy, None)
        super().__init__(*args, **kwargs)
