"""Yurutme defteri (execution ledger) — tamper-evident hash zinciri.

Her tool cagrisi icin actor, policy karari, before/after, SAP mesaji ve
correlation ID kalici olarak yazilir. Kayitlar hash zinciri ile baglanir; bir
satir sonradan degistirilirse `verify()` bunu yakalar.
Audit kaydi hicbir kosulda compact edilmez.

**Butunluk garantisinin siniri:** bu uygulama *tamper-evident*'tir, *immutable*
degildir. Dosya/veritabani yazma yetkisi olan biri zinciri bastan yeniden
uretebilir. Gercek WORM garantisi icin kayitlar dis bir append-only
servise (veya duzenli hash checkpoint'i ile) aktarilmalidir; `checkpoint()`
bunun icin uretilen ozet degeri dondurur.

Zincir basi SQLite'ta tutulur ve `BEGIN IMMEDIATE` altinda guncellenir; boylece
birden fazla worker/process ayni anda yazsa da sira ve `prev_hash` tutarli kalir.
`threading.Lock` bu garantiyi veremezdi.

Loglara prompt veya ham SAP payload'i yazilmaz; hassas alanlar redakte edilir,
buyuk govdeler hash + boyut olarak saklanir.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..contracts import ActorContext, ExecutionContext
from .store import StateDatabase, get_state_db

log = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64

# Bu anahtarlar (buyuk/kucuk harf duyarsiz, alt dize eslesmesi) hicbir zaman
# defterde ham haliyle durmaz.
_REDACT_HINTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "cookie",
    "private_key",
)
_REDACTED = "[REDACTED]"
# Bundan uzun metinler hash + uzunluk olarak saklanir.
_MAX_VALUE_CHARS = 600


def canonical_json(payload: Any) -> str:
    """Hash icin kararli JSON: alfabetik anahtar, bosluksuz, ASCII kacissiz."""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def sha256_of(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Hassas alanlari maskeler, buyuk degerleri hash'e indirger."""
    if _depth > 8:
        return "[DEPTH_LIMIT]"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(hint in lowered for hint in _REDACT_HINTS):
                out[key] = _REDACTED
            else:
                out[key] = redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [redact(v, _depth=_depth + 1) for v in value]
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
        return f"[TRUNCATED chars={len(value)} sha256_16={digest}]"
    return value


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    entry_hash: str
    prev_hash: str
    recorded_at: str
    event: str
    execution_id: str
    correlation_id: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "entry_hash": self.entry_hash,
            "prev_hash": self.prev_hash,
            "recorded_at": self.recorded_at,
            "event": self.event,
            "execution_id": self.execution_id,
            "correlation_id": self.correlation_id,
            **self.payload,
        }


class AuditLedger:
    """SQLite destekli, process-safe hash zinciri.

    `mirror_path` verilirse kayitlar ayrica JSONL olarak da yazilir (dis log
    toplayicilar icin). Zincirin dogruluk kaynagi veritabanidir.
    """

    def __init__(self, db: StateDatabase, *, mirror_path: Path | str | None = None) -> None:
        self._db = db
        self.mirror_path = Path(mirror_path) if mirror_path else None
        if self.mirror_path is not None:
            self.mirror_path.parent.mkdir(parents=True, exist_ok=True)

    # --- Yazma --------------------------------------------------------------
    def append(
        self,
        event: str,
        *,
        execution: ExecutionContext | None = None,
        actor: ActorContext | None = None,
        tool: str = "",
        risk_tier: str = "",
        outcome: str = "",
        policy: dict[str, Any] | None = None,
        payload_sha256: str = "",
        idempotency_key: str = "",
        approval_id: str = "",
        before: Any = None,
        after: Any = None,
        sap_messages: Iterable[str] = (),
        evidence: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        model: str = "",
        prompt_version: str = "",
    ) -> AuditEntry:
        resolved_actor = actor or (execution.actor if execution else None)
        body: dict[str, Any] = {
            "actor": resolved_actor.to_dict(include_scopes=False) if resolved_actor else None,
            "tool": tool or None,
            "risk_tier": risk_tier or None,
            "outcome": outcome or None,
            "policy": redact(policy) if policy else None,
            "payload_sha256": payload_sha256 or None,
            "idempotency_key": idempotency_key or None,
            "approval_id": approval_id or None,
            "before": redact(before) if before is not None else None,
            "after": redact(after) if after is not None else None,
            "sap_messages": list(sap_messages) or None,
            "evidence": redact(evidence) if evidence else None,
            "detail": redact(detail) if detail else None,
            "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
            "channel": execution.channel if execution else None,
            "system_alias": execution.system_alias if execution else None,
            "dry_run": execution.dry_run if execution else None,
            # Model/prompt surumu, agent davranisi degistiginde hangi surumun
            # karar verdigini geriye donuk bulmayi saglar.
            "model": model or None,
            "prompt_version": prompt_version or None,
        }
        body = {k: v for k, v in body.items() if v is not None}

        execution_id = execution.execution_id if execution else ""
        correlation_id = execution.correlation_id if execution else ""
        tenant = resolved_actor.tenant if resolved_actor else ""
        recorded_at = datetime.now(timezone.utc).isoformat()

        # Zincir basi ve yeni kayit ayni transaction'da: cross-process guvenli.
        with self._db.write() as conn:
            head = conn.execute(
                "SELECT seq, entry_hash FROM audit_entries ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = head["entry_hash"] if head else GENESIS_HASH
            seq = (int(head["seq"]) + 1) if head else 1

            skeleton = {
                "seq": seq,
                "prev_hash": prev_hash,
                "recorded_at": recorded_at,
                "event": event,
                "execution_id": execution_id,
                "correlation_id": correlation_id,
                **body,
            }
            entry_hash = hashlib.sha256(canonical_json(skeleton).encode("utf-8")).hexdigest()
            conn.execute(
                """
                INSERT INTO audit_entries (seq, entry_hash, prev_hash, recorded_at, event,
                    execution_id, correlation_id, tenant, tool, outcome, body_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    seq, entry_hash, prev_hash, recorded_at, event, execution_id,
                    correlation_id, tenant, tool, outcome,
                    json.dumps(body, ensure_ascii=False, default=str),
                ),
            )

        if self.mirror_path is not None:
            self._mirror({**skeleton, "entry_hash": entry_hash})

        return AuditEntry(
            seq=seq,
            entry_hash=entry_hash,
            prev_hash=prev_hash,
            recorded_at=recorded_at,
            event=event,
            execution_id=execution_id,
            correlation_id=correlation_id,
            payload=body,
        )

    def _mirror(self, row: dict[str, Any]) -> None:
        try:
            with self.mirror_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:  # pragma: no cover - dosya sistemi hatasi
            log.warning("Audit mirror yazilamadi: %s", exc)

    # --- Okuma / dogrulama --------------------------------------------------
    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        return {
            "seq": int(row["seq"]),
            "entry_hash": row["entry_hash"],
            "prev_hash": row["prev_hash"],
            "recorded_at": row["recorded_at"],
            "event": row["event"],
            "execution_id": row["execution_id"],
            "correlation_id": row["correlation_id"],
            **json.loads(row["body_json"] or "{}"),
        }

    def chain(self, execution_id: str) -> list[dict[str, Any]]:
        """Bir islemin plan/onay/cagri/verification zinciri."""
        rows = self._db.query(
            "SELECT * FROM audit_entries WHERE execution_id = ? ORDER BY seq", (execution_id,)
        )
        return [self._row_to_dict(r) for r in rows]

    def by_correlation(self, correlation_id: str) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM audit_entries WHERE correlation_id = ? ORDER BY seq", (correlation_id,)
        )
        return [self._row_to_dict(r) for r in rows]

    def recent(self, limit: int = 50, *, tenant: str | None = None) -> list[dict[str, Any]]:
        if tenant:
            rows = self._db.query(
                "SELECT * FROM audit_entries WHERE tenant = ? ORDER BY seq DESC LIMIT ?",
                (tenant, limit),
            )
        else:
            rows = self._db.query(
                "SELECT * FROM audit_entries ORDER BY seq DESC LIMIT ?", (limit,)
            )
        return [self._row_to_dict(r) for r in reversed(rows)]

    def verify(self, *, limit: int | None = None) -> dict[str, Any]:
        """Hash zincirini dogrular.

        `limit` verilirse yalniz son N kayit kontrol edilir; buyuk defterlerde
        `/health` gibi sik cagrilan yerlerde tam tarama yapilmamalidir.
        """
        if limit:
            rows = list(
                reversed(
                    self._db.query(
                        "SELECT * FROM audit_entries ORDER BY seq DESC LIMIT ?", (limit,)
                    )
                )
            )
            partial = True
        else:
            rows = self._db.query("SELECT * FROM audit_entries ORDER BY seq")
            partial = False

        prev: str | None = None
        for row in rows:
            claimed = row["entry_hash"]
            skeleton = self._row_to_dict(row)
            skeleton.pop("entry_hash", None)
            recomputed = hashlib.sha256(canonical_json(skeleton).encode("utf-8")).hexdigest()
            if prev is not None and row["prev_hash"] != prev:
                return {"valid": False, "broken_at": int(row["seq"]), "reason": "prev_hash uyusmuyor"}
            if prev is None and not partial and row["prev_hash"] != GENESIS_HASH:
                return {"valid": False, "broken_at": int(row["seq"]), "reason": "zincir basi hatali"}
            if claimed != recomputed:
                return {"valid": False, "broken_at": int(row["seq"]), "reason": "entry_hash uyusmuyor"}
            prev = claimed

        head = self._db.query_one("SELECT seq, entry_hash FROM audit_entries ORDER BY seq DESC LIMIT 1")
        return {
            "valid": True,
            "entries": int(head["seq"]) if head else 0,
            "head": head["entry_hash"] if head else GENESIS_HASH,
            "scope": f"son {len(rows)} kayit" if partial else "tam zincir",
        }

    def checkpoint(self) -> dict[str, Any]:
        """Dis sisteme yazilacak zincir ozeti.

        Duzenli araliklarla harici bir WORM/log servisine gonderilirse, yerel
        defterin tamaminin yeniden yazilmasi tespit edilebilir hale gelir.
        """
        head = self._db.query_one(
            "SELECT seq, entry_hash, recorded_at FROM audit_entries ORDER BY seq DESC LIMIT 1"
        )
        return {
            "seq": int(head["seq"]) if head else 0,
            "head_hash": head["entry_hash"] if head else GENESIS_HASH,
            "recorded_at": head["recorded_at"] if head else None,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }


class NullAuditLedger(AuditLedger):
    """Testler ve olcum modu icin kalici yazma yapmayan defter."""

    def __init__(self) -> None:  # noqa: D107 - ust sinifin kurulumunu atlar
        self.mirror_path = None
        self._lock = threading.Lock()
        self._seq = 0
        self._last_hash = GENESIS_HASH
        self._memory: list[dict[str, Any]] = []

    def append(self, event: str, **kwargs: Any) -> AuditEntry:  # type: ignore[override]
        with self._lock:
            self._seq += 1
            execution = kwargs.get("execution")
            entry = AuditEntry(
                seq=self._seq,
                entry_hash=f"mem{self._seq:08d}",
                prev_hash=self._last_hash,
                recorded_at=datetime.now(timezone.utc).isoformat(),
                event=event,
                execution_id=execution.execution_id if execution else "",
                correlation_id=execution.correlation_id if execution else "",
                payload={"tool": kwargs.get("tool", ""), "outcome": kwargs.get("outcome", "")},
            )
            self._last_hash = entry.entry_hash
            self._memory.append(entry.to_dict())
        return entry

    def chain(self, execution_id: str) -> list[dict[str, Any]]:
        return [r for r in self._memory if r.get("execution_id") == execution_id]

    def by_correlation(self, correlation_id: str) -> list[dict[str, Any]]:
        return [r for r in self._memory if r.get("correlation_id") == correlation_id]

    def recent(self, limit: int = 50, *, tenant: str | None = None) -> list[dict[str, Any]]:
        return self._memory[-limit:]

    def verify(self, *, limit: int | None = None) -> dict[str, Any]:
        return {"valid": True, "entries": self._seq, "head": self._last_hash, "scope": "bellek"}

    def checkpoint(self) -> dict[str, Any]:
        return {"seq": self._seq, "head_hash": self._last_hash}


_LEDGER_CACHE: dict[str, AuditLedger] = {}
_LEDGER_LOCK = threading.Lock()


def get_audit_ledger(db_path: Path | str, *, mirror_path: Path | str | None = None) -> AuditLedger:
    """Ayni durum veritabani icin tek defter ornegi."""
    key = str(db_path)
    with _LEDGER_LOCK:
        ledger = _LEDGER_CACHE.get(key)
        if ledger is None:
            ledger = AuditLedger(get_state_db(db_path), mirror_path=mirror_path)
            _LEDGER_CACHE[key] = ledger
        return ledger


def reset_audit_cache() -> None:
    """Testler arasinda temiz baslangic icin."""
    with _LEDGER_LOCK:
        _LEDGER_CACHE.clear()
