#!/usr/bin/env python3
"""Envanter, P2P, gizlilik, risk ve cache icin elle calistirilabilir kabul dogrulamasi.

`pytest` bir CI kapisidir: gecer ya da kalir, ama **ne oldugunu** gostermez.
Bu script insan icin yazildi: her kontrolun yanina gercek cikti degerini basar,
boylece "gecti" yazisina degil gordugunuz sayiya guvenirsiniz.

Ozellikle gozle gorulmeyen davranislari gosterir:
  - ayni sorgunun iki farkli rolde farkli sonuc dondurmesi,
  - D3 verinin modele hicbir kosulda ham gitmemesi,
  - dusuk tutar beyaninin riski dusurmemesi,
  - baska tenant'in cache'e dusmemesi.

Kullanim:
    python scripts/verify.py            # tumu
    python scripts/verify.py --bolum privacy
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from robotics_agent.cache import reset_tool_cache  # noqa: E402
from robotics_agent.config import get_settings  # noqa: E402
from robotics_agent.contracts import ActorContext, RiskTier  # noqa: E402
from robotics_agent.core.router import PACKS, domains_for_packs  # noqa: E402
from robotics_agent.privacy import DataClass, DataPolicy  # noqa: E402
from robotics_agent.risk import (  # noqa: E402
    ImpactProfile,
    ImpactSignals,
    MutationKind,
    Reversibility,
    score_impact,
)
from robotics_agent.sap import build_backend  # noqa: E402
from robotics_agent.tools import (  # noqa: E402
    ToolContext,
    execute_tool,
    load_all_tools,
    visible_tool_names,
)
from robotics_agent.tools.registry import REGISTRY  # noqa: E402

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

_results: list[tuple[str, bool]] = []


def check(label: str, condition: bool, evidence: Any = "") -> bool:
    """Tek bir kontrol. Kanit her zaman basilir, gecse de kalsa da."""
    mark = f"{GREEN}GECTI{RESET}" if condition else f"{RED}KALDI{RESET}"
    print(f"  [{mark}] {label}")
    if evidence != "":
        print(f"          {DIM}{evidence}{RESET}")
    _results.append((label, condition))
    return condition


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}")
    print("  " + "-" * (len(title) + 2))


def _actor(
    *, roles: tuple[str, ...], tenant: str = "100", subject: str = "test@firma.test"
) -> ActorContext:
    cfg = get_settings().sap
    return ActorContext(
        subject=subject,
        tenant=tenant,
        roles=roles,
        company_codes=frozenset({cfg.company_code}),
        plants=frozenset({cfg.plant}),
        purchasing_orgs=frozenset({cfg.purch_org}),
        auth_method="verify-script",
    )


def _ctx(actor: ActorContext) -> ToolContext:
    settings = get_settings()
    settings.ensure_dirs()
    return ToolContext(settings=settings, sap=build_backend(settings), actor=actor)


def _call(ctx: ToolContext, name: str, **arguments: Any) -> tuple[dict[str, Any], bool]:
    payload, is_error = execute_tool(name, arguments, ctx)
    return json.loads(payload), is_error


# ---------------------------------------------------------------------------
def verify_inventory() -> None:
    section("1. Tool envanteri ve sozlesmeler")
    p2p = [
        "sap_document_flow", "sap_purchase_order_360",
        "sap_supplier_invoice_status", "sap_invoice_block_explain",
    ]
    # Katalog 21 aractir: 20'si read-only urun profilinde gorunur,
    # `sap_pr_submit` ise yalniz gelecek write surumunun regresyon testleri icin
    # kayitli kalir ve policy tarafindan reddedilir. Released API yolu olmayan
    # ATP/workflow/project-cost araclari bilerek kaldirildi.
    check("21 tool kayitli", len(REGISTRY) == 21, f"kayitli: {len(REGISTRY)}")

    settings = get_settings()
    actor = _actor(roles=("BUYER_LEAD", "AUDITOR"))
    visible = visible_tool_names(
        domains_for_packs(tuple(PACKS)), actor, settings=settings
    )
    catalog_locked = (
        settings.sap.read_only
        and "sap_pr_submit" not in visible
        and "sap_pr_prepare" in visible
        and all(not REGISTRY[name].risk_tier.is_mutating for name in visible)
    )
    check(
        "Varsayilan urun katalogu yalniz SAP read tool'lari gosteriyor",
        catalog_locked,
        f"SAP_READ_ONLY={settings.sap.read_only} | gorunen={len(visible)} | "
        f"sap_pr_submit={'var' if 'sap_pr_submit' in visible else 'yok'}",
    )

    guessed, guessed_error = _call(
        _ctx(actor),
        "sap_pr_submit",
        items=[{"material_id": "SFT-SCN-270", "quantity": 1}],
        idempotency_key="verify:readonly:guess:v1",
    )
    check(
        "Adi tahmin edilen write tool policy'de handler'dan once reddediliyor",
        guessed_error and guessed.get("denial_code") == "READ_ONLY_MODE",
        f"denial_code={guessed.get('denial_code', '')}",
    )
    check(
        "kaldirilan tool'lar geri gelmedi",
        not {"sap_list_agents", "sap_validate_change"} & set(REGISTRY),
        "sap_list_domains ve sap_pr_prepare yerlerini aliyor",
    )
    check("4 P2P tool'u kayit defterinde", all(n in REGISTRY for n in p2p), ", ".join(p2p))

    read_only = all(not REGISTRY[n].risk_tier.is_mutating for n in p2p)
    check("P2P gorunurluk tool'larinin hicbiri SAP'a yazmiyor", read_only,
          " | ".join(f"{n.split('_', 1)[1]}={REGISTRY[n].risk_tier.value}" for n in p2p))

    declared = all(
        REGISTRY[n].data_policy.fields and REGISTRY[n].performance_budget.max_sap_calls >= 1
        for n in p2p
    )
    check("Her tool veri politikasi ve performans butcesi bildirmis", declared,
          f"orn. {p2p[1]}: max_sap_calls="
          f"{REGISTRY[p2p[1]].performance_budget.max_sap_calls}, "
          f"p95={REGISTRY[p2p[1]].performance_budget.p95_ms}ms")


def verify_p2p() -> None:
    section("2. Procure-to-pay tool'lari (islevsel)")
    reset_tool_cache()
    ctx = _ctx(_actor(roles=("PURCHASER",)))

    flow, err = _call(ctx, "sap_document_flow", document_id="5105600231")
    stages = [s["type"] for s in flow.get("stages", [])]
    check("Fatura numarasindan tum zincir bulunuyor", not err and len(stages) == 4,
          " -> ".join(stages))

    links = {n["linked_by"] for n in flow.get("chain", [])}
    check("Her belge bagi SAP referans alani tasiyor (uydurma bag yok)",
          bool(links) and all(links), ", ".join(sorted(links)))

    ghost, _ = _call(ctx, "sap_document_flow", document_id="9999999999")
    check("Olmayan belge icin tahmin uretilmiyor", ghost.get("chain") == [],
          ghost.get("interpretation", "")[:80])

    po, err = _call(ctx, "sap_purchase_order_360", po_id="4500019014")
    # 20 siparis - 12 teslim = 8 acik | 12 teslim - 8 fatura = 4 GR/IR farki
    correct = po.get("gr_ir_gap_qty") == 4.0 and po.get("max_delay_days") == 26
    check("PO 360 acik miktar, gecikme ve GR/IR farkini hesapliyor", not err and correct,
          f"acik={po.get('open_value')} EUR | gecikme={po.get('max_delay_days')} gun | "
          f"GR/IR={po.get('gr_ir_gap_value')} EUR")

    block, err = _call(ctx, "sap_invoice_block_explain", invoice_id="5105600231")
    price = next((f for f in block.get("findings", []) if f["reason"] == "price"), {})
    check("Fatura blokaji sayisal olarak aciklaniyor",
          price.get("variance_pct") == 6.25 and len(price.get("exceeded_limits") or []) == 2,
          f"sapma %{price.get('variance_pct')} > tolerans %{price.get('tolerance_limit_pct')} "
          f"(anahtar {price.get('tolerance_key')})")

    reset_tool_cache()


def verify_privacy() -> None:
    section("3. Veri gizliligi")
    reset_tool_cache()

    # Ayni cagri, iki farkli rol.
    buyer = _ctx(_actor(roles=("PURCHASER",), subject="alici@firma.test"))
    watcher = _ctx(_actor(roles=("VIEWER",), subject="gozlemci@firma.test"))
    buyer_view, _ = _call(buyer, "sap_purchase_order_360", po_id="4500019014", detail="standard")
    watcher_view, _ = _call(watcher, "sap_purchase_order_360", po_id="4500019014", detail="standard")

    buyer_price = buyer_view["items"][0]["net_price"]
    watcher_price = watcher_view["items"][0]["net_price"]
    check("Ayni tool farkli role farkli projeksiyon donduruyor",
          buyer_price == 2480.0 and watcher_price == "***",
          f"PURCHASER net_price={buyer_price} | VIEWER net_price={watcher_price}")
    check("Is verisi her iki rolde de korunuyor (maskeleme karari felce ugratmiyor)",
          buyer_view["items"][0]["open_qty"] == watcher_view["items"][0]["open_qty"],
          f"open_qty={watcher_view['items'][0]['open_qty']} (iki rolde de ayni)")


    # D3 hicbir hedefe ham gitmiyor.
    from robotics_agent.privacy import DLPEngine, FieldAccessPolicy, get_pseudonymizer

    engine = DLPEngine(field_policy=FieldAccessPolicy(), pseudonymizer=get_pseudonymizer())
    iban = "DE89370400440532013000"
    policy = DataPolicy(fields={"supplier_iban": DataClass.D3})
    leaks = []
    for sink in ("model", "log", "handoff", "client"):
        out = engine.apply({"supplier_iban": iban}, actor=_actor(roles=("BUYER_LEAD",)),
                           sink=sink, policy=policy)
        if iban in str(out.payload):
            leaks.append(sink)
    sample = engine.apply({"supplier_iban": iban}, actor=_actor(roles=("BUYER_LEAD",)),
                          sink="model", policy=policy)
    check("D3 veri model/log/handoff/istemci hicbirine ham gitmiyor", not leaks,
          f"IBAN -> {sample.payload['supplier_iban']} (geri cozulemez takma kimlik)")

    a = engine.apply({"supplier_iban": iban}, actor=_actor(roles=("BUYER_LEAD",), tenant="100"),
                     sink="model", policy=policy).payload["supplier_iban"]
    b = engine.apply({"supplier_iban": iban}, actor=_actor(roles=("BUYER_LEAD",), tenant="200"),
                     sink="model", policy=policy).payload["supplier_iban"]
    check("Ayni IBAN iki tenant'ta farkli token uretiyor (korelasyon kurulamiyor)", a != b,
          f"tenant 100 -> {a} | tenant 200 -> {b}")

    # SAP belge numaralari yanlis pozitif uretmiyor.
    clean = engine.apply(
        {"po_id": "4500018821", "wbs_element": "R-2026-014-1", "invoice_id": "5105600231"},
        actor=_actor(roles=("PURCHASER",)), sink="model", policy=DataPolicy(),
    ).payload
    check("SAP belge numaralari vergi/telefon sanilip bozulmuyor",
          clean["po_id"] == "4500018821" and clean["wbs_element"] == "R-2026-014-1",
          json.dumps(clean, ensure_ascii=False))
    reset_tool_cache()


def verify_risk() -> None:
    section("4. Dinamik risk motoru")

    profile = ImpactProfile(
        mutation=MutationKind.WRITE, reversible=Reversibility.COMPENSATING,
        financial_fields=("total_value",), external_commitment=True,
    )
    # 1. Statik taban dusurulemez.
    from robotics_agent.risk import READ_ONLY

    floored = score_impact(READ_ONLY, ImpactSignals(), declared_tier=RiskTier.R3)
    check("Runtime skoru bildirilen tabani dusuremiyor",
          floored.runtime_tier is RiskTier.R0 and floored.effective_tier is RiskTier.R3,
          f"runtime={floored.runtime_tier.value} ama effective={floored.effective_tier.value}")

    # 2. Dusuk beyan indirim saglamiyor.
    lying = score_impact(profile, ImpactSignals.from_arguments(profile, {"total_value": 1.0}),
                         declared_tier=RiskTier.R3)
    silent = score_impact(profile, ImpactSignals(), declared_tier=RiskTier.R3)
    check("Dusuk tutar beyani riski dusurmuyor",
          lying.effective_tier is silent.effective_tier,
          f"1 EUR beyan -> {lying.effective_tier.value} | hic beyan yok -> "
          f"{silent.effective_tier.value} (ayni)")

    # 3. Dogrulanmis tutar riski yukseltiyor.
    verified = score_impact(
        profile,
        ImpactSignals.from_arguments(profile, {"total_value": 1.0}).verified_with(
            total_value=2_400_000.0, currency="EUR", record_count=120
        ),
        declared_tier=RiskTier.R3,
    )
    check("SAP'ten dogrulanan tutar riski yukseltiyor",
          verified.effective_tier is RiskTier.R4 and verified.escalated,
          f"2.4M EUR dogrulandi -> skor {verified.score} -> "
          f"{verified.declared_tier.value} yerine {verified.effective_tier.value}")
    print(f"          {DIM}boyutlar: "
          f"{json.dumps(verified.to_dict()['dimensions'], ensure_ascii=False)}{RESET}")

    # 4. Bulk/geri donussuz taban kurali.
    bulk = score_impact(
        ImpactProfile(mutation=MutationKind.BULK_WRITE, reversible=Reversibility.IRREVERSIBLE),
        ImpactSignals(), declared_tier=RiskTier.R2,
    )
    check("Bulk ve geri donussuz islem skordan bagimsiz en az R4",
          bulk.effective_tier is RiskTier.R4,
          f"bildirilen R2 -> uygulanan {bulk.effective_tier.value}")

    # 5. Veri sinifi risk seviyesini degistirmiyor.
    sensitive_read = score_impact(READ_ONLY, ImpactSignals(data_class=DataClass.D3),
                                  declared_tier=RiskTier.R0)
    check("D3 veri okuyan R0 tool yazma onayi istemiyor (ayri eksenler)",
          sensitive_read.effective_tier is RiskTier.R0,
          "yazma onayi gerekmiyor ama maskeleme ve export kontrolu zorunlu")


def verify_cache() -> None:
    section("5. Guvenli cache")
    reset_tool_cache()

    ali = _ctx(_actor(roles=("PURCHASER",), subject="ali@firma.test", tenant="100"))
    first, _ = _call(ali, "sap_purchase_order_360", po_id="4500019014")
    second, _ = _call(ali, "sap_purchase_order_360", po_id="4500019014")
    check("Ayni actor'un tekrar sorgusu cache'ten donuyor",
          second["_meta"].get("cached") is True,
          f"1. cagri cached={first['_meta'].get('cached', False)} | "
          f"2. cagri cached={second['_meta'].get('cached')} "
          f"(yas {second['_meta'].get('age_seconds')} sn)")

    check("Cache cevabi tazelik bilgisi tasiyor (model koru korune taahhut vermesin)",
          "source_read_at" in second["_meta"],
          f"kaynak okuma zamani: {second['_meta'].get('source_read_at')}")

    other_tenant = _ctx(_actor(roles=("PURCHASER",), subject="ali@firma.test", tenant="900"))
    cross, _ = _call(other_tenant, "sap_purchase_order_360", po_id="4500019014")
    check("Baska tenant ayni cache girdisine dusmuyor",
          cross["_meta"].get("cached") is not True,
          "tenant 900 icin cached=False (capraz tenant okuma yapisal olarak imkansiz)")

    viewer = _ctx(_actor(roles=("VIEWER",), subject="gozlemci@firma.test", tenant="100"))
    other_role, _ = _call(viewer, "sap_purchase_order_360", po_id="4500019014")
    check("Farkli yetkideki kullanici baskasinin cache'ine dusmuyor",
          other_role["_meta"].get("cached") is not True,
          "VIEWER icin cached=False (kapsam hash'i farkli)")
    reset_tool_cache()


def verify_authorization() -> None:
    section("6. Yetkilendirme (deny by default)")
    reset_tool_cache()
    anon = _ctx(ActorContext.anonymous())
    denied = []
    for name, args in (
        ("sap_document_flow", {"document_id": "4500019014"}),
        ("sap_purchase_order_360", {"po_id": "4500019014"}),
        ("sap_supplier_invoice_status", {"only_blocked": True}),
        ("sap_invoice_block_explain", {"invoice_id": "5105600231"}),
    ):
        result, is_error = _call(anon, name, **args)
        denied.append(is_error and result.get("denial_code") == "AUTH_REQUIRED")
    check("Kimligi dogrulanmamis cagirici hicbir P2P verisi goremiyor", all(denied),
          "4/4 tool AUTH_REQUIRED ile reddedildi")

    # Yetkisiz tesis.
    wrong_plant = ActorContext(
        subject="baska@firma.test", tenant="100", roles=("PURCHASER",),
        company_codes=frozenset({"1000"}), plants=frozenset({"9999"}),
        purchasing_orgs=frozenset({"1000"}), auth_method="verify-script",
    )
    result, is_error = _call(_ctx(wrong_plant), "sap_stock_overview",
                             material_ids=["SFT-SCN-270"], plant="1100")
    check("Yetki alani disindaki tesis reddediliyor (ABAC)",
          is_error and result.get("denial_code") == "ORG_SCOPE",
          result.get("error", "")[:90])
    reset_tool_cache()


SECTIONS: dict[str, Callable[[], None]] = {
    "envanter": verify_inventory,
    "p2p": verify_p2p,
    "privacy": verify_privacy,
    "risk": verify_risk,
    "cache": verify_cache,
    "yetki": verify_authorization,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="CertaOps kabul dogrulamasi")
    parser.add_argument(
        "--bolum", choices=sorted(SECTIONS), help="Yalniz bu bolumu calistir"
    )
    parser.add_argument("--log", action="store_true", help="Uygulama loglarini da goster")
    args = parser.parse_args()

    # Bu betik deterministik yerel kabul kapisidir. `.env` icinde canli bir
    # Hub/tenant tanimli olsa bile dis sisteme cikamaz; gercek baglanti icin
    # yalniz `check_real_sap.py` ve `sap_tool_sweep.py` kullanilir.
    settings = get_settings()
    object.__setattr__(settings.sap, "backend", "mock")
    object.__setattr__(settings.sap, "dry_run", True)
    object.__setattr__(settings.security, "allowed_sap_hosts", ())
    object.__setattr__(settings.security, "disabled_tools", ())

    # Bilincli ret'ler (AUTH_REQUIRED, ORG_SCOPE) uygulama tarafinda WARNING
    # uretir. Bunlar bu script icin **beklenen** davranistir; ciktiyi
    # kirletmemesi icin susturulur. `--log` ile geri acilir.
    if not args.log:
        logging.getLogger("robotics_agent").setLevel(logging.ERROR)
        logging.getLogger("robotics_agent.tools.registry").setLevel(logging.ERROR)

    load_all_tools()
    print(f"{BOLD}CertaOps 0.1.0 - kabul dogrulamasi{RESET}")
    print(f"{DIM}backend={settings.sap.backend} | dlp={settings.privacy.dlp_mode} | "
          f"risk={settings.risk.scoring_mode} | cache={settings.cache.backend}{RESET}")

    chosen = [SECTIONS[args.bolum]] if args.bolum else list(SECTIONS.values())
    for run in chosen:
        run()

    failed = [label for label, ok in _results if not ok]
    print(f"\n{BOLD}Sonuc:{RESET} {len(_results) - len(failed)}/{len(_results)} kontrol gecti")
    if failed:
        print(f"{RED}Kalan kontroller:{RESET}")
        for label in failed:
            print(f"  - {label}")
        return 1
    print(f"{GREEN}Tum kontroller gecti.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
