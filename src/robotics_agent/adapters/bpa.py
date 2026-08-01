"""SAP Build Process Automation onay adaptoru.

Insan onayi BPA user task ile alinir ve callback agent yurutmesini devam
ettirir. Bu modul iki uygulama sunar:

  BPAApprovalGateway   -> gercek BPA workflow API'si (REST)
  LocalApprovalGateway -> BPA yokken ayni sozlesmeyi yerine getiren yerel akis

Ikisi de ayni sonucu uretir: `core.approvals.ApprovalStore` icinde payload
hash'ine bagli, sureli ve tek kullanimlik bir onay kaydi. Boylece tool katmani
onayin nereden geldigini bilmek zorunda kalmaz.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from ..contracts import ActorContext
from ..core.approvals import ApprovalError, ApprovalRecord, ApprovalStore, payload_hash
from .sap.errors import SAPError

log = logging.getLogger(__name__)


@dataclass
class ApprovalRequest:
    """Onaya sunulan islem."""

    tool: str
    payload: dict[str, Any]
    tenant: str
    requested_by: str
    subject_line: str
    diff: list[dict[str, Any]]
    total_value: float = 0.0
    currency: str = "EUR"
    max_value: float | None = None

    def to_task_context(self) -> dict[str, Any]:
        """BPA user task'ina gonderilen baglam. Onaylayan bunu gorur."""
        return {
            "tool": self.tool,
            "subject": self.subject_line,
            "requestedBy": self.requested_by,
            "totalValue": self.total_value,
            "currency": self.currency,
            "payloadSha256": payload_hash(self.payload),
            "diff": self.diff[:50],
        }


class ApprovalGateway(ABC):
    """Onay akisinin ortak sozlesmesi."""

    name = "abstract"

    def __init__(self, store: ApprovalStore, *, ttl_minutes: int = 60) -> None:
        self.store = store
        self.ttl_minutes = ttl_minutes

    @abstractmethod
    def request(self, request: ApprovalRequest) -> dict[str, Any]:
        """Onay talebi acar; bekleyen task bilgisini dondurur."""

    @abstractmethod
    def complete(
        self, *, task_id: str, approvers: Sequence[ActorContext] = (),
        request: ApprovalRequest, comment: str = "",
    ) -> ApprovalRecord:
        """Onay tamamlandiginda dogrulanabilir kayit uretir.

        `approvers` yalniz yerel gecitte anlamlidir. BPA gecidinde onaylayan
        kimligi workflow'dan okunur ve bu parametre yok sayilir.
        """


class LocalApprovalGateway(ApprovalGateway):
    """BPA olmadan calisan yerel onay akisi.

    Onemli: bu, "sohbette evet demek" degildir. Onaylayan olarak gecirilen
    ActorContext'in `sap.pr.approve` benzeri bir kapsami olmali; aksi halde
    `ApprovalStore.validate` reddeder.
    """

    name = "local"

    def __init__(self, store: ApprovalStore, *, ttl_minutes: int = 60) -> None:
        super().__init__(store, ttl_minutes=ttl_minutes)
        self._pending: dict[str, ApprovalRequest] = {}

    def request(self, request: ApprovalRequest) -> dict[str, Any]:
        task_id = f"task-local-{payload_hash(request.payload)[:12]}"
        self._pending[task_id] = request
        return {
            "gateway": self.name,
            "task_id": task_id,
            "status": "pending",
            "payload_sha256": payload_hash(request.payload),
            "context": request.to_task_context(),
            "instruction": (
                "Yetkili onaylayici bu task'i tamamladiginda approval_id uretilir. "
                "Onay kaydi yalniz bu payload icin gecerlidir."
            ),
        }

    def complete(
        self, *, task_id: str, approvers: Sequence[ActorContext] = (),
        request: ApprovalRequest, comment: str = "",
    ) -> ApprovalRecord:
        if not approvers:
            raise ApprovalError(
                "Yerel onay gecidi en az bir onaylayan gerektirir.", code="NO_APPROVER"
            )
        self._pending.pop(task_id, None)
        scope: dict[str, Any] = {"task_id": task_id}
        if request.max_value is not None:
            scope["max_value"] = request.max_value
        return self.store.issue(
            tool=request.tool,
            payload=request.payload,
            tenant=request.tenant,
            approvers=approvers,
            requested_by=request.requested_by,
            workflow_instance_id=task_id,
            ttl_minutes=self.ttl_minutes,
            comment=comment,
            scope=scope,
        )

    def pending(self) -> dict[str, dict[str, Any]]:
        return {k: v.to_task_context() for k, v in self._pending.items()}


class BPAApprovalGateway(ApprovalGateway):
    """SAP Build Process Automation workflow API'si uzerinden onay."""

    name = "sap_bpa"

    def __init__(
        self,
        store: ApprovalStore,
        *,
        base_url: str,
        token_provider: Any,
        definition_id: str,
        client: httpx.Client | None = None,
        ttl_minutes: int = 60,
    ) -> None:
        super().__init__(store, ttl_minutes=ttl_minutes)
        self._base_url = base_url.rstrip("/")
        self._token_provider = token_provider
        self._definition_id = definition_id
        self._client = client or httpx.Client(timeout=30.0)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token_provider()}",
            "Content-Type": "application/json",
        }

    def request(self, request: ApprovalRequest) -> dict[str, Any]:
        body = {
            "definitionId": self._definition_id,
            "context": request.to_task_context(),
        }
        response = self._client.post(
            f"{self._base_url}/workflow/rest/v1/workflow-instances",
            json=body,
            headers=self._headers(),
        )
        if not response.is_success:
            raise SAPError(
                f"BPA workflow baslatilamadi (HTTP {response.status_code}).",
                code="BPA_START_FAILED",
                detail=self._definition_id,
            )
        payload = response.json()
        return {
            "gateway": self.name,
            "task_id": str(payload.get("id", "")),
            "status": str(payload.get("status", "RUNNING")),
            "payload_sha256": payload_hash(request.payload),
            "context": request.to_task_context(),
        }

    def complete(
        self, *, task_id: str, approvers: Sequence[ActorContext] = (), request: ApprovalRequest,
        comment: str = "",
    ) -> ApprovalRecord:
        """BPA callback'i geldiginde cagrilir.

        Onaylayan kimligi ve karar **BPA'dan okunur**; cagiricinin bildirdigi
        `approvers` listesi yok sayilir. Workflow'un
        yalnizca `COMPLETED` olmasi yeterli degildir: kararin gercekten
        "approved" oldugu ve task'i kimin tamamladigi dogrulanir.
        """
        if approvers:
            log.warning(
                "BPA gecidi cagiricinin bildirdigi onaylayan listesini yok sayar; "
                "kimlik workflow'dan okunur."
            )

        instance = self._get_instance(task_id)
        status = str(instance.get("status", "")).upper()
        if status != "COMPLETED":
            raise SAPError(
                f"BPA workflow {task_id} henuz tamamlanmadi (durum: {status}).",
                code="BPA_NOT_COMPLETED",
                detail=task_id,
            )

        context = instance.get("context") or {}
        expected_hash = payload_hash(request.payload)
        if context.get("payloadSha256") != expected_hash:
            raise SAPError(
                "BPA workflow'daki payload hash'i mevcut istekle uyusmuyor.",
                code="BPA_PAYLOAD_MISMATCH",
                detail=task_id,
            )
        # Tenant sinirinin asilmadigini da workflow baglamindan dogrula.
        context_tenant = str(context.get("tenant") or request.tenant)
        if context_tenant != request.tenant:
            raise SAPError(
                f"BPA workflow tenant'i ({context_tenant}) istekle uyusmuyor.",
                code="BPA_TENANT_MISMATCH",
                detail=task_id,
            )

        decisions = self._approved_decisions(task_id)
        if not decisions:
            raise SAPError(
                f"BPA workflow {task_id} tamamlandi ancak 'approved' bir karar bulunamadi. "
                "Reddedilmis veya iptal edilmis olabilir.",
                code="BPA_NOT_APPROVED",
                detail=task_id,
            )

        verified_approvers = tuple(
            ActorContext(
                subject=subject,
                tenant=request.tenant,
                roles=roles,
                auth_method="sap_bpa",
            )
            for subject, roles in decisions
        )

        scope: dict[str, Any] = {
            "workflow_instance_id": task_id,
            "verified_by": "sap_bpa",
        }
        if request.max_value is not None:
            scope["max_value"] = request.max_value
        return self.store.issue(
            tool=request.tool,
            payload=request.payload,
            tenant=request.tenant,
            approvers=verified_approvers,
            requested_by=request.requested_by,
            workflow_instance_id=task_id,
            ttl_minutes=self.ttl_minutes,
            comment=comment or str(context.get("decisionComment", "")),
            scope=scope,
        )

    def _get_instance(self, task_id: str) -> dict[str, Any]:
        response = self._client.get(
            f"{self._base_url}/workflow/rest/v1/workflow-instances/{task_id}",
            headers=self._headers(),
        )
        if not response.is_success:
            raise SAPError(
                f"BPA workflow durumu okunamadi (HTTP {response.status_code}).",
                code="BPA_STATUS_FAILED",
                detail=task_id,
            )
        return response.json()

    def _approved_decisions(self, task_id: str) -> list[tuple[str, tuple[str, ...]]]:
        """Workflow'un user task'larindan onaylayan kimliklerini cikarir.

        Yalnizca `COMPLETED` + kararı olumlu olan task'lar sayilir. Karar alani
        BPA modeline gore `decision`/`outcome` olabilir; ikisi de kontrol edilir.
        """
        response = self._client.get(
            f"{self._base_url}/workflow/rest/v1/task-instances",
            params={"workflowInstanceId": task_id},
            headers=self._headers(),
        )
        if not response.is_success:
            raise SAPError(
                f"BPA task listesi okunamadi (HTTP {response.status_code}).",
                code="BPA_TASKS_FAILED",
                detail=task_id,
            )
        approved: list[tuple[str, tuple[str, ...]]] = []
        for task in response.json() or []:
            if str(task.get("status", "")).upper() != "COMPLETED":
                continue
            decision = str(
                task.get("decision") or task.get("outcome") or ""
            ).strip().lower()
            if decision not in _APPROVED_DECISIONS:
                continue
            subject = str(
                task.get("processedBy") or task.get("completedBy") or task.get("recipientUsers", [""])[0]
            ).strip()
            if not subject:
                raise SAPError(
                    f"BPA task {task.get('id')} onaylayan kimligi icermiyor; "
                    "onay kaydi uretilemez.",
                    code="BPA_APPROVER_UNKNOWN",
                    detail=task_id,
                )
            roles = tuple(str(r).upper() for r in (task.get("recipientGroups") or ())) or (
                "APPROVER",
            )
            approved.append((subject, roles))
        return approved

    def close(self) -> None:
        self._client.close()


# BPA user task'inda "onaylandi" anlamina gelen karar degerleri.
_APPROVED_DECISIONS = frozenset({"approve", "approved", "accept", "accepted", "yes", "onayla"})


def build_approval_gateway(settings: Any, store: ApprovalStore) -> ApprovalGateway:
    """Yapilandirmaya gore onay gecidini kurar.

    `AGENT_APPROVAL_GATEWAY=bpa` iken gercek workflow dogrulamasi devreye girer;
    uretim profilinde `local` gecit ile gercek SAP yazmasi engellenir
    (bkz. `Settings.production_blockers`).
    """
    security = settings.security
    ttl = security.approval_ttl_minutes
    if security.approval_gateway != "bpa":
        return LocalApprovalGateway(store, ttl_minutes=ttl)

    from .sap.destination import OAuth2TokenProvider

    token_provider = OAuth2TokenProvider(
        token_url=security.bpa_token_url,
        client_id=security.bpa_client_id,
        client_secret=security.bpa_client_secret,
    )
    return BPAApprovalGateway(
        store,
        base_url=security.bpa_base_url,
        token_provider=token_provider,
        definition_id=security.bpa_definition_id,
        ttl_minutes=ttl,
    )
