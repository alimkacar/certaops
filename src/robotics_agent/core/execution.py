"""Zorunlu yazma protokolu.

Her mutating tool su siradan gecer:

  Resolve -> Read -> Validate -> Diff -> Approve -> Execute -> Verify -> Audit -> Compensate

`WriteGuard` bu zincirin Execute/Verify/Audit/Compensate kismini tek yerde
uygular ki her tool kendi versiyonunu yazmasin. Onay ve policy karari cagri
oncesinde `core.policy` tarafindan uretilir; burada kanit **rezerve edilir**,
yazma yapilir ve basarida tuketilir.

Eszamanlilik garantileri:
  - Idempotency lease ve onay rezervasyonu ayni transaction'da alinir.
  - Baska bir yurutme lease tutuyorsa asla ikinci POST gonderilmez.
  - Lease devralindiginda once mutabakat denenir; kanitlanmadan yazilmaz.

Timeout sonrasinda hemen tekrar POST edilmez. Idempotency kaydi `unknown`a
alinir, business key ile read-back denenir, olusmussa mutabakat yapilir;
olusmamissa ayni anahtarla kontrollu tekrar mumkundur.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from ..contracts import ExecutionContext
from .approvals import ApprovalError, ApprovalStore
from .audit import AuditLedger, sha256_of
from .idempotency import (
    ApprovalReservationConflict,
    BeginOutcome,
    IdempotencyConflict,
    IdempotencyStore,
)
from .policy import OBLIGATION_CONSUME_APPROVAL, PolicyDecision

log = logging.getLogger(__name__)

WriteStatus = Literal[
    "created",
    "simulated",
    "duplicate_prevented",
    "reconciled",
    "needs_review",
    "in_progress",
    "failed",
]


@dataclass
class Verification:
    """Read-after-write sonucu."""

    verified: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    snapshot: dict[str, Any] | None = None
    message: str = ""

    @classmethod
    def compare(
        cls, expected: dict[str, Any], actual: dict[str, Any] | None, *, tolerance: float = 0.01
    ) -> Verification:
        """Beklenen postcondition'lari SAP'tan okunan kayitla karsilastirir."""
        if actual is None:
            return cls(verified=False, message="Is nesnesi SAP'tan geri okunamadi.")
        checks: list[dict[str, Any]] = []
        ok = True
        for key, want in expected.items():
            got = actual.get(key)
            if isinstance(want, int | float) and isinstance(got, int | float):
                matched = abs(float(want) - float(got)) <= tolerance
            else:
                matched = str(want) == str(got)
            checks.append({"field": key, "expected": want, "actual": got, "match": matched})
            ok = ok and matched
        return cls(
            verified=ok,
            checks=checks,
            snapshot=actual,
            message="Postcondition'lar dogrulandi." if ok else "Postcondition uyusmazligi var.",
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"verified": self.verified}
        if self.message:
            payload["message"] = self.message
        mismatches = [c for c in self.checks if not c["match"]]
        if mismatches:
            payload["mismatches"] = mismatches
        elif self.checks:
            payload["checked_fields"] = [c["field"] for c in self.checks]
        return payload


@dataclass
class WriteOutcome:
    status: WriteStatus
    business_object_id: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    verification: Verification | None = None
    idempotency: dict[str, Any] = field(default_factory=dict)
    messages: list[str] = field(default_factory=list)
    remediation: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status in {"created", "reconciled", "duplicate_prevented", "simulated"}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "write_status": self.status,
            "business_object_id": self.business_object_id or None,
            **self.result,
        }
        if self.verification is not None:
            payload["verification"] = self.verification.to_dict()
            payload["verified"] = self.verification.verified
        if self.idempotency:
            payload["idempotency"] = self.idempotency
        if self.messages:
            payload["messages"] = list(self.messages)
        if self.remediation:
            payload["remediation"] = self.remediation
        if self.status in {"needs_review", "in_progress"}:
            payload["needs_review"] = True
        return {k: v for k, v in payload.items() if v is not None}


# Hangi hatalar "sonuc bilinmiyor" sayilir: istek gitmis olabilir.
_UNKNOWN_OUTCOME_ERRORS = (
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    httpx.NetworkError,
)


@dataclass
class WriteGuard:
    """Yazma protokolunu uygulayan yardimci."""

    audit: AuditLedger
    idempotency: IdempotencyStore
    approvals: ApprovalStore | None
    execution: ExecutionContext

    def run(
        self,
        *,
        tool: str,
        decision: PolicyDecision,
        payload: dict[str, Any],
        idempotency_key: str,
        execute: Callable[[], tuple[str, dict[str, Any]]],
        verify: Callable[[str], Verification],
        before: dict[str, Any] | None = None,
        reconcile: Callable[[], tuple[str, dict[str, Any]] | None] | None = None,
    ) -> WriteOutcome:
        tenant = self.execution.actor.tenant
        digest = sha256_of(payload)
        approval_id = decision.approval_id

        # --- Idempotency lease + onay rezervasyonu (tek transaction) --------
        try:
            begin = self.idempotency.begin(
                idempotency_key,
                tenant=tenant,
                tool=tool,
                payload_sha256=digest,
                execution_id=self.execution.execution_id,
                approval_id=approval_id if decision.requires(OBLIGATION_CONSUME_APPROVAL) else "",
            )
        except IdempotencyConflict as exc:
            self._audit("write.idempotency.conflict", tool, decision, digest, idempotency_key,
                        outcome="denied", detail={"reason": str(exc)})
            return WriteOutcome(
                status="failed",
                messages=[str(exc)],
                remediation="Farkli bir islem icin farkli idempotency_key kullanin.",
            )
        except ApprovalReservationConflict as exc:
            self._audit("write.approval.reservation_conflict", tool, decision, digest,
                        idempotency_key, outcome="denied", detail={"reason": str(exc)})
            return WriteOutcome(
                status="in_progress",
                messages=[str(exc)],
                remediation=(
                    "Bu onay baska bir yurutmede kullaniliyor. Sonucunu bekleyin ve "
                    "sap_reconcile_execution ile durumu dogrulayin; ikinci yazma denemeyin."
                ),
            )

        record = begin.record

        if begin.outcome is BeginOutcome.COMPLETED:
            self._audit("write.duplicate_prevented", tool, decision, digest, idempotency_key,
                        outcome="ok", detail=record.to_dict())
            return WriteOutcome(
                status="duplicate_prevented",
                business_object_id=record.business_object_id,
                result=record.result or {},
                idempotency=record.to_dict(),
                messages=[
                    f"Bu islem daha once {record.business_object_id} olarak tamamlandi; "
                    "yeni belge olusturulmadi."
                ],
            )

        if begin.outcome is BeginOutcome.IN_PROGRESS:
            # Baska bir worker ayni anahtarla calisiyor. Ikinci POST gondermek
            # duplicate belge uretme riskidir; kontrollu sekilde geri cekiliyoruz.
            self._audit("write.in_progress", tool, decision, digest, idempotency_key,
                        outcome="needs_review", detail=record.to_dict())
            return WriteOutcome(
                status="in_progress",
                idempotency=record.to_dict(),
                messages=[
                    f"Ayni idempotency_key '{idempotency_key}' su anda "
                    f"{record.lease_owner} yurutmesi tarafindan isleniyor."
                ],
                remediation=(
                    "Tekrar gondermeyin. Kisa bir bekleme sonrasi "
                    "sap_reconcile_execution ile sonucu ogrenin."
                ),
            )

        # Devralinan lease veya onceki denemenin kalintisi: once mutabakat.
        needs_recovery_check = (
            begin.outcome in {BeginOutcome.RECOVERED, BeginOutcome.RETRY}
            and reconcile is not None
            and record.needs_reconciliation
        )
        if needs_recovery_check:
            found = reconcile()
            if found is not None:
                object_id, existing = found
                verification = verify(object_id)
                self.idempotency.complete(
                    idempotency_key,
                    tenant=tenant,
                    business_object_id=object_id,
                    result=existing,
                )
                self._consume_approval(decision)
                self._audit("write.reconciled", tool, decision, digest, idempotency_key,
                            outcome="ok", after=existing)
                return WriteOutcome(
                    status="reconciled",
                    business_object_id=object_id,
                    result=existing,
                    verification=verification,
                    idempotency=record.to_dict(),
                    messages=[
                        f"Onceki denemede olusan {object_id} bulundu ve mutabakat yapildi."
                    ],
                )

        # --- Yurutme --------------------------------------------------------
        started = time.perf_counter()
        try:
            business_object_id, raw_result = execute()
        except _UNKNOWN_OUTCOME_ERRORS as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self.idempotency.mark_unknown(idempotency_key, tenant=tenant, reason=reason)
            recovered = reconcile() if reconcile is not None else None
            if recovered is not None:
                object_id, existing = recovered
                verification = verify(object_id)
                self.idempotency.complete(
                    idempotency_key, tenant=tenant, business_object_id=object_id, result=existing
                )
                self._consume_approval(decision)
                self._audit("write.reconciled_after_timeout", tool, decision, digest,
                            idempotency_key, outcome="ok", after=existing,
                            detail={"error": reason})
                return WriteOutcome(
                    status="reconciled",
                    business_object_id=object_id,
                    result=existing,
                    verification=verification,
                    messages=[f"Cagri kesildi ama islem SAP'ta olustu: {object_id}."],
                )
            # Belge bulunamadi ama "yok" oldugu kanitlanmadi (eventual
            # consistency): onay rezervasyonu birakilir, karar insana kalir.
            self._release_approval(decision)
            self._audit("write.unknown_outcome", tool, decision, digest, idempotency_key,
                        outcome="needs_review", detail={"error": reason})
            return WriteOutcome(
                status="needs_review",
                idempotency={"idempotency_key": idempotency_key, "status": "unknown"},
                messages=[f"Yazma cagrisi kesildi, sonuc bilinmiyor: {reason}"],
                remediation=(
                    "Tekrar POST etmeyin. sap_reconcile_execution ile ayni idempotency_key "
                    "uzerinden SAP'ta olusup olusmadigini kontrol edin."
                ),
            )
        except Exception as exc:  # noqa: BLE001 - is/teknik hata modele aciklanmali
            reason = f"{type(exc).__name__}: {exc}"
            self.idempotency.fail(idempotency_key, tenant=tenant, reason=reason)
            self._release_approval(decision)
            self._audit("write.failed", tool, decision, digest, idempotency_key,
                        outcome="error", detail={"error": reason})
            raise

        duration_ms = (time.perf_counter() - started) * 1000

        # --- Verify (read-after-write) --------------------------------------
        verification = verify(business_object_id)
        self.idempotency.complete(
            idempotency_key,
            tenant=tenant,
            business_object_id=business_object_id,
            result=raw_result,
        )

        # --- Onayi tuket (replay engeli) ------------------------------------
        self._consume_approval(decision)

        status: WriteStatus = "created" if business_object_id else "needs_review"
        outcome = WriteOutcome(
            status=status,
            business_object_id=business_object_id,
            result=raw_result,
            verification=verification,
            idempotency={"idempotency_key": idempotency_key, "status": "completed"},
        )
        if not verification.verified:
            outcome.status = "needs_review"
            outcome.remediation = (
                "Belge olustu ancak postcondition dogrulanamadi. Kaydi manuel inceleyin; "
                "gerekirse duzeltme/iptal islemi acin."
            )

        self._audit(
            "write.completed" if verification.verified else "write.verification_failed",
            tool,
            decision,
            digest,
            idempotency_key,
            outcome="ok" if verification.verified else "needs_review",
            before=before,
            after=verification.snapshot or raw_result,
            duration_ms=duration_ms,
        )
        return outcome

    # --- Onay yasam dongusu -------------------------------------------------
    def _consume_approval(self, decision: PolicyDecision) -> None:
        if not (decision.requires(OBLIGATION_CONSUME_APPROVAL) and self.approvals and decision.approval_id):
            return
        try:
            self.approvals.consume(
                decision.approval_id, execution_id=self.execution.execution_id
            )
        except ApprovalError as exc:
            # Yazma gerceklesti; onay kaydi tuketilemedi. Bu bir tutarsizlik
            # sinyalidir ve gozden gecirilmesi gerekir.
            log.error("Onay tuketilemedi (%s): %s", decision.approval_id, exc)
            self.audit.append(
                "write.approval_consume_failed",
                execution=self.execution,
                tool=decision.tool,
                risk_tier=decision.risk_tier.value,
                outcome="needs_review",
                approval_id=decision.approval_id,
                detail={"error": str(exc)},
            )

    def _release_approval(self, decision: PolicyDecision) -> None:
        """Yazma kanitlanmadiginda rezervasyonu birakir (tuketmez)."""
        if decision.approval_id:
            self.idempotency.release_approval(
                decision.approval_id, execution_id=self.execution.execution_id
            )

    def _audit(
        self,
        event: str,
        tool: str,
        decision: PolicyDecision,
        payload_sha256: str,
        idempotency_key: str,
        *,
        outcome: str,
        before: Any = None,
        after: Any = None,
        detail: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> None:
        self.audit.append(
            event,
            execution=self.execution,
            tool=tool,
            risk_tier=decision.risk_tier.value,
            outcome=outcome,
            policy=decision.to_dict(),
            payload_sha256=payload_sha256,
            idempotency_key=idempotency_key,
            approval_id=decision.approval_id,
            before=before,
            after=after,
            detail=detail,
            duration_ms=duration_ms,
        )
