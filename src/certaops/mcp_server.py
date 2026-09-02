"""CertaOps tool'larini MCP uzerinden acan sunucu cephesi.

Neden var
---------
CertaOps normalde **kendi agent dongusunu** calistirir: sistem prompt, pack
router, model cagrisi, policy kapisi, tool, DLP, gecmis. Bu, konusma duzeyi
kontrolleri (sema butcesi, muhakeme kademesi, egress DLP) mumkun kilar.

MCP'de dongu ISTEMCININDIR (Claude Desktop, VS Code, Cursor...). Sunucu
yalnizca "su tool'u su argumanlarla calistir" cagrisini gorur. Bu cephe o
sinirin dogru tarafina ne konabiliyorsa onu koyar: her cagri yine
`execute_tool`'dan gecer, yani yetki, risk, onay, idempotency, DLP ve audit
aynen isler.

Ne KORUNUR (cagri duzeyi)
    - RBAC/ABAC ve organizasyon kapsami
    - risk kademesi ve onay zorunlulugu (R3 onaysiz yazamaz)
    - idempotency ve mutabakat
    - alan politikasi + DLP (sonuc govdesi uzerinde)
    - denetim defteri ve kanit
    - operator kapatma anahtari (AGENT_DISABLED_TOOLS)

Ne KORUNAMAZ (dongu istemcide oldugu icin)
    - pack router: modele hangi tool'larin gosterilecegine istemci karar verir
    - muhakeme kademesi: `thinking_level` istemcinin model ayaridir
    - konusma gecmisi uzerinde DLP: gecmis istemcide tutulur
    - tur bazli sema/sonuc butceleri
    - direct-answer kisayolu: model zaten cagrilmis olur

Kimlik
------
stdio tasimasinda istek basina kimlik dogrulama YOKTUR. Actor yapilandirmadan
gelir (`AGENT_LOCAL_SUBJECT`, `AGENT_LOCAL_ROLES`) - yani sunucuyu calistiran
kisi kimse, tum cagrilar onun adina yapilir. Cok kullanicili kurulum icin bu
cephe degil, kimlik dogrulayan HTTP API (`certaops.api`) kullanilmalidir.

Yazma
-----
Urun varsayilan olarak `SAP_READ_ONLY=true` ile calisir ve mutating tool'lar
tum cephelerde kapalidir. Gelecekteki write paketini gelistirme ortaminda
sinamak icin once `SAP_READ_ONLY=false`, MCP'de ayrica:

    CERTAOPS_MCP_ALLOW_WRITE=1

Bu bayrak yalniz tool'u GORUNUR kilar; yazmanin kendisi hala `SAP_DRY_RUN`,
onay kaydi ve idempotency kapilarina tabidir.

Kullanim
--------
    pip install -e ".[mcp]"
    certaops-mcp

Claude Desktop yapilandirmasi:

    {
      "mcpServers": {
        "certaops": {
          "command": "certaops-mcp",
          "env": {"SAP_BACKEND": "mock", "AGENT_LOCAL_SUBJECT": "ali@firma.test"}
        }
      }
    }
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from robotics_agent.config import get_settings, setup_logging
from robotics_agent.contracts import ActorContext
from robotics_agent.core.router import PACKS, domains_for_packs
from robotics_agent.sap import build_backend
from robotics_agent.tools import (
    ToolContext,
    execute_tool,
    load_all_tools,
    visible_tool_names,
)
from robotics_agent.tools.registry import REGISTRY

log = logging.getLogger(__name__)

SERVER_NAME = "certaops"

#: MCP istemcisine bildirilen kullanim notu. Yonetisimin nerede bittigi
#: gizlenmez: istemci bunu kullaniciya gosterebilir.
INSTRUCTIONS = (
    "SAP S/4HANA operasyon tool'lari. Her cagri policy, risk, tool'a ozel onay, "
    "idempotency, DLP ve audit kapilarindan gecer. Yan etkili tool'lar varsayilan "
    "olarak kapalidir. Konusma duzeyi kontrolleri (pack router, muhakeme "
    "kademesi, gecmis uzerinde DLP) bu cephede YOKTUR; onlar CertaOps'un kendi "
    "runtime'inda calisir."
)

# SAP'a yazmasa da yerel dosya olusturan tool, salt-okunur bir MCP cephesinde
# yan etkidir. Kullanici yazmayi acikca acana kadar bildirilemez/cagrilamaz.
_SIDE_EFFECT_TOOLS = frozenset({"sap_generate_report"})


def write_enabled() -> bool:
    """MCP cephesinin yan etkili tool'lari gostermesine izin var mi?"""
    return os.getenv("CERTAOPS_MCP_ALLOW_WRITE", "").strip().lower() in {"1", "true", "yes"}


def configured_actor(settings: Any) -> ActorContext:
    """Yapilandirmadan gelen tek kimlik.

    stdio'da istek basina kimlik yoktur; bu yuzden actor sabittir ve
    `AGENT_LOCAL_*` degerlerinden kurulur.
    """
    return ActorContext.local_operator(
        subject=settings.agent.local_subject,
        tenant=settings.sap.tenant,
        roles=settings.agent.local_roles,
        company_code=settings.sap.company_code,
        plant=settings.sap.plant,
        purchasing_org=settings.sap.purch_org,
    )


def exposed_tool_names(settings: Any, actor: ActorContext) -> list[str]:
    """MCP istemcisine bildirilecek tool adlari.

    Uc filtre uygulanir:
      1. actor'un yetkisi (`visible_tool_names` - kapatilanlari da eler),
      2. global read-only profil veya MCP yazma bayragi kapaliysa mutating
         tool'lar; MCP bayragi kapaliysa yerel dosya olusturan tool'lar,
      3. kayit defterinde olmayanlar.
    """
    all_domains = domains_for_packs(tuple(PACKS))
    names = visible_tool_names(all_domains, actor, settings=settings)
    if not write_enabled():
        names = [
            n for n in names
            if not REGISTRY[n].risk_tier.is_mutating and n not in _SIDE_EFFECT_TOOLS
        ]
    return sorted(names)


def build_context(settings: Any, actor: ActorContext) -> ToolContext:
    return ToolContext(settings=settings, sap=build_backend(settings), actor=actor)


def run_tool(name: str, arguments: dict[str, Any], ctx: ToolContext) -> tuple[str, bool]:
    """Tek bir tool cagrisi. Donus: (json_govde, hata_mi).

    Ayri fonksiyon olmasi kasitli: MCP tasimasi olmadan da test edilebilir.
    """
    if name not in REGISTRY:
        return json.dumps(
            {"error": f"Bilinmeyen tool: {name}", "denial_code": "UNKNOWN_TOOL"},
            ensure_ascii=False,
        ), True
    if (REGISTRY[name].risk_tier.is_mutating or name in _SIDE_EFFECT_TOOLS) and not write_enabled():
        # Bildirilmeyen bir tool yine de adiyla cagrilabilir; kapiyi burada da
        # tutariz. Tek katmanli gorunurluk filtresi yeterli degildir.
        return json.dumps(
            {
                "error": (
                    f"{name} yan etkili bir tool ve bu MCP cephesinde kapali. "
                    "Gelecekteki gelistirme profili icin CERTAOPS_MCP_ALLOW_WRITE=1; "
                    "SAP mutasyonu icin ayrica SAP_READ_ONLY=false gerekir."
                ),
                "denial_code": "WRITE_DISABLED_ON_MCP",
            },
            ensure_ascii=False,
        ), True
    return execute_tool(name, arguments, ctx)


def run_isolated_tool(
    settings: Any,
    actor: ActorContext,
    name: str,
    arguments: dict[str, Any],
) -> tuple[str, bool]:
    """Her MCP cagrisi icin bagimsiz, kapatilabilir bir baglam kullanir.

    ``ToolContext`` ve SAP adapteri aktif actor/profil, karar ve sayac tutar.
    Bunlari paralel MCP cagrilari arasinda paylastirmak kimlik/audit yarisi
    yaratir. Baglanti havuzundan vazgecmek pahasina izolasyonu tercih ederiz.
    """
    ctx = build_context(settings, actor)
    try:
        return run_tool(name, arguments, ctx)
    finally:
        close = getattr(ctx.sap, "close", None)
        if callable(close):
            close()


async def serve() -> None:  # pragma: no cover - tasima katmani
    """stdio uzerinden MCP sunucusunu calistirir."""
    import anyio
    import mcp.types as types
    from mcp.server import Server
    from mcp.server.stdio import stdio_server

    setup_logging()
    # stdio tasimasinda STDOUT saf JSON-RPC'dir: oraya dusen tek bir log satiri
    # protokolu bozar. `setup_logging` bugun stderr kullaniyor ama bu varsayilan
    # bir gun degisirse hata sessiz ve teshisi zor olur - burada acikca sabitleriz.
    for handler in logging.getLogger().handlers:
        stream = getattr(handler, "stream", None)
        if stream is sys.stdout:
            handler.stream = sys.stderr  # type: ignore[attr-defined]

    load_all_tools()
    settings = get_settings()
    settings.ensure_dirs()
    actor = configured_actor(settings)
    exposed = exposed_tool_names(settings, actor)
    log.info(
        "CertaOps MCP | actor=%s | backend=%s | tool=%d | yazma=%s",
        actor.subject, settings.sap.backend, len(exposed),
        "acik" if write_enabled() else "kapali",
    )

    server: Server = Server(SERVER_NAME, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=n,
                description=REGISTRY[n].description,
                inputSchema=dict(REGISTRY[n].input_schema),
            )
            for n in exposed_tool_names(settings, actor)
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any] | None):
        # `execute_tool` senkron ve SAP cagrisinda bloklar; event loop'u
        # tikamamak icin ayri bir is parcaciginda calistirilir.
        payload, is_error = await anyio.to_thread.run_sync(
            run_isolated_tool, settings, actor, name, dict(arguments or {})
        )
        if is_error:
            # MCP sozlesmesi: hata govdesi de icerik olarak doner, istisna
            # firlatilmaz. Model hatayi gorup duzeltebilmelidir.
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=payload)], isError=True
            )
        return [types.TextContent(type="text", text=payload)]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    """Giris noktasi (`certaops-mcp`)."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--list", action="store_true",
        help="Bildirilecek tool'lari yazdirip cik (tasima baslatilmaz).",
    )
    args = parser.parse_args(argv)

    if args.list:
        load_all_tools()
        settings = get_settings()
        actor = configured_actor(settings)
        names = exposed_tool_names(settings, actor)
        print(f"actor  : {actor.subject} ({', '.join(actor.roles)})")
        print(f"backend: {settings.sap.backend}")
        print(f"yazma  : {'acik' if write_enabled() else 'kapali'}")
        print(f"tool   : {len(names)}")
        for n in names:
            spec = REGISTRY[n]
            print(f"  {spec.risk_tier.value} {n}")
        return 0

    try:
        import anyio
        import mcp  # noqa: F401 -- tasima extra'sinin kurulu oldugunu erken dogrula
    except ImportError:  # pragma: no cover
        print("MCP cephesi icin: pip install -e '.[mcp]'", file=sys.stderr)
        return 2
    anyio.run(serve)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
