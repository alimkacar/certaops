#!/usr/bin/env python3
"""OData backend'inin uctan uca duman testi (gercek HTTP uzerinden).

`pytest` bir kapidir: gecer ya da kalir. Bu script **ne olup bittigini
gosterir** - hangi servise kac cagri gitti, govdede tam olarak ne var, hesap
atamasi nereye yazildi. Gercek bir SAP baglantisindan once burada gorulen
degerler, gercek sistemde beklenecek degerlerdir.

Iki modda calisir:

1. **Sahte Gateway (varsayilan).** Kendi surecinde bir sahte SAP baslatir;
   hicbir kurulum gerekmez.

       python scripts/smoke_odata.py

2. **Dis sistem.** Ortam degiskenleri ile hedef verilir (sahte Gateway'i ayri
   terminalde calistirmak veya gercek bir quality tenant'a baglanmak icin).

       SAP_BASE_URL=https://s4q.firma.local:44300 \\
       SAP_ALLOWED_HOSTS=s4q.firma.local \\
       SAP_AUTH_MODE=oauth2 ... \\
       python scripts/smoke_odata.py --external

UYARI: `--write` bayragi GERCEKTEN satinalma talebi olusturur. Dis sistemde
yazma icin ayrica `SAP_DRY_RUN=false` ve `SAP_INTEGRATION_ALLOW_WRITE=1`
kapilarinin ikisi de acik olmalidir.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"

_failures: list[str] = []


def check(label: str, condition: bool, evidence: object = "") -> bool:
    mark = f"{GREEN}GECTI{RESET}" if condition else f"{RED}KALDI{RESET}"
    print(f"  [{mark}] {label}")
    if evidence != "":
        print(f"          {DIM}{evidence}{RESET}")
    if not condition:
        _failures.append(label)
    return condition


def section(title: str) -> None:
    print(f"\n{BOLD}{title}{RESET}\n  " + "-" * (len(title) + 2))


def _configure_local(port: int) -> None:
    """Sahte Gateway'e baglanacak minimum ayar seti."""
    os.environ.update({
        "SAP_BACKEND": "odata",
        "SAP_BASE_URL": f"http://127.0.0.1:{port}",
        "SAP_CLIENT": "100",
        "SAP_AUTH_MODE": "basic",
        "SAP_USERNAME": "SMOKE",
        "SAP_PASSWORD": "smoke",
        "SAP_ALLOWED_HOSTS": "127.0.0.1",
        "SAP_PLANT": "1100",
        "SAP_PURCH_ORG": "1000",
        "SAP_PURCH_GROUP": "R01",
        "SAP_COMPANY_CODE": "1000",
        "SAP_CURRENCY": "EUR",
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", "smoke-test"),
        "AGENT_STATE_DIR": os.getenv("AGENT_STATE_DIR", "./state"),
    })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--external", action="store_true",
                        help="Sahte Gateway baslatma; ortam degiskenlerini kullan.")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--write", action="store_true",
                        help="Yazma dene; dis sistemde iki ek guvenlik kapisi gerekir.")
    parser.add_argument("--material", default="MAT-1")
    args = parser.parse_args(argv)

    if args.external and args.write:
        dry_run_off = os.getenv("SAP_DRY_RUN", "true").strip().lower() in {
            "0", "false", "no",
        }
        integration_write = os.getenv("SAP_INTEGRATION_ALLOW_WRITE", "") == "1"
        if not (dry_run_off and integration_write):
            print(
                f"{RED}Dis sisteme yazma engellendi.{RESET} --write yaninda "
                "SAP_DRY_RUN=false ve SAP_INTEGRATION_ALLOW_WRITE=1 birlikte gerekli."
            )
            return 2

    tracker = None
    if not args.external:
        import fake_sap_gateway as gateway

        gateway.serve(args.port)
        tracker = gateway.STATE
        _configure_local(args.port)
        print(f"{DIM}Sahte SAP Gateway 127.0.0.1:{args.port} uzerinde calisiyor.{RESET}")

    from robotics_agent.adapters.sap import SAPError, SAPNotSupported
    from robotics_agent.config import Settings
    from robotics_agent.sap import build_backend
    from robotics_agent.sap.models import PurchaseRequisitionItem

    settings = Settings()
    problems = settings.sap.validate()
    if problems:
        print(f"{RED}SAP konfigurasyonu eksik:{RESET} " + "; ".join(problems))
        return 2

    backend = build_backend(settings)
    material = args.material

    # --- 1. Baglanti -------------------------------------------------------
    section("1. Baglanti ve kimlik")
    health = backend.ping()
    check("SAP'a erisiliyor", health.get("status") == "ok", health)
    check("Kimlik modu bildiriliyor",
          bool(backend.connection.describe().get("auth")),
          backend.connection.describe())

    # --- 2. Servis surumu secimi ------------------------------------------
    section("2. Servis surumu secimi (V4 -> V2 fallback)")
    resolved = backend.resolved_services()
    for job, info in resolved.items():
        check(f"{job}: {info['odata']} ({info['status']})", True, info["service"])

    # --- 3. Okuma ----------------------------------------------------------
    section("3. Okuma ve alan minimizasyonu")
    master = backend.get_material(material)
    check("Malzeme ana verisi okundu", master is not None,
          f"{master.material_id} / {master.description}" if master else "yok")
    check("Aciklama bos degil (dil/expand dogru)",
          bool(master and master.description),
          "Bos ise to_Description expand'i kontrol edin")

    records = backend.get_info_records(material)
    check("Bilgi kaydi okundu", bool(records),
          [(r.vendor_id, r.vendor_name, r.net_price) for r in records])
    check("Tedarikci adi dolduruldu (V2 uyumlu okuma)",
          all(r.vendor_name for r in records) if records else False,
          "Bos ise A_Supplier okumasi basarisiz")

    levels = backend.get_stock([material])
    check("Stok okundu", bool(levels),
          {"serbest": levels[0].unrestricted_qty, "yolda": levels[0].on_order_qty}
          if levels else "yok")

    # --- 4. Toplu okuma (performans) --------------------------------------
    section("4. Performans: cagri sayisi kalem sayisindan bagimsiz mi")
    if tracker is not None:
        tracker.requests.clear()
    items = [PurchaseRequisitionItem(material_id=material, quantity=1) for _ in range(5)]
    draft_perf = backend.prepare_purchase_requisition(items, header_text="SMOKE PERF")
    if tracker is not None:
        reads = [p for m, p in tracker.requests if m == "GET" and "$metadata" not in p]
        check("5 kalemli talep <= 6 SAP okumasi yapiyor", len(reads) <= 6,
              f"{len(reads)} okuma: " + ", ".join(sorted({r.rsplit('/', 1)[-1] for r in reads})))
    check("Taslak tutari hesaplandi", draft_perf.total_value > 0,
          f"{draft_perf.total_value} {draft_perf.currency}")

    # --- 5. Yazma govdesi ---------------------------------------------------
    section("5. Satinalma talebi govdesi")
    draft = backend.prepare_purchase_requisition(
        [PurchaseRequisitionItem(
            material_id=material, quantity=3, wbs_element="SMOKE-WBS-1",
            delivery_date=date.today() + timedelta(days=45),
        )],
        header_text="SMOKE TEST",
    )
    item = draft.payload["items"][0]
    check("Hesap atamasi kategorisi kalemde (P = proje)",
          item.get("AccountAssignmentCategory") in ("P", None),
          item.get("AccountAssignmentCategory") or "sozlesmede alan yok, gonderilmedi")
    # Sekil sozlesmeden turetilir: alt entity (released API) ya da kalem
    # uzerinde inline. Ikisi de gecerlidir; onemli olan SOZLESMEYE UYMASI.
    acct_nav = next(
        (k for k in item if k.endswith("PurchaseReqnAcctAssgmt")), ""
    )
    inline = "WBSElement" in item
    check("Hesap atamasi sozlesmeye uygun yerde",
          bool(acct_nav) != inline,
          f"alt entity: {item.get(acct_nav)}" if acct_nav else "kalem uzerinde (inline)")
    check("Miktar/fiyat JSON sayisi (string degil)",
          isinstance(item.get("RequestedQuantity"), int | float)
          and isinstance(item.get("PurchaseRequisitionPrice"), int | float),
          {k: type(item.get(k)).__name__
           for k in ("RequestedQuantity", "PurchaseRequisitionPrice")})
    check("Bos string alan yok",
          not [k for k, v in item.items() if v == ""],
          [k for k, v in item.items() if v == ""] or "temiz")
    print(f"\n{DIM}Tam kalem govdesi:{RESET}")
    print(json.dumps(item, ensure_ascii=False, indent=2))

    # --- 6. Yazma + geri okuma ---------------------------------------------
    if args.write:
        section("6. Yazma, geri okuma ve mutabakat")
        # Her calistirmada benzersiz: kalici bir test sisteminde onceki
        # calistirmanin belgesini bulup yanlis pozitif uretmesin.
        reference = f"smoke:{date.today().isoformat()}:{os.getpid()}:pr:v1"
        try:
            result = backend.submit_purchase_requisition(draft, external_reference=reference)
            check("Talep olusturuldu", bool(result.requisition_id), result.requisition_id)
            record = backend.read_purchase_requisition(result.requisition_id or "")
            check("Read-after-write dogrulandi",
                  bool(record) and record["item_count"] == len(draft.items),
                  record)
            found = backend.find_purchase_requisition_by_reference(reference)
            check("Referansla mutabakat bulundu (timeout senaryosu)",
                  bool(found) and found[0] == result.requisition_id,
                  found[0] if found else None)
        except SAPError as exc:
            check("Yazma tamamlandi", False, f"{exc.code}: {exc}")
    else:
        section("6. Yazma (atlandi)")
        print(f"  {DIM}--write ile calistirin. GERCEK sistemde belge olusturur.{RESET}")

    # --- 7. Egress ---------------------------------------------------------
    section("7. Egress allowlist")
    try:
        backend._core_v4._assert_host_allowed("https://exfil.example/steal")
        check("Izinsiz host engellendi", False, "Engellenmedi!")
    except SAPError as exc:
        check("Izinsiz host engellendi", True, exc.code)

    # --- 8. Yetenek sondasi -------------------------------------------------
    section("8. Kontrat sondasi (sap_discover_capabilities karsiligi)")
    # Fiilen KULLANILAN alias sondalanir. Birincil V4 alias'ini sondalamak,
    # V2 fallback'e dusmus dogru calisan bir sistemi hatali gosterirdi.
    try:
        used = [info["alias"] for info in resolved.values()]
        for probe in backend.probe_capabilities(used):
            check(f"{probe['alias']}: sozlesme uyumlu",
                  bool(probe.get("contract_ok")),
                  {k: v for k, v in probe.items() if k != "alias"})
    except (SAPError, SAPNotSupported) as exc:
        check("Kontrat sondasi calisti", False, str(exc))

    backend.close()

    print()
    total = len(_failures)
    if total:
        print(f"{RED}{total} kontrol kaldi:{RESET}")
        for name in _failures:
            print(f"  - {name}")
        return 1
    print(f"{GREEN}Tum kontroller gecti.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
