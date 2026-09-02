#!/usr/bin/env python3
"""SAP API Business Hub salt-okunur MCP kabul testi.

Aktif `.env` profilindeki Hub baglantisini kullanir; MCP sunucusunu stdio ile
baslatir, 18 Hub-uyumlu tool'un tamamini gercek protokol uzerinden cagirir ve
yazma/yan-etki tool'larinin yayimlanmadigini dogrular.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "get_evidence",
    "sap_compare_vendors",
    "sap_connection_health",
    "sap_discover_capabilities",
    "sap_document_flow",
    "sap_explain_authorization_failure",
    "sap_list_domains",
    "sap_material_360",
    "sap_mrp_shortage_explain",
    "sap_invoice_block_explain",
    "sap_pr_prepare",
    "sap_purchase_order_360",
    "sap_reconcile_execution",
    "sap_search_materials",
    "sap_stock_overview",
    "sap_supplier_invoice_status",
    "sap_supplier_score_360",
    "sap_track_purchase_orders",
}
FORBIDDEN_TOOLS = {
    "sap_pr_submit",
    "sap_generate_report",
    "sap_get_execution_audit",
}


def _payload(result: Any) -> dict[str, Any]:
    text = "".join(getattr(item, "text", "") for item in result.content)
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {"raw": text[:500]}
    return value if isinstance(value, dict) else {"value": value}


def _find(value: Any, key: str) -> str:
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, (str, int)) and str(candidate).strip():
            return str(candidate).strip()
        for child in value.values():
            if found := _find(child, key):
                return found
    elif isinstance(value, list):
        for child in value:
            if found := _find(child, key):
                return found
    return ""


async def acceptance() -> int:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        print("MCP extra eksik: uv pip install --python .venv/bin/python '.[mcp]'", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "SAP_PURCH_ORG": "1010",
            "SAP_READ_ONLY": "true",
            "SAP_DRY_RUN": "true",
            "SAP_INTEGRATION_ALLOW_WRITE": "0",
            "CERTAOPS_MCP_ALLOW_WRITE": "",
            "AGENT_DISABLED_TOOLS": "sap_pr_submit,sap_generate_report",
        }
    )
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "certaops.mcp_server"],
        cwd=PROJECT_ROOT,
        env=env,
    )
    required_date = (date.today() + timedelta(days=45)).isoformat()
    cases: list[tuple[str, dict[str, Any]]] = [
        ("sap_connection_health", {}),
        ("sap_discover_capabilities", {"probe": True}),
        (
            "sap_explain_authorization_failure",
            {
                "http_status": 403,
                "sap_message": "No authorization for plant",
                "target_api": "API_PRODUCT_SRV",
            },
        ),
        ("sap_list_domains", {"message": "TG10 icin tedarikci ve stok analizi"}),
        ("sap_search_materials", {"query": "TG", "limit": 5}),
        ("sap_material_360", {"material_id": "21"}),
        ("sap_stock_overview", {"material_ids": ["21"]}),
        ("sap_mrp_shortage_explain", {"material_id": "21"}),
        (
            "sap_compare_vendors",
            {
                "material_id": "TG10",
                "quantity": 1,
                "required_date": required_date,
            },
        ),
        ("sap_supplier_score_360", {"vendor_ids": ["USSU_GRIR5"]}),
        ("sap_track_purchase_orders", {"material_id": "21"}),
        ("sap_purchase_order_360", {"po_id": "4500000012", "detail": "full"}),
        (
            "sap_document_flow",
            {
                "document_id": "4500000012",
                "document_type": "purchase_order",
                "detail": "full",
            },
        ),
        (
            "sap_supplier_invoice_status",
            {"invoice_id": "5100000001", "detail": "full"},
        ),
        (
            "sap_invoice_block_explain",
            {"invoice_id": "5100000001", "detail": "full"},
        ),
        (
            "sap_pr_prepare",
            {
                "items": [
                    {
                        "material_id": "TG10",
                        "quantity": 1,
                        "delivery_date": required_date,
                    }
                ],
                "header_text": "API HUB ACCEPTANCE - DRY RUN",
            },
        ),
        ("sap_reconcile_execution", {"list_pending": True}),
    ]

    evidence_id = ""
    passed = 0
    async with (
        stdio_client(server) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        missing = EXPECTED_TOOLS - names
        forbidden = FORBIDDEN_TOOLS & names
        if missing or forbidden:
            print(
                f"KALDI tools/list: eksik={sorted(missing)} yasak={sorted(forbidden)}",
                file=sys.stderr,
            )
            return 1
        print(
            f"[OK] initialize/list | server={initialized.serverInfo.name} "
            f"protocol={initialized.protocolVersion} tools={len(names)}"
        )

        for tool_name, arguments in cases:
            started = time.perf_counter()
            result = await session.call_tool(tool_name, arguments)
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            payload = _payload(result)
            if result.isError or payload.get("error"):
                detail = payload.get("error") or payload.get("raw") or "MCP error"
                print(f"[KALDI] {tool_name:<34} {elapsed_ms:>6} ms | {str(detail)[:180]}")
                continue
            evidence_id = evidence_id or _find(payload, "evidence_id")
            passed += 1
            print(f"[OK]    {tool_name:<34} {elapsed_ms:>6} ms")

        if not evidence_id:
            print("[KALDI] get_evidence: onceki sonuclardan evidence_id uretilmedi")
        else:
            started = time.perf_counter()
            result = await session.call_tool("get_evidence", {"evidence_id": evidence_id})
            elapsed_ms = round((time.perf_counter() - started) * 1000)
            payload = _payload(result)
            if result.isError or payload.get("error"):
                print(f"[KALDI] get_evidence                       {elapsed_ms:>6} ms")
            else:
                passed += 1
                print(f"[OK]    get_evidence                       {elapsed_ms:>6} ms")

    expected = len(EXPECTED_TOOLS)
    print(f"\nSonuc: {passed}/{expected} tool MCP + SAP Hub kabulunden gecti.")
    return 0 if passed == expected else 1


def main() -> int:
    try:
        import anyio
    except ImportError:
        print("MCP extra eksik: uv pip install --python .venv/bin/python '.[mcp]'", file=sys.stderr)
        return 2
    return anyio.run(acceptance)


if __name__ == "__main__":
    raise SystemExit(main())
