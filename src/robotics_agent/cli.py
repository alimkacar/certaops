"""Interaktif terminal arayuzu."""

from __future__ import annotations

import argparse
import json
import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .agent import SAPMultiAgent
from .config import get_settings, setup_logging

console = Console()

BANNER = """[bold cyan]CertaOps[/bold cyan]
Ana veri, planlama, tedarik zinciri, satinalma ve proje finans icin izole SAP domain agent'lari.

[dim]Komutlar:[/dim]
  [yellow]/tools[/yellow]            kayitli toollari risk seviyesiyle listele
  [yellow]/health[/yellow]           SAP, model ve yetki durumu
  [yellow]/butce[/yellow]            aktif sema/token butcesi ve telemetri
  [yellow]/audit[/yellow]            son islem kayitlari ve hash zinciri
  [yellow]/reset[/yellow]            konusma gecmisini temizle
  [yellow]/cikis[/yellow]            programi kapat
"""

EXAMPLES = [
    "HD-GEAR-CSF25-100 malzemesinin 360 gorunumunu ve ATP durumunu getir.",
    "HD-GEAR-CSF25-100 icin tedarikcileri TCO bazinda karsilastir, 24 adet, 15 Ekim'e ihtiyacimiz var.",
    "R-2026-014 projesinde butce durumu ne? Asim riski var mi?",
    "SFT-SCN-270 icin satinalma talebi hazirla; yazmadan once diff ve onay durumunu goster.",
]


_RISK_STYLE = {"R0": "green", "R1": "cyan", "R2": "yellow", "R3": "red", "R4": "bold red"}


def _print_tools(agent: SAPMultiAgent) -> None:
    visible = set(agent._visible_names())  # noqa: SLF001 - CLI ayni surecte
    table = Table(title="Kayitli Toollar", header_style="bold cyan", show_lines=False)
    table.add_column("Tool", style="green", no_wrap=True)
    table.add_column("Domain", style="magenta", no_wrap=True)
    table.add_column("Risk", no_wrap=True)
    table.add_column("Onay", no_wrap=True)
    table.add_column("Aktif", no_wrap=True)
    table.add_column("Aciklama")
    for spec in agent.describe_tools():
        risk = spec["risk"]
        table.add_row(
            spec["name"],
            spec["domain"],
            f"[{_RISK_STYLE.get(risk, 'white')}]{risk}[/]",
            spec["approval"] if spec["approval"] != "none" else "-",
            "[green]*[/green]" if spec["name"] in visible else "[dim]-[/dim]",
            spec["description"],
        )
    console.print(table)
    console.print(
        f"[dim]Aktif pack: {', '.join(agent.active_packs)} | "
        f"{len(visible)}/{len(agent.describe_tools())} tool modele gorunur[/dim]"
    )


def _print_budget(agent: SAPMultiAgent) -> None:
    console.print(
        Panel(
            json.dumps(agent.budget_report(), indent=2, ensure_ascii=False),
            title="Token butcesi ve telemetri",
            border_style="cyan",
        )
    )


def _print_audit(agent: SAPMultiAgent, limit: int = 10) -> None:
    ledger = agent.ctx.audit
    if ledger is None:
        console.print("[yellow]Audit defteri yapilandirilmamis.[/yellow]")
        return
    entries = ledger.recent(limit=limit)
    table = Table(title="Son islem kayitlari", header_style="bold cyan")
    table.add_column("Olay", style="green", no_wrap=True)
    table.add_column("Tool", no_wrap=True)
    table.add_column("Risk", no_wrap=True)
    table.add_column("Sonuc", no_wrap=True)
    table.add_column("Zaman", no_wrap=True)
    for row in entries:
        table.add_row(
            row.get("event", ""),
            row.get("tool", "-") or "-",
            row.get("risk_tier", "-") or "-",
            row.get("outcome", "-") or "-",
            (row.get("recorded_at", "") or "")[11:19],
        )
    console.print(table)
    console.print(f"[dim]Hash zinciri: {ledger.verify()}[/dim]")


def _print_health(agent: SAPMultiAgent) -> None:
    health = agent.health()
    console.print(
        Panel(
            json.dumps(health, indent=2, ensure_ascii=False),
            title="Durum",
            border_style="cyan",
        )
    )


def _run_turn(agent: SAPMultiAgent, message: str, *, show_tools: bool) -> None:
    def on_tool_start(name: str, args: dict) -> None:
        if show_tools:
            preview = json.dumps(args, ensure_ascii=False)
            console.print(f"  [dim]-> {name}({preview[:140]}{'...' if len(preview) > 140 else ''})[/dim]")

    def on_tool_end(name: str, is_error: bool) -> None:
        if show_tools and is_error:
            console.print(f"  [red]!! {name} hata dondurdu[/red]")

    with console.status("[cyan]Dusunuyor...", spinner="dots"):
        turn = agent.chat(message, on_tool_start=on_tool_start, on_tool_end=on_tool_end)

    console.print()
    console.print(Markdown(turn.text or "_(bos yanit)_"))
    console.print()
    console.print(
        f"[dim]{len(turn.tool_calls)} tool cagrisi | {turn.iterations} adim | "
        f"{turn.input_tokens:,} girdi + {turn.output_tokens:,} cikti token | "
        f"sema {turn.schema_tokens:,} tok | pack: {', '.join(turn.active_packs)}[/dim]"
    )
    console.print(f"[dim]Agent zinciri: {' -> '.join(turn.active_agents)}[/dim]")
    if turn.policy_denials:
        console.print(f"[yellow]{turn.policy_denials} tool cagrisi policy tarafindan reddedildi.[/yellow]")
    if turn.needs_review:
        console.print(
            "[red]Dikkat:[/red] bu turda gozden gecirme gerektiren bir sonuc var "
            f"(correlation {turn.correlation_id})."
        )
    for artifact in turn.artifacts:
        console.print(f"[green]Dosya olusturuldu:[/green] {artifact}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="certaops",
        description="Policy denetimli SAP S/4HANA AI agent toolkit'i",
    )
    parser.add_argument("-p", "--prompt", help="Tek seferlik soru sor ve cik.")
    parser.add_argument("--no-tool-log", action="store_true", help="Tool cagrilarini gizle.")
    parser.add_argument("--log-level", default=None, help="DEBUG/INFO/WARNING")
    args = parser.parse_args(argv)

    setup_logging(args.log_level or "WARNING")
    settings = get_settings()

    try:
        agent = SAPMultiAgent(settings)
    except RuntimeError as exc:
        console.print(f"[red]Baslatma hatasi:[/red] {exc}")
        console.print("[dim].env dosyasini olusturup ANTHROPIC_API_KEY degerini girin.[/dim]")
        return 1

    show_tools = not args.no_tool_log

    # Tek seferlik mod
    if args.prompt:
        _run_turn(agent, args.prompt, show_tools=show_tools)
        return 0

    console.print(Panel(BANNER, border_style="cyan"))
    health = agent.health()
    console.print(
        f"[dim]Model: {health['model']} | SAP: {health['sap'].get('backend')} "
        f"({health['sap'].get('status')}) | {health['visible_tool_count']}/"
        f"{health['registered_tool_count']} tool gorunur | dry-run: {health['dry_run']} | "
        f"roller: {', '.join(health['actor']['roles']) or 'yok'}[/dim]\n"
    )
    console.print("[dim]Ornek sorular:[/dim]")
    for example in EXAMPLES:
        console.print(f"  [dim]- {example}[/dim]")
    console.print()

    while True:
        try:
            user_input = console.input("[bold green]sen[/bold green] > ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Gorusmek uzere.[/dim]")
            return 0

        if not user_input:
            continue
        lowered = user_input.lower()

        if lowered in {"/cikis", "/exit", "/quit", "/q"}:
            console.print("[dim]Gorusmek uzere.[/dim]")
            return 0
        if lowered == "/tools":
            _print_tools(agent)
            continue
        if lowered == "/health":
            _print_health(agent)
            continue
        if lowered in {"/butce", "/budget"}:
            _print_budget(agent)
            continue
        if lowered == "/audit":
            _print_audit(agent)
            continue
        if lowered == "/reset":
            agent.reset()
            console.print("[dim]Konusma gecmisi temizlendi.[/dim]")
            continue
        try:
            _run_turn(agent, user_input, show_tools=show_tools)
        except KeyboardInterrupt:
            console.print("\n[yellow]Islem iptal edildi.[/yellow]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Hata:[/red] {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    sys.exit(main())
