#!/usr/bin/env python3
"""Paket ici sembol tutarliligini SAP'a baglanmadan, saniyeler icinde dogrular.

Neden gerekli
-------------
`pytest` bir modulu ancak import edebilirse calistirabilir. Bir refactor
sirasinda bir sabit tasinir ya da bir alan yeniden adlandirilirsa, hata
"toplama" (collection) asamasinda patlar ve **655 test gecmis gibi gorunurken
34 test hic calismamis olur**. Bu script tam olarak o siniftaki bozulmayi,
hicbir sey calistirmadan, statik olarak bulur.

Iki denetim yapar:

1. **Import tutarliligi.** `from .x import Y` diyen her yerde Y gercekten
   x'te tanimli mi? (Modul seviyesinde def/class/atama/import olarak.)

2. **Settings alan surukienmesi.** Kodda gecen her `settings.<bolum>.<alan>`
   icin, o bolumun dataclass'i o alani gercekten tanimliyor mu? Bir alan
   `AgentSettings`'ten `ModelSettings`'e tasindiginda cagri yerleri sessizce
   geride kalir; calisma zamaninda AttributeError olur.

Kullanim
--------
    python scripts/check_symbols.py            # insan okunur
    python scripts/check_symbols.py --json     # makine okunur
    python scripts/check_symbols.py --strict   # sorun varsa exit 1 (CI kapisi)

Cikis kodlari: 0 temiz (veya --strict yok), 1 sorun bulundu (--strict ile),
2 kaynak dizini bulunamadi.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGES = ("robotics_agent", "certaops")

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


# --- Yardimcilar -----------------------------------------------------------
def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _bound_names(nodes: list[ast.stmt]) -> set[str]:
    """Verilen govdede modul seviyesinde baglanan tum isimler."""
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, (ast.If, ast.Try)):
            # try/except ImportError ve if TYPE_CHECKING bloklari da isim baglar.
            for branch in (
                getattr(node, "body", []),
                getattr(node, "orelse", []),
                getattr(node, "finalbody", []),
                *[h.body for h in getattr(node, "handlers", [])],
            ):
                names |= _bound_names(list(branch))
    return names


def _module_path(dotted: str) -> Path | None:
    base = SRC / dotted.replace(".", "/")
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _dotted(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(module: str, is_package: bool, level: int, target: str | None) -> str:
    base = module.split(".") if module else []
    if not is_package:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join(base + ([target] if target else []))


# --- Denetim 1: import tutarliligi ----------------------------------------
def check_imports() -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    exports: dict[str, set[str]] = {}

    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = _parse(path)
        if tree is None:
            problems.append({
                "kind": "SOZDIZIMI", "file": str(path.relative_to(SRC)),
                "line": "0", "detail": "dosya ayristirilamadi",
            })
            continue
        exports[_dotted(path)] = _bound_names(tree.body)

    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = _parse(path)
        if tree is None:
            continue
        me = _dotted(path)
        is_pkg = path.name == "__init__.py"

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            target = (
                _resolve_relative(me, is_pkg, node.level, node.module)
                if node.level else (node.module or "")
            )
            if not target.startswith(PACKAGES):
                continue
            if _module_path(target) is None:
                problems.append({
                    "kind": "MODUL YOK", "file": str(path.relative_to(SRC)),
                    "line": str(node.lineno), "detail": target,
                })
                continue
            available = exports.get(target, set())
            for alias in node.names:
                if alias.name == "*":
                    continue
                # Alt modul de gecerli bir import hedefidir.
                if alias.name in available or _module_path(f"{target}.{alias.name}"):
                    continue
                problems.append({
                    "kind": "ISIM YOK", "file": str(path.relative_to(SRC)),
                    "line": str(node.lineno), "detail": f"{target}.{alias.name}",
                })
    return problems


# --- Denetim 2: Settings alan surukienmesi --------------------------------
def _settings_sections() -> dict[str, str]:
    """Settings uzerindeki bolum adi -> dataclass adi eslemesi."""
    path = _module_path(f"{PACKAGES[0]}.config")
    tree = _parse(path) if path else None
    if tree is None:
        return {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            out: dict[str, str] = {}
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    ann = item.annotation
                    if isinstance(ann, ast.Name):
                        out[item.target.id] = ann.id
            return out
    return {}


def _class_members(class_name: str) -> set[str] | None:
    path = _module_path(f"{PACKAGES[0]}.config")
    tree = _parse(path) if path else None
    if tree is None:
        return None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            members: set[str] = set()
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    members.add(item.target.id)
                elif isinstance(item, ast.Assign):
                    members.update(t.id for t in item.targets if isinstance(t, ast.Name))
                elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members.add(item.name)
            return members
    return None


def check_settings_fields() -> list[dict[str, str]]:
    sections = _settings_sections()
    if not sections:
        return []
    members = {name: _class_members(cls) for name, cls in sections.items()}
    problems: list[dict[str, str]] = []

    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            # settings.<bolum>.<alan> kalibini yakala
            if not isinstance(node, ast.Attribute):
                continue
            mid = node.value
            if not isinstance(mid, ast.Attribute):
                continue
            root = mid.value
            if not (isinstance(root, ast.Name) and root.id in {"settings", "cfg", "_settings"}):
                continue
            section, field_name = mid.attr, node.attr
            known = members.get(section)
            if known is None or field_name in known:
                continue
            owner = next(
                (s for s, m in members.items() if m and field_name in m and s != section), ""
            )
            hint = f"  -> settings.{owner}.{field_name} olabilir" if owner else ""
            problems.append({
                "kind": "ALAN YOK", "file": str(path.relative_to(SRC)),
                "line": str(node.lineno),
                "detail": f"{sections[section]}.{field_name}{hint}",
            })
    return problems


# --- Rapor -----------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="Makine okunur cikti.")
    parser.add_argument("--strict", action="store_true", help="Sorun varsa 1 ile cik.")
    args = parser.parse_args(argv)

    if not SRC.is_dir():
        print(f"{RED}Kaynak dizini bulunamadi:{RESET} {SRC}")
        return 2

    problems = check_imports() + check_settings_fields()

    if args.json:
        print(json.dumps({"problem_count": len(problems), "problems": problems},
                         ensure_ascii=False, indent=2))
    elif not problems:
        print(f"{GREEN}Temiz.{RESET} Paket ici tum importlar ve Settings alanlari tutarli.")
    else:
        print(f"{BOLD}Sembol tutarsizliklari{RESET}")
        print("  " + "-" * 66)
        for p in problems:
            colour = RED if p["kind"] in {"ISIM YOK", "MODUL YOK", "SOZDIZIMI"} else YELLOW
            print(f"  [{colour}{p['kind']:10}{RESET}] {p['file']}:{p['line']}")
            print(f"      {DIM}{p['detail']}{RESET}")
        print(f"\n{BOLD}Ozet:{RESET} {RED}{len(problems)} sorun{RESET}")
        print(f"\n{DIM}Bu hatalar pytest'te 'collection error' olarak gorunur: "
              f"ilgili testler HIC calismaz, ama gecen test sayisi yuksek kalir. "
              f"Once bunlari kapatin.{RESET}")

    return 1 if (args.strict and problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
