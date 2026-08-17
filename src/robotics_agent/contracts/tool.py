"""Ortak tool sonuc sozlesmesi, alan projeksiyonu ve token butcesi.

Temel sonuc sozlesmesi:
  - Her sonuc `detail` (summary/standard/full) seviyesini destekler.
  - Butceyi asan sonuc konusmaya tam yazilmaz; ozet + cursor + evidence handle doner.
  - Kirpma sirasinda "korunan alanlar" (business object ID, tutar, ETag, onay,
    policy karari, uyari) asla dusurulmez.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from .evidence import Evidence

DetailLevel = Literal["summary", "standard", "full"]
DETAIL_LEVELS: tuple[str, ...] = ("summary", "standard", "full")

# Ortak `detail`/`limit`/`cursor` parametrelerinin JSON Schema parcasi.
# Tool semalarina tek yerden eklenir; metin tekrari token harcamasin diye kisa tutuldu.
COMMON_PROJECTION_SCHEMA: dict[str, Any] = {
    "detail": {
        "type": "string",
        "enum": list(DETAIL_LEVELS),
        "description": "Sonuc ayrinti seviyesi. summary=karar+toplamlar, standard=varsayilan, full=tam kayit (sayfali).",
        "default": "standard",
    },
    "limit": {"type": "integer", "description": "Dondurulecek kayit sayisi ust siniri."},
    "cursor": {"type": "string", "description": "Onceki cagriliktan gelen sayfa imleci."},
}

# Tool semalarina gomulen ortak, **kompakt** detay alani. Enum degerleri kendini
# aciklar; her tool'un ayni cumleyi tekrar etmesi turda ~15 token bosa harcar
# ve `BUDGET_SCHEMA_TOKENS` butcesini erken doldurur. Seviyelerin ne anlama
# geldigi tool aciklamasinda ve burada tek yerde yazilidir:
#   summary  = karar + toplamlar
#   standard = varsayilan calisma seviyesi
#   full     = tam kayit (ek kapsam ve purpose_code ister, bkz. FieldAccessPolicy)
DETAIL_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": list(DETAIL_LEVELS),
    "default": "standard",
}

# Kirpma/compaction sirasinda korunmasi zorunlu alan adlari.
PROTECTED_KEYS: frozenset[str] = frozenset(
    {
        "error",
        "errors",
        "sap_code",
        "sap_messages",
        "warnings",
        "risk",
        "risk_flags",
        "risk_tier",
        "policy_decision",
        "approval",
        "approval_id",
        "payload_sha256",
        "idempotency_key",
        "business_object_id",
        "requisition_id",
        "po_id",
        "material_id",
        "vendor_id",
        "wbs_element",
        "etag",
        "currency",
        "total_value",
        "verified",
        "evidence",
        "evidence_id",
        "actor",
        "correlation_id",
        "execution_id",
        "estimated",
        "estimated_fields",
        "requires_human_approval",
        "needs_review",
    }
)


def estimate_tokens(value: Any) -> int:
    """Kaba token tahmini. Turkce JSON icin ~4 karakter/token yeterince dogru."""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return max(1, len(text) // 4)


def project(rows: Iterable[dict[str, Any]], fields: Iterable[str] | None) -> list[dict[str, Any]]:
    """Satirlari istenen alanlara indirger; korunan alanlar her zaman kalir."""
    if not fields:
        return [dict(r) for r in rows]
    wanted = set(fields) | PROTECTED_KEYS
    return [{k: v for k, v in row.items() if k in wanted} for row in rows]


def resolve_detail(detail: str | None) -> DetailLevel:
    normalized = (detail or "standard").strip().lower()
    if normalized in DETAIL_LEVELS:
        return normalized  # type: ignore[return-value]
    return "standard"


def page_limit(detail: DetailLevel, requested: int | None, *, default: int = 20) -> int:
    """Detay seviyesine gore varsayilan sayfa boyu."""
    if requested and requested > 0:
        return min(int(requested), 200)
    return {"summary": max(3, default // 4), "standard": default, "full": min(200, default * 5)}[
        detail
    ]


@dataclass
class ToolResult:
    """Tool'larin dondurdugu zarf.

    `data` is verisi, `evidence` kaynak kaniti, `warnings` karar vericiye
    gosterilecek uyarilar. `to_payload()` bunlari tek sozluge cevirir.
    """

    data: dict[str, Any]
    evidence: Evidence | None = None
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    detail: DetailLevel = "standard"
    cursor: str | None = None
    total_count: int | None = None
    returned_count: int | None = None
    evidence_id: str | None = None
    needs_review: bool = False

    def warn(self, message: str) -> ToolResult:
        if message not in self.warnings:
            self.warnings.append(message)
        return self

    def note(self, message: str) -> ToolResult:
        if message not in self.notes:
            self.notes.append(message)
        return self

    def to_payload(self) -> dict[str, Any]:
        payload = dict(self.data)
        if self.warnings:
            payload["warnings"] = list(self.warnings)
        if self.notes:
            payload["notes"] = list(self.notes)
        meta: dict[str, Any] = {"detail": self.detail}
        if self.total_count is not None:
            meta["total_count"] = self.total_count
        if self.returned_count is not None:
            meta["returned_count"] = self.returned_count
        if self.cursor:
            meta["next_cursor"] = self.cursor
        if self.evidence_id:
            meta["evidence_id"] = self.evidence_id
        if self.evidence is not None:
            payload["evidence"] = self.evidence.to_dict()
        if self.needs_review:
            payload["needs_review"] = True
        payload["_meta"] = meta
        return payload


@dataclass
class BudgetOutcome:
    """Butce uygulamasinin sonucu."""

    payload: dict[str, Any]
    original_tokens: int
    final_tokens: int
    trimmed: bool
    dropped_items: int = 0

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.final_tokens)


def enforce_result_budget(
    payload: dict[str, Any],
    *,
    max_tokens: int,
    evidence_id: str | None = None,
) -> BudgetOutcome:
    """Sonucu token butcesine sigdirir.

    Strateji: en buyuk liste veya metni kademeli kisaltir; gerekirse buyuk,
    korumasiz alanlari kaldirir. Normal kosullarda korunan alanlara dokunulmaz.
    Yalniz korunan verinin kendisi butceyi asiyorsa hard limit fail-closed
    uygulanir ve tam kayit evidence handle arkasinda kalir.
    Kirpma yapildiginda `_meta.truncated` ve varsa `_meta.evidence_id` eklenir ki
    model tam kaydin nerede oldugunu bilsin.
    """
    max_tokens = max(1, int(max_tokens))
    original = estimate_tokens(payload)
    if original <= max_tokens:
        return BudgetOutcome(
            payload=payload, original_tokens=original, final_tokens=original, trimmed=False
        )

    trimmed_payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
    existing_meta = trimmed_payload.get("_meta")
    if not isinstance(existing_meta, dict):
        existing_meta = {}
        trimmed_payload["_meta"] = existing_meta
    meta = existing_meta
    meta["truncated"] = True
    meta["dropped_items"] = 0
    meta["budget_tokens"] = max_tokens
    if evidence_id:
        meta["evidence_id"] = evidence_id
    meta["hint"] = "Tam kayit icin get_evidence tool'unu evidence_id ile cagir."

    dropped = _compact_to_budget(trimmed_payload, max_tokens=max_tokens, include_protected=False)
    if estimate_tokens(trimmed_payload) > max_tokens:
        # Hard limit, korunan tek bir scalar/string'in de siniri asmasina izin
        # vermez. Tam deger evidence store'da kalir.
        dropped += _compact_to_budget(
            trimmed_payload, max_tokens=max_tokens, include_protected=True
        )
    meta["dropped_items"] = dropped

    if estimate_tokens(trimmed_payload) > max_tokens:
        # Handler tarafindan gelen sisirilmis `_meta` de hard limiti delemez.
        compact_meta: dict[str, Any] = {
            "truncated": True,
            "dropped_items": dropped,
            "budget_tokens": max_tokens,
        }
        if evidence_id:
            compact_meta["evidence_id"] = evidence_id
        trimmed_payload["_meta"] = compact_meta
        _compact_to_budget(trimmed_payload, max_tokens=max_tokens, include_protected=True)

    if estimate_tokens(trimmed_payload) > max_tokens:
        # Cok dusuk yapilandirma limitlerinde metadata bile sigmayabilir. En
        # kucuk dogru zarf secilir; `{}` her pozitif limitte bir token sayilir.
        candidates: list[dict[str, Any]] = []
        if evidence_id:
            candidates.append({"_meta": {"truncated": True, "evidence_id": evidence_id}})
        candidates.extend(({"_meta": {"truncated": True}}, {}))
        trimmed_payload = next(
            candidate for candidate in candidates if estimate_tokens(candidate) <= max_tokens
        )

    final = estimate_tokens(trimmed_payload)
    assert final <= max_tokens
    return BudgetOutcome(
        payload=trimmed_payload,
        original_tokens=original,
        final_tokens=final,
        trimmed=True,
        dropped_items=dropped,
    )


_Path = tuple[str | int, ...]
_Compaction = tuple[int, _Path, Any, int]


def _compact_to_budget(payload: dict[str, Any], *, max_tokens: int, include_protected: bool) -> int:
    """Nested list/string'leri hard token limitine kadar deterministik kisalt."""

    dropped = 0
    for _ in range(512):
        if estimate_tokens(payload) <= max_tokens:
            break
        candidate = _largest_compactable(payload, include_protected=include_protected)
        if candidate is not None:
            _, path, replacement, removed = candidate
            _set_path(payload, path, replacement)
            dropped += removed
            continue
        removable = _largest_removable_key(payload, include_protected=include_protected)
        if removable is None:
            break
        parent, key = _parent_and_key(payload, removable)
        del parent[key]
    return dropped


def _largest_compactable(payload: dict[str, Any], *, include_protected: bool) -> _Compaction | None:
    best: _Compaction | None = None

    def consider(path: _Path, value: Any, *, protected: bool) -> None:
        nonlocal best
        if protected and not include_protected:
            return
        replacement: Any
        removed = 0
        if isinstance(value, list) and value:
            keep = len(value) // 2
            replacement = value[:keep]
            removed = len(value) - keep
        elif isinstance(value, str) and value:
            keep = len(value) // 2
            replacement = value[:keep] + ("…" if keep else "")
        else:
            return
        saving = _json_size(value) - _json_size(replacement)
        if saving > 0 and (best is None or saving > best[0]):
            best = (saving, path, replacement, removed)

    def visit(value: Any, path: _Path, *, protected: bool) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "_meta":
                    continue
                child_protected = protected or key in PROTECTED_KEYS
                consider(path + (key,), child, protected=child_protected)
                visit(child, path + (key,), protected=child_protected)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                consider(path + (index,), child, protected=protected)
                visit(child, path + (index,), protected=protected)

    visit(payload, (), protected=False)
    return best


def _largest_removable_key(payload: dict[str, Any], *, include_protected: bool) -> _Path | None:
    best: tuple[int, _Path] | None = None

    def visit(value: Any, path: _Path, *, protected: bool) -> None:
        nonlocal best
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            if key == "_meta":
                continue
            child_protected = protected or key in PROTECTED_KEYS
            child_path = path + (key,)
            if include_protected or not child_protected:
                size = _json_size({key: child})
                if best is None or size > best[0]:
                    best = (size, child_path)
            visit(child, child_path, protected=child_protected)

    visit(payload, (), protected=False)
    return best[1] if best is not None else None


def _parent_and_key(payload: dict[str, Any], path: _Path) -> tuple[Any, str | int]:
    parent: Any = payload
    for part in path[:-1]:
        parent = parent[part]
    return parent, path[-1]


def _set_path(payload: dict[str, Any], path: _Path, value: Any) -> None:
    parent, key = _parent_and_key(payload, path)
    parent[key] = value


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))
