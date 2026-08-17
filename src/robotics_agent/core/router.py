"""SAP domain routing ve agent tool pack'leri.

Tum tool semalarini her istege eklemek gereksiz token maliyeti ve saldiri
yuzeyi acar. Cozum:

  1. Ilk asamada yalniz kucuk bir bootstrap seti gorunur (capability discovery,
     domain router, audit, evidence, mutabakat).
  2. Router; kullanici metni, actor rolleri ve risk seviyesine gore domain pack
     secer. Sinsiflandirma kural tabanlidir; LLM tokeni harcamaz.
  3. Ikinci asamada yalniz ilgili ve **yetkili** tool'lar yuklenir. Kullanicinin
     yetkisi olmayan mutating tool modele hic gosterilmez.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts import ActorContext, estimate_tokens

BOOTSTRAP_PACK = "bootstrap"


@dataclass(frozen=True)
class DomainPack:
    key: str
    title: str
    domains: tuple[str, ...]
    triggers: tuple[str, ...] = ()
    includes: tuple[str, ...] = ()
    description: str = ""

    def all_domains(self, registry: dict[str, DomainPack]) -> tuple[str, ...]:
        collected: list[str] = list(self.domains)
        for included in self.includes:
            pack = registry.get(included)
            if pack is not None:
                collected.extend(d for d in pack.all_domains(registry) if d not in collected)
        return tuple(collected)


PACKS: dict[str, DomainPack] = {
    BOOTSTRAP_PACK: DomainPack(
        key=BOOTSTRAP_PACK,
        title="Platform cekirdegi",
        domains=("platform",),
        description=(
            "Her turda acik olan kucuk set: yetenek kesfi, domain router, islem durumu, "
            "audit sorgusu ve evidence erisimi."
        ),
    ),
    "diagnostics": DomainPack(
        key="diagnostics",
        title="Teshis ve dogrulama",
        domains=("diagnostics",),
        triggers=(
            "baglanti",
            "saglik",
            "health",
            "yetki hatasi",
            "403",
            "401",
            "yetkisiz",
            "dogrula",
            "validate",
            "neden calismiyor",
            "hata aliyorum",
        ),
        description=(
            "Baglanti sagligi, yetki hatasi teshisi ve yazma oncesi payload dogrulamasi. "
            "Bir sey beklendigi gibi calismadiginda acilir."
        ),
    ),
    "master_data": DomainPack(
        key="master_data",
        title="Malzeme ana verisi",
        domains=("master_data",),
        triggers=(
            "malzeme",
            "material",
            "urun",
            "stok karti",
            "siniflandirma",
            "karakteristik",
            "360",
            "ara",
            "bul",
        ),
        description="Malzeme arama, 360 gorunum, siniflandirma ve veri kalitesi.",
    ),
    "procurement_read": DomainPack(
        key="procurement_read",
        title="Satinalma (okuma)",
        domains=("planning", "procurement_read"),
        triggers=(
            "stok",
            "atp",
            "mrp",
            "bulunabilirlik",
            "termin",
            "tedarikci",
            "vendor",
            "fiyat",
            "tco",
            "siparis",
            "purchase order",
            "gecikme",
            "eksik",
            "shortage",
            "teyit",
        ),
        description="ATP, MRP shortage, tedarikci skoru, TCO ve siparis takibi.",
    ),
    "procurement_write": DomainPack(
        key="procurement_write",
        title="Satinalma (yazma)",
        domains=("procurement_write",),
        # Yazma akisi ATP/tedarikci okumasina ihtiyac duyar; malzeme arama ise
        # bu asamada gerekmez (numaralar zaten belirlenmis olur). Ana veri ayri
        # profile birakilir; bu, yazma profilinin sema butcesini
        # (`BUDGET_SCHEMA_TOKENS`, varsayilan 4.000) icinde tutar. Olculen deger
        # `python demo.py --tokens` ciktisinda gorulur.
        includes=("procurement_read",),
        triggers=(
            "talep ac",
            "talep olustur",
            "satinalma talebi",
            "pr olustur",
            "requisition",
            "siparis ac",
            "onay",
            "submit",
        ),
        description="PR hazirlama/gonderme ve degisiklik dogrulama.",
    ),
    # --- Procure-to-pay gorunurlugu ----------------------------------------
    # Ayri pack olmasinin nedeni sema butcesi: bu tool'lari mevcut
    # `procurement_read` pack'ine koymak, Satinalma profilini
    # `BUDGET_SCHEMA_TOKENS` sinirinin uzerine cikarirdi. Ayirmak hem butceyi
    # korur hem de her profilin yalniz kendi isine yarayan semayi gormesini
    # saglar. Butce tek kaynaktir; yorumda sabit bir sayi tekrarlanmaz.
    "p2p_visibility": DomainPack(
        key="p2p_visibility",
        title="Belge akisi ve siparis durumu",
        domains=("p2p_flow",),
        triggers=(
            "belge akisi",
            "document flow",
            "zincir",
            "siparis durumu",
            "siparis nerede",
            "po durumu",
            "mal kabul",
            "teslim edildi mi",
            "irsaliye",
            "gr/ir",
            "nerede kaldi",
            "hangi talepten",
        ),
        description="PR-PO-mal kabul-fatura zinciri ve siparis 360 gorunumu.",
    ),
    "p2p_approval": DomainPack(
        key="p2p_approval",
        title="Onay is akisi durumu",
        domains=("p2p_approval",),
        triggers=(
            "onayda",
            "onay bekliyor",
            "kimde bekliyor",
            "is akisi",
            "workflow",
            "serbest birakma",
            "release",
        ),
        description="Onayin hangi adimda, kimde ve neden bekledigi.",
    ),
    "p2p_finance": DomainPack(
        key="p2p_finance",
        title="Tedarikci faturasi ve blokaj",
        domains=("p2p_finance",),
        triggers=(
            "fatura",
            "invoice",
            "odeme",
            "payment",
            "blokaj",
            "bloke",
            "vade",
            "odendi mi",
            "tolerans",
            "fiyat farki",
        ),
        description="Fatura muhasebe/odeme durumu ve tolerans blokaji aciklamasi.",
    ),
    "project_finance": DomainPack(
        key="project_finance",
        title="Proje ve finans",
        domains=("project_finance",),
        triggers=(
            "maliyet",
            "butce",
            "marj",
            "wbs",
            "proje",
            "eac",
            "nakit",
            "asim",
            "kontrolling",
            "forecast",
            "sap proje maliyeti",
        ),
        description="Maliyet tahmini, butce projeksiyonu ve WBS finansal durumu.",
    ),
    "reporting": DomainPack(
        key="reporting",
        title="Raporlama",
        domains=("reporting",),
        triggers=("rapor", "excel", "xlsx", "ozet dosya", "sunum", "dokuman uret"),
        description="Excel/Markdown rapor uretimi.",
    ),
}

# Bir pack'i acmak icin gereken minimum kapsam; yoksa pack hic teklif edilmez.
PACK_REQUIRED_SCOPES: dict[str, tuple[str, ...]] = {
    "procurement_write": ("sap.prepare",),
}

_TR_MAP = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def _normalize(text: str) -> str:
    return text.translate(_TR_MAP).lower()


@dataclass
class RoutingDecision:
    packs: tuple[str, ...]
    matched_triggers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    fallback: bool = False
    omitted_packs: tuple[str, ...] = ()

    @property
    def truncated(self) -> bool:
        """Yetkili bir eslesme ``max_packs`` siniri nedeniyle atlandi mi?"""

        return bool(self.omitted_packs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "packs": list(self.packs),
            "matched_triggers": {k: list(v) for k, v in self.matched_triggers.items()},
            "fallback": self.fallback,
            "omitted_packs": list(self.omitted_packs),
            "truncated": self.truncated,
        }


def route(message: str, actor: ActorContext, *, max_packs: int = 2) -> RoutingDecision:
    """Kullanici metnine gore domain pack secer.

    Bootstrap her zaman aciktir. Eslesme yoksa yalniz teshis pack'i acilir;
    genis bir veri pack'ini tahmin ederek modele gostermek yerine kullanicidan
    hedefini netlestirmesi beklenir. Mutating pack asla varsayilan acilmaz.
    """
    normalized = _normalize(message or "")
    scores: list[tuple[int, str, tuple[str, ...]]] = []

    for key, pack in PACKS.items():
        if key == BOOTSTRAP_PACK or not pack.triggers:
            continue
        if not _actor_may_open(pack.key, actor):
            continue
        hits = tuple(t for t in pack.triggers if _normalize(t) in normalized)
        if hits:
            scores.append((len(hits), key, hits))

    scores.sort(key=lambda item: (-item[0], item[1]))
    selected_scores = scores[:max_packs]
    omitted_scores = scores[max_packs:]
    chosen = [key for _, key, _ in selected_scores]
    matched = {key: hits for _, key, hits in selected_scores}
    fallback = False
    if not chosen:
        fallback = True
        chosen = ["diagnostics"] if _actor_may_open("diagnostics", actor) else []

    ordered = _expand(chosen)
    # Bir alt-skorlu pack, secilen bir pack'in dependency'si olarak zaten
    # acilmissa gercekte omit edilmis sayilmaz.
    omitted = tuple(key for _, key, _ in omitted_scores if key not in ordered)
    return RoutingDecision(
        packs=ordered,
        matched_triggers=matched,
        fallback=fallback,
        omitted_packs=omitted,
    )


def _actor_may_open(pack_key: str, actor: ActorContext) -> bool:
    required = PACK_REQUIRED_SCOPES.get(pack_key, ())
    return all(actor.has_scope(scope) for scope in required)


def _expand(pack_keys: Sequence[str]) -> tuple[str, ...]:
    """Bootstrap + istenen pack'ler + bagimliliklari, sirali ve tekrarsiz."""
    ordered: list[str] = [BOOTSTRAP_PACK]
    stack = list(pack_keys)
    while stack:
        key = stack.pop(0)
        pack = PACKS.get(key)
        if pack is None or key in ordered:
            continue
        ordered.append(key)
        stack.extend(p for p in pack.includes if p not in ordered)
    return tuple(ordered)


def domains_for_packs(pack_keys: Iterable[str]) -> frozenset[str]:
    domains: set[str] = set()
    for key in pack_keys:
        pack = PACKS.get(key)
        if pack is not None:
            domains.update(pack.all_domains(PACKS))
    return frozenset(domains)


def pack_catalogue(actor: ActorContext | None = None) -> list[dict[str, Any]]:
    """Router tool'unun modele gosterdigi kisa katalog."""
    out: list[dict[str, Any]] = []
    for key, pack in PACKS.items():
        if key == BOOTSTRAP_PACK:
            continue
        entry: dict[str, Any] = {"pack": key, "title": pack.title, "covers": pack.description}
        if actor is not None and not _actor_may_open(key, actor):
            entry["available"] = False
            entry["reason"] = "Gereken yetki kapsami yok."
        out.append(entry)
    return out


def schema_token_report(definitions: Sequence[dict[str, Any]], *, budget: int) -> dict[str, Any]:
    """Aktif tool semalarinin token maliyeti ve butce durumu."""
    total = sum(estimate_tokens(d) for d in definitions)
    return {
        "tool_count": len(definitions),
        "schema_tokens": total,
        "budget": budget,
        "within_budget": total <= budget,
        "largest": sorted(
            ({"tool": d.get("name", "?"), "tokens": estimate_tokens(d)} for d in definitions),
            key=lambda item: -item["tokens"],
        )[:5],
    }


def normalize_pack_keys(raw: Iterable[str]) -> tuple[str, ...]:
    """Kullanici/model tarafindan verilen pack adlarini dogrular."""
    cleaned = [key.strip().lower() for key in raw if key and key.strip()]
    return tuple(key for key in cleaned if key in PACKS)


_WORD_SPLIT = re.compile(r"[^a-z0-9]+")


def summarize_intent(message: str, *, max_words: int = 12) -> str:
    """Telemetriye prompt icerigi yazmadan intent izi birakmak icin.

    Yalniz alfanumerik anahtar kelimeler, kisaltilmis. Sayilar ve ozel adlar
    dusurulmez ama tam metin de saklanmaz.
    """
    words = [w for w in _WORD_SPLIT.split(_normalize(message)) if len(w) > 3]
    return " ".join(words[:max_words])
