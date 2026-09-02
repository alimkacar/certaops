"""MCP cephesi icin sir sizdirmayan durum ve stdio baglanti testi.

Bu modul web arayuzune MCP'yi *yonetim motoru* olarak eklemez. Arayuz HTTP
API'yi kullanmaya devam eder; buradaki kod yalnizca ayri bir MCP istemcisinin
baglanacagi yerel stdio sunucusunu tarif eder ve ``initialize + tools/list``
el sikismasini gercek bir alt surecle dogrular.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from datetime import timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from robotics_agent.tools import load_all_tools
from robotics_agent.tools.registry import REGISTRY

from .mcp_server import (
    SERVER_NAME,
    configured_actor,
    exposed_tool_names,
    write_enabled,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class MCPProbeError(RuntimeError):
    """Kullaniciya guvenle gosterilebilen MCP teshis hatasi."""


def _sdk_version() -> str | None:
    if importlib.util.find_spec("mcp") is None:
        return None
    try:
        return version("mcp")
    except PackageNotFoundError:  # pragma: no cover - bozuk/gelistirme kurulumu
        return "installed"


def _command() -> tuple[str, list[str]]:
    """Kurulu entrypoint'i, yoksa ayni Python yorumlayicisini kullan."""
    entrypoint = Path(sys.executable).with_name("certaops-mcp")
    if entrypoint.is_file():
        return str(entrypoint), []
    return sys.executable, ["-m", "certaops.mcp_server"]


def _safe_client_env(settings: Any) -> dict[str, str]:
    """Kopyalanabilir istemci ornegi; kimlik bilgileri kasitli olarak yoktur."""
    return {
        # baslat.command API'yi --app-dir ile kaynak agacindan acar. MCP
        # istemcisi de ayni kodu kullanmali; sanal ortamdaki daha eski bir
        # wheel kopyasina sessizce dusmemesi icin kaynak yolu acik yazilir.
        "PYTHONPATH": str(PROJECT_ROOT / "src"),
        "SAP_BACKEND": settings.sap.backend,
        "SAP_SYSTEM_ALIAS": settings.sap.system_alias,
        "SAP_TENANT": settings.sap.tenant,
        "SAP_COMPANY_CODE": settings.sap.company_code,
        "SAP_PLANT": settings.sap.plant,
        "SAP_PURCH_ORG": settings.sap.purch_org,
        "AGENT_LOCAL_SUBJECT": settings.agent.local_subject,
        "AGENT_LOCAL_ROLES": ",".join(settings.agent.local_roles),
        # Web arayuzunden uretilen ornek daima guvenli baslar. Bu, calisan MCP
        # surecinin ayarini degistirmez; yalniz kopyalanan istemci tanimidir.
        "SAP_READ_ONLY": "true",
        "SAP_DRY_RUN": "true",
        "CERTAOPS_MCP_ALLOW_WRITE": "0",
    }


def status_snapshot(settings: Any) -> dict[str, Any]:
    """Mevcut MCP durusunu ve sir icermeyen istemci yapilandirmasini don."""
    load_all_tools()
    actor = configured_actor(settings)
    names = exposed_tool_names(settings, actor)
    mutating = [name for name in names if REGISTRY[name].risk_tier.is_mutating]
    command, args = _command()
    sdk = _sdk_version()

    return {
        "installed": sdk is not None,
        "sdk_version": sdk,
        "server_name": SERVER_NAME,
        "transport": "stdio",
        "ui_channel": "http_api",
        "ui_uses_mcp": False,
        "identity": {
            "mode": "process_static",
            "subject": actor.subject,
            "tenant": actor.tenant,
            "roles": list(actor.roles),
        },
        "sap": {
            "backend": settings.sap.backend,
            "system_alias": settings.sap.system_alias,
            "mode": "simulation" if settings.sap.backend == "mock" else "live",
            "read_only": settings.sap.read_only,
            "dry_run": settings.sap.dry_run,
        },
        "security": {
            "mcp_write_flag": write_enabled(),
            "write_tools_exposed": bool(mutating),
            "secrets_in_response": False,
            "probe_forces_read_only": True,
        },
        "tools": {
            "count": len(names),
            "names": names,
            "mutating": mutating,
        },
        "client_config": {
            "mcpServers": {
                SERVER_NAME: {
                    "command": command,
                    "args": args,
                    "cwd": str(PROJECT_ROOT),
                    "env": _safe_client_env(settings),
                }
            }
        },
        "notes": [
            "Web arayuzu MCP uzerinden degil, kimlik dogrulayan HTTP API uzerinden calisir.",
            "stdio tasimasinda istek basina kimlik yoktur; cagrilar yapilandirilan actor adina kaydolur.",
            "Kopyalanabilir istemci tanimi SAP parolasi, API anahtari veya OAuth sirri icermez.",
        ],
    }


async def probe_stdio(settings: Any, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
    """Guvenli bir alt surecte gercek MCP initialize ve tools/list yap."""
    if _sdk_version() is None:
        raise MCPProbeError("MCP paketi kurulu degil; proje '.[mcp]' extra'siyla kurulmalı.")

    try:
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # pragma: no cover - _sdk_version ile ayni kapi
        raise MCPProbeError("MCP istemci kutuphanesi yuklenemedi.") from exc

    env = dict(os.environ)
    env.update(_safe_client_env(settings))
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "SAP_READ_ONLY": "true",
            "SAP_DRY_RUN": "true",
            "SAP_INTEGRATION_ALLOW_WRITE": "0",
            "CERTAOPS_MCP_ALLOW_WRITE": "0",
        }
    )
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "certaops.mcp_server"],
        cwd=PROJECT_ROOT,
        env=env,
    )

    started = time.perf_counter()
    try:
        with anyio.fail_after(timeout_seconds):
            async with (
                stdio_client(server) as (read_stream, write_stream),
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=timeout_seconds),
                ) as session,
            ):
                initialized = await session.initialize()
                listed = await session.list_tools()
    except TimeoutError as exc:
        raise MCPProbeError(f"MCP stdio el sikismasi {timeout_seconds:g} saniyede tamamlanmadi.") from exc
    except MCPProbeError:
        raise
    except Exception as exc:
        # Hata metni ortam degiskeni, URL veya credential tasiyabilir. API'ye
        # yalniz sinif adi ve sabit aciklama cikarilir; ayrinti log/trace'tedir.
        raise MCPProbeError(
            f"MCP stdio el sikismasi basarisiz ({type(exc).__name__})."
        ) from exc

    names = sorted(tool.name for tool in listed.tools)
    write_names = [name for name in names if name in REGISTRY and REGISTRY[name].risk_tier.is_mutating]
    server_info = initialized.serverInfo
    return {
        "ok": True,
        "server_name": server_info.name,
        "server_version": server_info.version,
        "protocol_version": initialized.protocolVersion,
        "transport": "stdio",
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "tool_count": len(names),
        "tools": names,
        "write_tools_exposed": bool(write_names),
        "write_tools": write_names,
        "read_only_forced": True,
        "sap_calls": 0,
    }
