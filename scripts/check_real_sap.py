#!/usr/bin/env python3
"""Gercek bir SAP sistemine karsi SALT OKUNUR kontrat dogrulamasi.

Bu script hicbir sey yazmaz. Yaptigi tek sey, manifestteki her servisin
`$metadata` belgesini hedef sistemden okuyup su soruyu cevaplamak:

    "Kodun bekledigi servis, entity set ve alanlar bu sistemde GERCEKTEN var mi?"

Mock testleri bu soruyu cevaplayamaz. Bir alan adinin yanlis olmasi, bir
servisin SICF'te aktif olmamasi ya da surumun V2/V4 farki yalniz gercek
sistemde ortaya cikar - ve genellikle ilk yazma denemesinde, en pahali anda.

Nereye baglanabilirsiniz
------------------------
1. SAP API Business Hub sandbox (ucretsiz, 5 dakika, SALT OKUNUR):

       SAP_BACKEND=odata \\
       SAP_BASE_URL=https://sandbox.api.sap.com/s4hanacloud \\
       SAP_ALLOWED_HOSTS=sandbox.api.sap.com \\
       SAP_AUTH_MODE=apikey SAP_API_KEY=<api.sap.com anahtariniz> \\
       python scripts/check_real_sap.py

2. Kurumunuzun quality/DEV sistemi (okuma yetkili teknik kullanici):

       SAP_BACKEND=odata \\
       SAP_BASE_URL=https://s4q.firma.local:44300 \\
       SAP_ALLOWED_HOSTS=s4q.firma.local \\
       SAP_AUTH_MODE=basic SAP_USERNAME=... SAP_PASSWORD=... \\
       python scripts/check_real_sap.py

Cikti nasil okunur
------------------
  YOK          servis hedef sistemde erisilebilir degil (SICF/yetki/surum).
  ALAN EKSIK   servis var ama kodun bekledigi alan(lar) yok -> mapping duzeltilmeli.
  UYUMLU       kod bu servisi bu sistemde guvenle kullanabilir.

`--json` ile makine okunur cikti verir (CI kapisi olarak kullanilabilir).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="Makine okunur cikti.")
    parser.add_argument("--alias", action="append",
                        help="Yalniz bu servisleri sonda (tekrarlanabilir).")
    parser.add_argument("--strict", action="store_true",
                        help="Herhangi bir servis uyumsuzsa 1 ile cik (CI kapisi).")
    args = parser.parse_args(argv)

    from robotics_agent.adapters.sap import CAPABILITY_MANIFEST
    from robotics_agent.config import Settings
    from robotics_agent.sap import build_backend

    settings = Settings()
    if settings.sap.backend == "mock":
        print(f"{RED}SAP_BACKEND=mock.{RESET} Bu script gercek bir sisteme baglanmak "
              "icindir; SAP_BACKEND=odata (veya ecc) verin.")
        return 2
    problems = settings.sap.validate()
    if problems:
        print(f"{RED}Konfigurasyon eksik:{RESET}")
        for problem in problems:
            print(f"  - {problem}")
        return 2

    backend = build_backend(settings)
    connection = backend.connection.describe()
    # V4 -> V2 secimini raporlayan bu yardimci yalniz S/4 adapterinde var.
    # ECC tek protokol kullandigi icin burada guvenli bir bos deger kullanilir.
    resolved = getattr(backend, "resolved_services", dict)()

    if not args.json:
        print(f"{BOLD}Hedef sistem{RESET}")
        print(f"  URL        : {connection['base_url']}")
        print(f"  Kimlik     : {connection['auth']} ({connection['origin']})")
        print(f"  Client     : {settings.sap.client}")
        for warning in connection.get("warnings", []):
            print(f"  {YELLOW}Uyari{RESET}    : {warning}")

    # Baglanti gercekten kuruluyor mu?
    health = backend.ping()
    if health.get("status") != "ok" and not args.json:
        print(f"\n{RED}Baglanti kurulamadi:{RESET} {health.get('detail', '')}")
        print(f"{DIM}Yine de kontrat sondasi denenecek.{RESET}")

    aliases = args.alias or list(CAPABILITY_MANIFEST)
    results = backend.probe_capabilities(aliases)

    if args.json:
        print(json.dumps({
            "connection": connection,
            "ping": health,
            "services": results,
            "resolved": resolved,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"\n{BOLD}Servis kontratlari{RESET}")
        print("  " + "-" * 60)
        for entry in results:
            alias = entry["alias"]
            capability = CAPABILITY_MANIFEST[alias]
            if not entry.get("available"):
                label, colour = "YOK       ", RED
            elif not entry.get("contract_ok"):
                label, colour = "ALAN EKSIK", YELLOW
            else:
                label, colour = "UYUMLU    ", GREEN
            print(f"  [{colour}{label}{RESET}] {alias:24} "
                  f"{capability.odata_version:3} {capability.status}")
            if entry.get("error"):
                print(f"      {DIM}{entry['error'][:150]}{RESET}")
            for missing in entry.get("missing_entity_sets", []):
                print(f"      {DIM}entity set yok: {missing}{RESET}")
            for entity_set, fields in (entry.get("missing_properties") or {}).items():
                print(f"      {DIM}{entity_set}: eksik alan -> {', '.join(fields)}{RESET}")

        if resolved:
            print(f"\n{BOLD}Fiilen secilen servis surumu{RESET}")
            print("  " + "-" * 60)
            for job, info in resolved.items():
                print(f"  {job:24} -> {info['odata']} ({info['status']}) {info['service']}")

        ok = sum(1 for r in results if r.get("contract_ok"))
        missing = sum(1 for r in results if not r.get("available"))
        partial = len(results) - ok - missing
        print(f"\n{BOLD}Ozet:{RESET} {GREEN}{ok} uyumlu{RESET}, "
              f"{YELLOW}{partial} alan eksik{RESET}, {RED}{missing} yok{RESET} "
              f"({len(results)} servis)")
        if partial or missing:
            print(f"\n{DIM}Sonraki adim: eksik alanlari "
                  f"src/robotics_agent/adapters/sap/capabilities.py icindeki "
                  f"manifest ile karsilastirin. Alan adi farkliysa manifest ve "
                  f"mapping guncellenmelidir; servis hic yoksa SICF'te aktive "
                  f"edilmeli veya fallback kullanilmalidir.{RESET}")

    backend.close()
    if args.strict and any(not r.get("contract_ok") for r in results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
