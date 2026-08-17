"""Domain profilleri: ayri agent nesneleri yerine metadata.

Eskiden her SAP domaini kendi ``SAPDomainAgent`` ornegiydi: kendi model
istemcisi, kendi konusma gecmisi, kendi LLM cagrisi. Cok domainli bir istek
uc ayri model turu demekti ve sonuclar model disinda birlestiriliyordu.

Domain ayrimi degerlidir - ama bir **calisma zamani nesnesi** olmasi
gerekmiyor. Bir domainin gercekte tasidigi sey dorttur:

    1. system prompt parcasi (gorev tanimi ve domaine ozgu kurallar)
    2. tool pack'leri (hangi tool'lar gorunur)
    3. iterasyon butcesi (kac tool adimina izin var)
    4. erisim kapsamlari (pack'in actor yetkisiyle acilip acilmayacagi)

Dordu de veridir. Bu modul onlari veri olarak tutar; tek bir runtime bu
veriyi okuyup **tek** model dongusu calistirir.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from robotics_agent.core.router import BOOTSTRAP_PACK

__all__ = [
    "DOMAIN_PROFILES",
    "DomainProfile",
    "iteration_budget_for",
    "profile_catalogue",
    "profiles_for_packs",
]


@dataclass(frozen=True)
class DomainProfile:
    """Bir SAP domaininin prompt/pack/butce metadata'si."""

    key: str
    title: str
    packs: tuple[str, ...]
    mission: str
    #: Bu domain acikken izin verilen azami tool adimi.
    iteration_budget: int = 6

    @property
    def domain_packs(self) -> tuple[str, ...]:
        """Bootstrap disindaki pack'ler (domaini fiilen tanimlayanlar)."""
        return tuple(p for p in self.packs if p != BOOTSTRAP_PACK)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.key,
            "title": self.title,
            "packs": list(self.packs),
            "mission": self.mission,
            "iteration_budget": self.iteration_budget,
        }


DOMAIN_PROFILES: dict[str, DomainProfile] = {
    "platform": DomainProfile(
        key="platform",
        title="SAP Platform ve Teshis",
        packs=(BOOTSTRAP_PACK, "diagnostics"),
        mission=(
            "SAP baglanti/yetenek kesfi, yetki hatasi teshisi, audit, evidence ve "
            "timeout sonrasi mutabakat. Is verisi hakkinda tahmin yapma."
        ),
        iteration_budget=4,
    ),
    "master_data": DomainProfile(
        key="master_data",
        title="SAP Ana Veri",
        packs=(BOOTSTRAP_PACK, "master_data"),
        mission=(
            "Malzeme arama, urun/tesis gorunumu, siniflandirma, degerleme ve ana "
            "veri kalitesi sorularini SAP kaynaklariyla cevapla."
        ),
        iteration_budget=5,
    ),
    "supply_chain": DomainProfile(
        key="supply_chain",
        title="SAP Planlama ve Tedarik Zinciri",
        packs=(BOOTSTRAP_PACK, "procurement_read", "p2p_visibility"),
        mission=(
            "Stok, ATP, MRP arz/talep, tedarikci performansi, TCO, acik siparis "
            "takibi ve PR-PO-mal kabul-fatura belge zinciri. Salt okunur."
        ),
        iteration_budget=7,
    ),
    "procurement": DomainProfile(
        key="procurement",
        title="SAP Satinalma",
        packs=(BOOTSTRAP_PACK, "procurement_write", "procurement_read", "p2p_approval"),
        mission=(
            "Satinalma talebini hazirla, deterministik diff ve dogrulama uret, onay "
            "is akisinin nerede bekledigini goster; yalniz policy/onay/idempotency "
            "protokolu izin verirse SAP'a yaz."
        ),
        iteration_budget=8,
    ),
    "finance": DomainProfile(
        key="finance",
        title="SAP Proje Finans ve Raporlama",
        packs=(BOOTSTRAP_PACK, "project_finance", "p2p_finance", "reporting"),
        mission=(
            "SAP WBS plan/fiili/taahhut verisi, EAC/ETC durumu, tedarikci faturasi "
            "odeme/blokaj durumu ve SAP kaynakli raporlar. SAP disi BOM veya "
            "muhendislik tahmini yapma."
        ),
        iteration_budget=6,
    ),
}

#: Pack -> onu "sahiplenen" domain profili. Prompt parcasini secmek icin
#: kullanilir; yetki karari degildir (o `visible_tool_names` isidir).
PACK_OWNER: dict[str, str] = {
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


def profiles_for_packs(packs: Iterable[str]) -> tuple[DomainProfile, ...]:
    """Acik pack'lerin ait oldugu domain profilleri (sirali, tekrarsiz).

    Cok domainli bir istekte birden fazla profil doner - ama bunlar ayri
    calisan agent'lar degil, **tek prompt'ta birlestirilecek** gorev
    tanimlaridir.
    """
    selected: list[DomainProfile] = []
    for pack in packs:
        key = PACK_OWNER.get(pack)
        if key is None:
            continue
        profile = DOMAIN_PROFILES.get(key)
        if profile is not None and profile not in selected:
            selected.append(profile)
    if not selected:
        selected.append(DOMAIN_PROFILES["platform"])
    return tuple(selected)


def iteration_budget_for(packs: Iterable[str]) -> int:
    """Acik domainlerin en genis butcesi.

    Birlesim aliyoruz cunku tek dongu tum domainlerin isini yapar; en dar
    butceye sikismak cok domainli istegi yarida keserdi.
    """
    profiles = profiles_for_packs(packs)
    return max((p.iteration_budget for p in profiles), default=6)


def profile_catalogue() -> list[dict[str, Any]]:
    """`/agents` uc noktasi icin domain yetenek gorunumu.

    Bilerek "agent" degil "domain" der: ayri calisan agent nesneleri artik
    yok, yaniltici bilgi verilmez.
    """
    return [profile.to_dict() for profile in DOMAIN_PROFILES.values()]
