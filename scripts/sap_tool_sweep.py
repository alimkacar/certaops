#!/usr/bin/env python3
"""Bagli bir SAP sistemine karsi TUM tool'lari sirayla calistirip rapor uretir.

Neden gerekli
-------------
`check_real_sap.py` servis **kontratini** dogrular: alan var mi, entity set var
mi. Ama bir alanin var olmasi, o alani okuyan tool'un calistigi anlamina
gelmez. Mapping, birim donusumu, tarih ayristirma, bos-deger davranisi ve
sonuc butcesi ancak tool gercekten calistirilinca gorunur.

Bu script her tool'u `execute_tool` uzerinden calistirir - yani politika, risk,
onay, idempotency, DLP, sonuc butcesi ve audit dahil **tam yigin** ile. Adapter'i
tek basina cagirmaz; cunku uretimde de kimse adapter'i tek basina cagirmayacak.

Guvenlik
--------
Varsayilan olarak HICBIR SEY YAZMAZ. R3/R4 tool'lar ancak su ucu birden
saglandiginda calisir:

    --allow-write  bayragi  +  SAP_DRY_RUN=false  +  SAP_INTEGRATION_ALLOW_WRITE=1

Uc kapinin da ayri ayri acilmasi kasitlidir: yanlislikla acilan tek bir bayrak
gercek bir belge olusturamaz.

Kullanim
--------
    # 1) Salt okunur tarama (once bunu calistirin)
    SAP_BACKEND=odata SAP_BASE_URL=... SAP_ALLOWED_HOSTS=... \\
    python scripts/sap_tool_sweep.py --out runs/sweep-01.json

    # 2) Cekirdek degerleri elle vermek (kesif atlanir, daha hizli ve kararli)
    python scripts/sap_tool_sweep.py --material 100000 --vendor 10300001

    # 3) Yetki reddini de dogrula (VIEWER yazma tool'unu gormemelidir)
    python scripts/sap_tool_sweep.py --negative

    # 4) Yazma dahil (DIKKAT: gercek belge olusur)
    SAP_DRY_RUN=false SAP_INTEGRATION_ALLOW_WRITE=1 \\
    python scripts/sap_tool_sweep.py --allow-write

Cikti nasil okunur
------------------
  OK          tool calisti ve veri dondurdu
  BOS         tool calisti ama veri yok  -> cekirdek deger yanlis olabilir
  RED         politika/yetki engelledi   -> beklenen olabilir (--negative)
  ONAY        R3 onay kaydi istiyor      -> beklenen
  SAP HATA    SAP hata dondurdu          -> mapping/servis sorunu
  ATLANDI     cekirdek deger yok ya da yazma kapali
  COKTU       beklenmeyen istisna        -> kod hatasi

Cikis kodlari: 0 temiz, 1 (--strict ile) SAP HATA/COKTU var, 2 konfigurasyon eksik.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import date, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

GREEN, RED, YELLOW, BLUE, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[2m", "\033[1m", "\033[0m"
)

OK, EMPTY, DENIED, APPROVAL, SAP_ERR, SKIPPED, CRASH = (
    "OK", "BOS", "RED", "ONAY", "SAP HATA", "ATLANDI", "COKTU"
)
_COLOUR = {
    OK: GREEN, EMPTY: YELLOW, DENIED: BLUE, APPROVAL: BLUE,
    SAP_ERR: RED, SKIPPED: DIM, CRASH: RED,
}
# --strict altinda kapiyi kapatan durumlar
_FATAL = {SAP_ERR, CRASH}


# --- JSON icinde deger avlama ---------------------------------------------
def _first(obj: Any, *keys: str) -> str:
    """Ic ice JSON'da verilen anahtarlardan ilk bos olmayan degeri bulur.

    Sonuc semasi surumden surume degisebildigi icin sabit yol yerine arama
    kullanilir: kirilgan olmasin.
    """
    stack = [obj]
    while stack:
        node = stack.pop(0)
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, (str, int)) and str(value).strip():
                    return str(value).strip()
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return ""


def _has_data(payload: Any) -> bool:
    """Sonuc gercekten veri tasiyor mu? Bos liste/None veri sayilmaz."""
    if isinstance(payload, dict):
        ignored = {"status", "message", "detail", "correlation_id", "evidence_id",
                   "truncated", "sap_calls", "tool", "notes"}
        for key, value in payload.items():
            if key in ignored:
                continue
            if isinstance(value, (list, dict)) and value:
                return True
            if isinstance(value, (int, float)) and value:
                return True
            if isinstance(value, str) and value.strip():
                return True
    return bool(payload) and not isinstance(payload, dict)


# --- Cekirdek degerlerin kesfi --------------------------------------------
class Seeds:
    """Tool'lara verilecek gercek is anahtarlari.

    Once CLI/ortam degiskeni, sonra sistemden kesif. Kesif basarisiz olursa
    ilgili tool ATLANDI olarak isaretlenir - uydurma deger ile SAP'a gidilmez.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.material = args.material or os.getenv("SAP_INTEGRATION_MATERIAL", "")
        self.vendor = args.vendor or os.getenv("SAP_INTEGRATION_VENDOR", "")
        self.po = args.po or os.getenv("SAP_INTEGRATION_PO", "")
        self.invoice = args.invoice or os.getenv("SAP_INTEGRATION_INVOICE", "")
        self.wbs = args.wbs or os.getenv("SAP_INTEGRATION_WBS", "")
        self.evidence_id = ""
        self.discovery: list[str] = []

    def discover(self, run) -> None:
        if not self.material:
            payload, err = run("sap_search_materials", {"limit": 5})
            if not err:
                self.material = _first(payload, "material_id", "material", "Product")
                self.discovery.append(f"material <- sap_search_materials: {self.material or 'YOK'}")
        if not self.vendor and self.material:
            payload, err = run("sap_compare_vendors", {"material_id": self.material, "quantity": 1})
            if not err:
                self.vendor = _first(payload, "vendor_id", "supplier", "Supplier")
                self.discovery.append(f"vendor <- sap_compare_vendors: {self.vendor or 'YOK'}")
        if not self.po:
            payload, err = run("sap_track_purchase_orders", {"only_open": False})
            if not err:
                self.po = _first(payload, "po_id", "purchase_order", "PurchaseOrder")
                self.discovery.append(f"po <- sap_track_purchase_orders: {self.po or 'YOK'}")
        if not self.invoice:
            # Bu tool en az bir filtre ister; po varsa onu, yoksa only_blocked kullan.
            filters = {"po_id": self.po} if self.po else {"only_blocked": True}
            payload, err = run("sap_supplier_invoice_status", {**filters, "limit": 5})
            if not err:
                self.invoice = _first(payload, "invoice_id", "supplier_invoice", "SupplierInvoice")
                self.discovery.append(f"invoice <- sap_supplier_invoice_status: {self.invoice or 'YOK'}")
        if not self.wbs:
            payload, err = run("sap_project_cost_status", {})
            if not err:
                self.wbs = _first(payload, "wbs_element", "wbs", "WBSElement")
                self.discovery.append(f"wbs <- sap_project_cost_status: {self.wbs or 'YOK'}")

    def as_dict(self) -> dict[str, str]:
        return {"material": self.material, "vendor": self.vendor, "po": self.po,
                "invoice": self.invoice, "wbs": self.wbs}


# --- Tool -> arguman uretimi ----------------------------------------------
def build_arguments(name: str, seeds: Seeds) -> dict[str, Any] | None:
    """Tool icin argumanlari uretir. None donerse tool ATLANDI olur."""
    soon = (date.today() + timedelta(days=45)).isoformat()
    material, vendor, po = seeds.material, seeds.vendor, seeds.po
    items = [{"material_id": material, "quantity": 1, "delivery_date": soon}]

    table: dict[str, Any] = {
        # --- cekirdek deger gerektirmeyenler ---
        "sap_connection_health": {},
        "sap_discover_capabilities": {"probe": True},
        "sap_list_domains": {},
        "sap_get_execution_audit": {"limit": 5, "verify_chain": True},
        "sap_reconcile_execution": {"list_pending": True},
        "sap_search_materials": {"limit": 5},
        "sap_explain_authorization_failure": {
            "http_status": 403, "sap_message": "No authorization for plant",
            "target_api": "API_PRODUCT_SRV",
        },
        # --- material gerektirenler ---
        "sap_material_360": {"material_id": material} if material else None,
        "sap_stock_overview": {"material_ids": [material]} if material else None,
        "sap_atp_check": (
            {"requests": [{"material_id": material, "quantity": 1, "required_date": soon}]}
            if material else None
        ),
        "sap_mrp_shortage_explain": {"material_id": material} if material else None,
        "sap_compare_vendors": (
            {"material_id": material, "quantity": 1, "required_date": soon} if material else None
        ),
        "sap_track_purchase_orders": {"material_id": material} if material else {},
        # --- vendor / po / invoice / wbs ---
        "sap_supplier_score_360": {"vendor_ids": [vendor]} if vendor else None,
        "sap_purchase_order_360": {"po_id": po} if po else None,
        "sap_document_flow": {"document_id": po, "document_type": "purchase_order"} if po else None,
        "sap_workflow_status": (
            {"object_type": "purchase_order", "object_id": po} if po else None
        ),
        # En az bir filtre zorunlu (FILTER_REQUIRED): po varsa onu kullan.
        "sap_supplier_invoice_status": (
            {"po_id": po, "limit": 5} if po else {"only_blocked": True, "limit": 5}
        ),
        "sap_invoice_block_explain": {"invoice_id": seeds.invoice} if seeds.invoice else None,
        "sap_project_cost_status": {"wbs_element": seeds.wbs} if seeds.wbs else {},
        "get_evidence": {"evidence_id": seeds.evidence_id} if seeds.evidence_id else None,
        # --- hesap / taslak (yazma yok) ---
        "sap_pr_prepare": (
            {"items": items, "header_text": "SWEEP PREPARE"} if material else None
        ),
        "sap_generate_report": {
            "title": "Tool Sweep", "format": "markdown",
            "sections": [{"heading": "Kapsam", "body": "Otomatik tarama ciktisi."}],
        },
        # --- YAZMA ---
        "sap_pr_submit": (
            {"items": items, "header_text": "SWEEP SUBMIT",
             "idempotency_key": f"sweep:{date.today().isoformat()}:"
                                f"{os.getenv('BUILD_ID', 'local')}:v1"}
            if material else None
        ),
    }
    return table.get(name, {})


# --- Tek tool calistirma ---------------------------------------------------
def classify(payload: Any, is_error: bool) -> str:
    if not is_error:
        return OK if _has_data(payload) else EMPTY
    blob = json.dumps(payload, ensure_ascii=False).upper() if payload else ""
    if any(k in blob for k in ("MISSING_SCOPE", "FORBIDDEN", "DENIED", "ORG_SCOPE", "TENANT")):
        return DENIED
    if any(k in blob for k in ("APPROVAL", "ONAY")):
        return APPROVAL
    return SAP_ERR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--material")
    parser.add_argument("--vendor")
    parser.add_argument("--po")
    parser.add_argument("--invoice")
    parser.add_argument("--wbs")
    parser.add_argument("--only", action="append", help="Yalniz bu tool (tekrarlanabilir).")
    parser.add_argument("--no-discover", action="store_true",
                        help="Kesif adimini atla (cekirdek degerleri elle verdiyseniz).")
    parser.add_argument("--allow-write", action="store_true",
                        help="R3/R4 tool'lari da calistir (uc kapidan biri).")
    parser.add_argument("--negative", action="store_true",
                        help="VIEWER rolu ile de tara: yazma RED olmali.")
    parser.add_argument("--roles", default="PURCHASER,AUDITOR")
    parser.add_argument("--out", help="Raporu bu JSON dosyasina yaz.")
    parser.add_argument("--json", action="store_true", help="Raporu stdout'a JSON bas.")
    parser.add_argument("--strict", action="store_true", help="SAP HATA/COKTU varsa exit 1.")
    args = parser.parse_args(argv)

    from robotics_agent.config import get_settings
    from robotics_agent.contracts import ActorContext
    from robotics_agent.sap import build_backend
    from robotics_agent.tools import ToolContext, execute_tool, load_all_tools
    from robotics_agent.tools.registry import REGISTRY

    load_all_tools()
    settings = get_settings()
    settings.ensure_dirs()

    problems = settings.sap.validate()
    if problems:
        print(f"{RED}SAP konfigurasyonu eksik:{RESET}")
        for problem in problems:
            print(f"  - {problem}")
        return 2

    write_gates = {
        "--allow-write": args.allow_write,
        "SAP_DRY_RUN=false": not settings.sap.dry_run,
        "SAP_INTEGRATION_ALLOW_WRITE=1": os.getenv("SAP_INTEGRATION_ALLOW_WRITE") == "1",
    }
    writes_enabled = all(write_gates.values())

    backend = build_backend(settings)

    def make_ctx(roles: tuple[str, ...]) -> ToolContext:
        actor = ActorContext.local_operator(
            subject="tool-sweep", tenant=settings.sap.tenant, roles=roles,
            company_code=settings.sap.company_code, plant=settings.sap.plant,
            purchasing_org=settings.sap.purch_org,
        )
        return ToolContext(settings=settings, sap=backend, actor=actor)

    ctx = make_ctx(tuple(r.strip() for r in args.roles.split(",") if r.strip()))

    def run(name: str, arguments: dict[str, Any]) -> tuple[Any, bool]:
        raw, is_error = execute_tool(name, arguments, ctx)
        try:
            return json.loads(raw), is_error
        except (TypeError, ValueError):
            return {"raw": str(raw)[:500]}, is_error

    # --- Kesif ---
    seeds = Seeds(args)
    if not args.no_discover:
        seeds.discover(run)

    # --- Tarama ---
    # get_evidence baska bir tool'un urettigi evidence_id'yi ister; en sona alinir.
    names = args.only or sorted(REGISTRY, key=lambda n: (n == "get_evidence", n))
    rows: list[dict[str, Any]] = []

    for name in names:
        spec = REGISTRY.get(name)
        if spec is None:
            rows.append({"tool": name, "status": SKIPPED, "note": "kayitli degil"})
            continue

        mutating = spec.risk_tier.is_mutating
        if mutating and not writes_enabled:
            closed = [g for g, open_ in write_gates.items() if not open_]
            rows.append({"tool": name, "risk": spec.risk_tier.value, "status": SKIPPED,
                         "note": "yazma kapali: " + ", ".join(closed)})
            continue

        arguments = build_arguments(name, seeds)
        if arguments is None:
            rows.append({"tool": name, "risk": spec.risk_tier.value, "status": SKIPPED,
                         "note": "gerekli cekirdek deger bulunamadi"})
            continue

        before = getattr(ctx, "sap_call_count", 0)
        started = time.perf_counter()
        try:
            payload, is_error = run(name, arguments)
            status = classify(payload, is_error)
            note = "" if not is_error else json.dumps(payload, ensure_ascii=False)[:220]
        except Exception:
            status, note, payload = CRASH, traceback.format_exc().strip().splitlines()[-1], None
        elapsed = time.perf_counter() - started

        # Butce nedeniyle kirpilan herhangi bir sonuc evidence_id uretir; ilkini sakla.
        if status in (OK, EMPTY) and not seeds.evidence_id:
            seeds.evidence_id = _first(payload, "evidence_id")

        rows.append({
            "tool": name, "risk": spec.risk_tier.value, "status": status,
            "ms": round(elapsed * 1000), "note": note,
            "sap_calls": max(0, getattr(ctx, "sap_call_count", 0) - before),
            "arguments": arguments,
        })

    # --- Negatif tarama: VIEWER yazmayi gorememeli ---
    negative: list[dict[str, Any]] = []
    if args.negative:
        viewer_ctx = make_ctx(("VIEWER",))
        for name in [n for n, s in REGISTRY.items() if s.risk_tier.is_mutating]:
            raw, is_error = execute_tool(
                name, build_arguments(name, seeds) or {}, viewer_ctx
            )
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                payload = {"raw": str(raw)[:200]}
            status = classify(payload, is_error)
            negative.append({
                "tool": name, "status": status,
                "expected": DENIED, "pass": status == DENIED,
                "note": json.dumps(payload, ensure_ascii=False)[:200],
            })

    report = {
        "connection": backend.connection.describe() if hasattr(backend, "connection") else {},
        "dry_run": settings.sap.dry_run,
        "writes_enabled": writes_enabled,
        "seeds": seeds.as_dict(),
        "discovery": seeds.discovery,
        "results": rows,
        "negative": negative,
    }
    backend.close()

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report, seeds)

    fatal = [r for r in rows if r["status"] in _FATAL]
    fatal += [n for n in negative if not n["pass"]]
    if args.out:
        print(f"\n{DIM}Rapor yazildi: {args.out}{RESET}")
    return 1 if (args.strict and fatal) else 0


def _print_human(report: dict[str, Any], seeds: Seeds) -> None:
    connection = report.get("connection") or {}
    print(f"{BOLD}Hedef{RESET}  {connection.get('base_url', '?')}  "
          f"({connection.get('auth', '?')})   dry_run={report['dry_run']}  "
          f"yazma={'ACIK' if report['writes_enabled'] else 'kapali'}")
    print(f"{BOLD}Cekirdek{RESET} " + "  ".join(
        f"{k}={v or DIM + 'yok' + RESET}" for k, v in report["seeds"].items()))
    for line in report["discovery"]:
        print(f"  {DIM}{line}{RESET}")

    print(f"\n{BOLD}Tool taramasi{RESET}")
    print("  " + "-" * 72)
    for row in report["results"]:
        colour = _COLOUR.get(row["status"], "")
        ms = f"{row.get('ms', 0):>5} ms" if "ms" in row else " " * 8
        # NOT: ToolContext.sap_call_count su an hicbir yerde artmiyor (olu sayac).
        # Yaniltmamak icin yalniz sifirdan buyukse basilir; sayac duzelince kendiliginden gorunur.
        calls = f"{row['sap_calls']} cagri" if row.get("sap_calls") else ""
        print(f"  [{colour}{row['status']:<8}{RESET}] {row['tool']:<34} "
              f"{row.get('risk', ''):<3} {ms} {DIM}{calls}{RESET}")
        if row.get("note"):
            print(f"      {DIM}{row['note'][:160]}{RESET}")

    tally: dict[str, int] = {}
    for row in report["results"]:
        tally[row["status"]] = tally.get(row["status"], 0) + 1
    print("\n  " + "  ".join(
        f"{_COLOUR.get(k, '')}{k}={v}{RESET}" for k, v in sorted(tally.items())))

    if report["negative"]:
        print(f"\n{BOLD}Negatif tarama (VIEWER){RESET}")
        print("  " + "-" * 72)
        for row in report["negative"]:
            mark = f"{GREEN}GECTI{RESET}" if row["pass"] else f"{RED}KALDI{RESET}"
            print(f"  [{mark}] {row['tool']:<34} durum={row['status']} (beklenen RED)")

    fatal = [r for r in report["results"] if r["status"] in _FATAL]
    if fatal:
        print(f"\n{DIM}Sonraki adim: SAP HATA satirlari icin "
              f"src/robotics_agent/adapters/sap/capabilities.py manifestini ve "
              f"src/robotics_agent/sap/odata.py mapping'ini karsilastirin. "
              f"BOS satirlar genellikle yanlis cekirdek degerdir, kod hatasi degil.{RESET}")


if __name__ == "__main__":
    raise SystemExit(main())
