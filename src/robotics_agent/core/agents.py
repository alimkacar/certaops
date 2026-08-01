"""SAP domain agent katalogu, planlama ve yapilandirilmis handoff sozlesmesi.

Orkestrator LLM'e guvenerek yetki genisletmez. Kullanici istegi once kural
tabanli router ile SAP domainlerine ayrilir; her domain agent yalniz kendi pack
ve tool'larini gorur. Agent'lar arasinda tam konusma gecmisi degil, bu moduldaki
sinirli ve denetlenebilir ``HandoffEnvelope`` tasinir.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..contracts import ActorContext
from .router import BOOTSTRAP_PACK, RoutingDecision, route


@dataclass(frozen=True)
class AgentSpec:
    """Bir SAP domain agent'inin sabit yetki ve tool siniri."""

    key: str
    title: str
    packs: tuple[str, ...]
    mission: str
    handoff_targets: tuple[str, ...] = ()


AGENT_SPECS: dict[str, AgentSpec] = {
    "platform": AgentSpec(
        key="platform",
        title="SAP Platform ve Teshis Agent'i",
        packs=(BOOTSTRAP_PACK, "diagnostics"),
        mission=(
            "SAP baglanti/yetenek kesfi, yetki hatasi teshisi, audit, evidence ve "
            "timeout sonrasi mutabakat islemlerini yurutur. Is verisi hakkinda tahmin yapmaz."
        ),
        handoff_targets=("master_data", "supply_chain", "procurement", "finance"),
    ),
    "master_data": AgentSpec(
        key="master_data",
        title="SAP Ana Veri Agent'i",
        packs=(BOOTSTRAP_PACK, "master_data"),
        mission=(
            "Malzeme arama, urun/tesis gorunumu, siniflandirma, degerleme ve ana veri "
            "kalitesi sorularini SAP kaynaklariyla cevaplar."
        ),
        handoff_targets=("supply_chain", "procurement", "finance", "platform"),
    ),
    "supply_chain": AgentSpec(
        key="supply_chain",
        title="SAP Planlama ve Tedarik Zinciri Agent'i",
        packs=(BOOTSTRAP_PACK, "procurement_read", "p2p_visibility"),
        mission=(
            "Stok, ATP, MRP arz/talep, tedarikci performansi, TCO, acik siparis takibi ve "
            "PR-PO-mal kabul-fatura belge zincirini salt okunur SAP verisiyle yurutur."
        ),
        handoff_targets=("procurement", "master_data", "finance", "platform"),
    ),
    "procurement": AgentSpec(
        key="procurement",
        title="SAP Satinalma Agent'i",
        packs=(BOOTSTRAP_PACK, "procurement_write", "procurement_read", "p2p_approval"),
        mission=(
            "Satinalma talebini hazirlar, deterministik diff ve dogrulama uretir, onay "
            "is akisinin nerede bekledigini gosterir; yalniz policy/onay/idempotency "
            "protokolu izin verirse SAP'a yazar."
        ),
        handoff_targets=("supply_chain", "master_data", "finance", "platform"),
    ),
    "finance": AgentSpec(
        key="finance",
        title="SAP Proje Finans ve Raporlama Agent'i",
        packs=(BOOTSTRAP_PACK, "project_finance", "p2p_finance", "reporting"),
        mission=(
            "SAP WBS plan/fiili/taahhut verisini, EAC/ETC durumunu, tedarikci faturasi "
            "odeme/blokaj durumunu ve SAP kaynakli yonetim raporlarini uretir. SAP disi "
            "BOM veya muhendislik tahmini yapmaz."
        ),
        handoff_targets=("procurement", "supply_chain", "platform"),
    ),
}


PACK_TO_AGENT: dict[str, str] = {
    "diagnostics": "platform",
    "master_data": "master_data",
    "procurement_read": "supply_chain",
    "procurement_write": "procurement",
    "p2p_visibility": "supply_chain",
    "p2p_approval": "procurement",
    "p2p_finance": "finance",
    "project_finance": "finance",
    "reporting": "finance",
}


@dataclass(frozen=True)
class OrchestrationPlan:
    """Bir kullanici turunda calisacak sirali domain agent listesi."""

    agents: tuple[str, ...]
    routing: RoutingDecision
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": list(self.agents),
            "reason": self.reason,
            "routing": self.routing.to_dict(),
        }


def plan_agents(message: str, actor: ActorContext, *, max_agents: int = 3) -> OrchestrationPlan:
    """Istek icin en dar yetkili SAP agent zincirini sec."""

    routing = route(message, actor, max_packs=max_agents)
    if routing.fallback:
        return OrchestrationPlan(
            agents=("platform",),
            routing=routing,
            reason="Belirgin SAP domaini bulunamadi; platform agent'i niyeti netlestirir.",
        )

    selected: list[str] = []
    for pack in routing.packs:
        key = PACK_TO_AGENT.get(pack)
        if key and key not in selected:
            selected.append(key)

    if "procurement" in selected and "supply_chain" in selected:
        selected.remove("supply_chain")
    if not selected:
        selected = ["platform"]

    return OrchestrationPlan(
        agents=tuple(selected[:max_agents]),
        routing=routing,
        reason="Kural tabanli SAP domain eslesmesi ve actor yetkileri.",
    )


def agent_catalogue() -> list[dict[str, Any]]:
    return [
        {
            "agent": spec.key,
            "title": spec.title,
            "packs": list(spec.packs),
            "mission": spec.mission,
            "handoff_targets": list(spec.handoff_targets),
        }
        for spec in AGENT_SPECS.values()
    ]


@dataclass(frozen=True)
class HandoffEnvelope:
    """Agent'lar arasinda tasinabilen sinirli, yapilandirilmis kanit ozeti."""

    from_agent: str
    to_agent: str
    objective: str
    correlation_id: str
    evidence_ids: tuple[str, ...] = ()
    business_objects: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    needs_review: bool = False
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "sap-agent-handoff/v1",
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "objective": self.objective[:500],
            "correlation_id": self.correlation_id,
            "evidence_ids": list(self.evidence_ids),
            "business_objects": list(self.business_objects),
            "warnings": list(self.warnings),
            "needs_review": self.needs_review,
            "summary": self.summary[:1200],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


def handoff_from_turn(
    *,
    from_agent: str,
    to_agent: str,
    objective: str,
    correlation_id: str,
    text: str,
    tool_calls: list[Any],
    needs_review: bool,
) -> HandoffEnvelope:
    """Tool sonuclarindan yalniz korunan kimlikleri handoff'a cikar."""

    evidence: set[str] = set()
    objects: set[str] = set()
    warnings: list[str] = []
    for call in tool_calls:
        try:
            payload = json.loads(call.result)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        meta = payload.get("_meta") or {}
        evidence_id = meta.get("evidence_id") or payload.get("evidence_id")
        if evidence_id:
            evidence.add(str(evidence_id))
        for key in ("business_object_id", "requisition_id", "purchase_order"):
            if payload.get(key):
                objects.add(str(payload[key]))
        for warning in payload.get("warnings", []) or []:
            if len(warnings) < 10:
                warnings.append(str(warning)[:240])

    return HandoffEnvelope(
        from_agent=from_agent,
        to_agent=to_agent,
        objective=objective,
        correlation_id=correlation_id,
        evidence_ids=tuple(sorted(evidence)),
        business_objects=tuple(sorted(objects)),
        warnings=tuple(warnings),
        needs_review=needs_review,
        summary=text[:1200],
    )
