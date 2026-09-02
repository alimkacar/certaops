"""Tek runtime icin SAP domain profili ve geriye uyumluluk metadata'si.

Gercek yurutme :mod:`robotics_agent.compat_agent` icindeki tek runtime'da ve router'in
sectigi exact tool-pack birlesimiyle yapilir. ``AgentSpec``, ``plan_agents`` ve
``HandoffEnvelope`` eski istemcilerin veri sozlesmesini kirmamak icin korunur;
ayri model istemcisi, history veya runtime handoff'u olusturmazlar.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..contracts import ActorContext
from .router import BOOTSTRAP_PACK, PACKS, RoutingDecision, route


@dataclass(frozen=True)
class AgentSpec:
    """Legacy adiyla tutulan mantiksal SAP domain profili."""

    key: str
    title: str
    packs: tuple[str, ...]
    mission: str
    handoff_targets: tuple[str, ...] = ()


AGENT_SPECS: dict[str, AgentSpec] = {
    "platform": AgentSpec(
        key="platform",
        title="SAP Platform ve Teshis Profili",
        packs=(BOOTSTRAP_PACK, "diagnostics"),
        mission=(
            "SAP baglanti/yetenek kesfi, yetki hatasi teshisi, audit, evidence ve "
            "timeout sonrasi mutabakat islemlerini yurutur. Is verisi hakkinda tahmin yapmaz."
        ),
        handoff_targets=("master_data", "supply_chain", "procurement", "finance"),
    ),
    "master_data": AgentSpec(
        key="master_data",
        title="SAP Ana Veri Profili",
        packs=(BOOTSTRAP_PACK, "master_data"),
        mission=(
            "Malzeme arama, urun/tesis gorunumu, siniflandirma, degerleme ve ana veri "
            "kalitesi sorularini SAP kaynaklariyla cevaplar."
        ),
        handoff_targets=("supply_chain", "procurement", "finance", "platform"),
    ),
    "supply_chain": AgentSpec(
        key="supply_chain",
        title="SAP Planlama ve Tedarik Zinciri Profili",
        packs=(BOOTSTRAP_PACK, "procurement_read", "p2p_visibility"),
        mission=(
            "Stok, MRP arz/talep, tedarikci performansi, TCO, acik siparis takibi ve "
            "PR-PO-mal kabul-fatura belge zincirini salt okunur SAP verisiyle yurutur."
        ),
        handoff_targets=("procurement", "master_data", "finance", "platform"),
    ),
    "procurement": AgentSpec(
        key="procurement",
        title="SAP Satinalma Profili",
        packs=(BOOTSTRAP_PACK, "procurement_write", "procurement_read"),
        mission=(
            "Satinalma talebi taslagi hazirlar, deterministik diff ve dogrulama uretir. "
            "Mevcut read-only surum SAP'a yazmaz; gelecekteki write protokolu yalniz "
            "policy/onay/idempotency kapilariyla acilabilir."
        ),
        handoff_targets=("supply_chain", "master_data", "finance", "platform"),
    ),
    "finance": AgentSpec(
        key="finance",
        title="SAP Finans ve Raporlama Profili",
        packs=(BOOTSTRAP_PACK, "p2p_finance", "reporting"),
        mission=(
            "Tedarikci faturasi odeme/blokaj durumunu ve SAP kaynakli yonetim "
            "raporlarini uretir. SAP disi BOM, proje maliyeti veya muhendislik "
            "tahmini yapmaz."
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
    "p2p_finance": "finance",
    "reporting": "finance",
}


def subsumed_packs(packs: tuple[str, ...] | list[str]) -> set[str]:
    """Baska bir SECILI pack tarafindan zaten icerilen pack'ler.

    `procurement_write` ornegin `procurement_read`i icerir. Yonlendirmenin
    OZETINI sunan yuzeyler bunlari ayri bir domain gibi gostermemelidir;
    kullanici iki profil almadi, yazma profilini ve onun bagimliligini aldi.
    `plan_agents` ise legacy sozlesmedir ve acilan tum profilleri listeler.
    """
    out: set[str] = set()
    for pack in packs:
        definition = PACKS.get(pack)
        if definition is None:
            continue
        out.update(i for i in getattr(definition, "includes", ()) if i != pack)
    return out


@dataclass(frozen=True)
class OrchestrationPlan:
    """Legacy istemciler icin secilen domain profili gorunumu."""

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
    """Istek icin mantiksal profil metadata'si uret; yurutme yapmaz."""

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

    if not selected:
        selected = ["platform"]

    return OrchestrationPlan(
        agents=tuple(selected[:max_agents]),
        routing=routing,
        reason="Kural tabanli SAP domain eslesmesi ve actor yetkileri.",
    )


def profiles_for_packs(pack_keys: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Pack listesini calisan process degil, mantiksal domain profillerine cevir.

    Eski ``plan_agents`` bir yazma profili secildiginde supply-chain agent'ini
    zincirden cikariyordu. Tek runtime'da buna gerek yoktur: router'in actigi
    hicbir pack metadata donusumunde kaybolmamalidir.
    """
    selected: list[str] = []
    for pack in pack_keys:
        key = PACK_TO_AGENT.get(pack)
        if key and key not in selected:
            selected.append(key)
    return tuple(selected or ("platform",))


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
    """Yalniz eski entegrasyonlar icin sinirli kanit zarfi.

    Tek-runtime sohbet yolunda kullanilmaz veya provider state'i tasimaz.
    """

    from_agent: str
    to_agent: str
    objective: str
    correlation_id: str
    evidence_ids: tuple[str, ...] = ()
    business_objects: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    needs_review: bool = False
    summary: str = ""

    #: Zarf ust verisi: allowlist'ten bagimsiz olarak her zaman tasinir.
    #: Bunlar is verisi degil, yonlendirme bilgisidir.
    _ENVELOPE_KEYS = ("schema", "from_agent", "to_agent")

    #: Serbest metin alanlari. Allowlist'ten gecseler bile DLP'den de gecerler:
    #: allowlist "hangi alan", DLP "o alanin icinde ne var" sorusunu cevaplar.
    _TEXT_KEYS = ("objective", "summary")

    def to_dict(self, *, dlp: Any = None, actor: Any = None) -> dict[str, Any]:
        """Zarfi sozluge cevirir; **alan allowlist'i ve DLP burada uygulanir**.

        Iki ayri kapi vardir ve ikisi de gereklidir:

          1. `handoff_allowlist(from_agent, to_agent)` - bu agent cifti icin
             hangi ALANLARIN tasinabilecegi. Tanimsiz cift icin daraltilmis
             taban set doner (fail-closed): is nesnesi kimlikleri bile gecmez.
          2. DLP `sink="handoff"` - gecen alanlarin ICINDE ne oldugu. Serbest
             metin (model ozeti) her turlu hassas degeri tasiyabilir; D2
             maskelenir, D3 dusurulur.

        `dlp`/`actor` verilmezse yalniz allowlist uygulanir. Motor olmadan
        cagirmak gecerlidir (test, serilestirme) ama uretim yolunda ikisi de
        verilmelidir.
        """
        from ..privacy import handoff_allowlist

        allowed = handoff_allowlist(self.from_agent, self.to_agent)
        candidate = {
            "objective": self.objective[:500],
            "correlation_id": self.correlation_id,
            "evidence_ids": list(self.evidence_ids),
            "business_objects": list(self.business_objects),
            "warnings": list(self.warnings),
            "needs_review": self.needs_review,
            "summary": self.summary[:1200],
        }
        payload: dict[str, Any] = {
            "schema": "sap-agent-handoff/v1",
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
        }
        payload.update({k: v for k, v in candidate.items() if k in allowed})

        dropped = sorted(k for k in candidate if k not in allowed)
        if dropped:
            # Sessizce dusurmek, alan gitti mi yoksa hic uretilmedi mi
            # sorusunu cevapsiz birakirdi.
            payload["dropped_fields"] = dropped

        if dlp is None or actor is None:
            return payload

        from ..privacy import sanitize_text

        for key in self._TEXT_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                payload[key] = sanitize_text(value, actor=actor, sink="handoff", dlp=dlp)
        if isinstance(payload.get("warnings"), list):
            payload["warnings"] = [
                sanitize_text(str(w), actor=actor, sink="handoff", dlp=dlp)
                for w in payload["warnings"]
            ]
        return payload

    def to_json(self, *, dlp: Any = None, actor: Any = None) -> str:
        return json.dumps(
            self.to_dict(dlp=dlp, actor=actor), ensure_ascii=False, separators=(",", ":")
        )


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
