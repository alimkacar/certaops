"""Actor, yetki kapsami ve yurutme baglami.

Her tool cagrisi kimin adina, hangi tenant'ta, hangi organizasyon kapsaminda ve
hangi correlation ID ile yapildigini bilmek zorundadir. Bu modul o sozlesmeyi
tanimlar; zorlama `core.policy` icindedir.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

# Organizasyon alanlarinda "her deger serbest" anlamina gelen tek isaret.
# Bos kume "hicbir deger" demektir; deny-by-default davranisi budur.
ORG_WILDCARD = "*"


class RiskTier(str, Enum):
    """SAP islemleri icin R0-R4 risk seviyeleri."""

    R0 = "R0"  # salt okunur
    R1 = "R1"  # hesap / simulasyon
    R2 = "R2"  # geri alinabilir taslak
    R3 = "R3"  # finansal / operasyonel yazma
    R4 = "R4"  # yuksek etkili / kisitli

    @property
    def level(self) -> int:
        return int(self.value[1])

    @property
    def is_mutating(self) -> bool:
        return self.level >= 3

    @property
    def requires_approval(self) -> bool:
        return self.level >= 3

    @property
    def requires_dual_control(self) -> bool:
        return self is RiskTier.R4

    @property
    def label(self) -> str:
        return {
            RiskTier.R0: "salt okunur",
            RiskTier.R1: "hesap/simulasyon",
            RiskTier.R2: "geri alinabilir taslak",
            RiskTier.R3: "finansal/operasyonel yazma",
            RiskTier.R4: "yuksek etkili/kisitli",
        }[self]


# --- Yetki kapsamlari -------------------------------------------------------
# Tool'lar bu sabitlere referans verir; roller kapsam kumelerine cevrilir.
SCOPE_PLATFORM_READ = "platform.read"
SCOPE_AUDIT_READ = "audit.read"
SCOPE_SAP_READ = "sap.read"
SCOPE_SAP_SIMULATE = "sap.simulate"
SCOPE_SAP_PREPARE = "sap.prepare"
SCOPE_PR_WRITE = "sap.pr.write"
SCOPE_PR_APPROVE = "sap.pr.approve"
SCOPE_PO_WRITE = "sap.po.write"
SCOPE_PO_APPROVE = "sap.po.approve"
SCOPE_REPORT_WRITE = "report.write"

# --- Alan bazli veri erisim kapsamlari -------------------------------------
# Tool erisimi ile ALAN erisimi ayri kararlardir: `sap.read` bir siparisi
# okumaya yeter ama tedarikci e-postasini (D2) veya IBAN'ini (D3) gormeye
# yetmez. Bu kapsamlar gorevler ayrimini alan seviyesine tasir.
SCOPE_DATA_CONFIDENTIAL = "sap.data.confidential"  # D2 okuma
SCOPE_DATA_RESTRICTED = "sap.data.restricted"  # D3 okuma (JIT verilir)
SCOPE_EXPORT_CONFIDENTIAL = "sap.export.confidential"  # D2/D3 disa aktarim
SCOPE_PRIVACY_ADMIN = "privacy.admin"  # saklama/purge/DLP yonetimi

ALL_SCOPES: frozenset[str] = frozenset(
    {
        SCOPE_PLATFORM_READ,
        SCOPE_AUDIT_READ,
        SCOPE_SAP_READ,
        SCOPE_SAP_SIMULATE,
        SCOPE_SAP_PREPARE,
        SCOPE_PR_WRITE,
        SCOPE_PR_APPROVE,
        SCOPE_PO_WRITE,
        SCOPE_PO_APPROVE,
        SCOPE_REPORT_WRITE,
        SCOPE_DATA_CONFIDENTIAL,
        SCOPE_DATA_RESTRICTED,
        SCOPE_EXPORT_CONFIDENTIAL,
        SCOPE_PRIVACY_ADMIN,
    }
)

_READ_BUNDLE = frozenset({SCOPE_PLATFORM_READ, SCOPE_SAP_READ, SCOPE_SAP_SIMULATE})

# Rol -> kapsam. SoD geregi PURCHASER talep hazirlar/olusturur, APPROVER onaylar.
# Ayni rol hem PR_WRITE hem PR_APPROVE tasimaz.
#
# D2 (ticari/kisisel) veri erisimi is rollerine verilir; VIEWER ve
# PLATFORM_ADMIN bilerek disaridadir: ilki salt gozlemci, ikincisi teknik
# roldur ve ticari veriye ihtiyaci yoktur. D3 (`sap.data.restricted`) hicbir
# varsayilan role bagli degildir; yalniz JIT/sureli olarak verilir.
ROLE_SCOPES: dict[str, frozenset[str]] = {
    "VIEWER": frozenset({SCOPE_PLATFORM_READ, SCOPE_SAP_READ}),
    "ENGINEER": _READ_BUNDLE | {SCOPE_SAP_PREPARE, SCOPE_REPORT_WRITE, SCOPE_DATA_CONFIDENTIAL},
    "PURCHASER": _READ_BUNDLE
    | {SCOPE_SAP_PREPARE, SCOPE_PR_WRITE, SCOPE_REPORT_WRITE, SCOPE_DATA_CONFIDENTIAL},
    "BUYER_LEAD": _READ_BUNDLE
    | {
        SCOPE_SAP_PREPARE,
        SCOPE_PR_WRITE,
        SCOPE_PO_WRITE,
        SCOPE_REPORT_WRITE,
        SCOPE_DATA_CONFIDENTIAL,
        SCOPE_EXPORT_CONFIDENTIAL,
    },
    "APPROVER": _READ_BUNDLE | {SCOPE_PR_APPROVE, SCOPE_PO_APPROVE, SCOPE_DATA_CONFIDENTIAL},
    "PROJECT_MANAGER": _READ_BUNDLE
    | {SCOPE_SAP_PREPARE, SCOPE_REPORT_WRITE, SCOPE_DATA_CONFIDENTIAL},
    "AP_CLERK": _READ_BUNDLE | {SCOPE_DATA_CONFIDENTIAL},
    "AUDITOR": frozenset(
        {SCOPE_PLATFORM_READ, SCOPE_SAP_READ, SCOPE_AUDIT_READ, SCOPE_DATA_CONFIDENTIAL}
    ),
    "PLATFORM_ADMIN": frozenset({SCOPE_PLATFORM_READ, SCOPE_AUDIT_READ}),
    "PRIVACY_ADMIN": frozenset({SCOPE_PLATFORM_READ, SCOPE_AUDIT_READ, SCOPE_PRIVACY_ADMIN}),
}


def scopes_for_roles(roles: Iterable[str]) -> frozenset[str]:
    """Rol listesini kapsam kumesine cevirir. Bilinmeyen rol kapsam uretmez."""
    resolved: set[str] = set()
    for role in roles:
        resolved |= ROLE_SCOPES.get(role.strip().upper(), frozenset())
    return frozenset(resolved)


def unknown_roles(roles: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({r.strip().upper() for r in roles} - set(ROLE_SCOPES)))


@dataclass(frozen=True)
class ActorContext:
    """Dogrulanmis kullanici kimligi ve organizasyon kapsami.

    `scopes` roller uzerinden turetilir; dogrudan verilen kapsamlar rollerin
    uzerine eklenir ama rollerin vermedigi bir kapsami tek basina uretmek icin
    `explicit_scopes` bilerek ayri tutulur (audit'te gorunur olmasi icin).
    """

    subject: str
    tenant: str
    roles: tuple[str, ...] = ()
    explicit_scopes: frozenset[str] = frozenset()
    company_codes: frozenset[str] = frozenset()
    plants: frozenset[str] = frozenset()
    purchasing_orgs: frozenset[str] = frozenset()
    auth_method: str = "none"
    display_name: str = ""

    @property
    def scopes(self) -> frozenset[str]:
        return scopes_for_roles(self.roles) | self.explicit_scopes

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def missing_scopes(self, required: Iterable[str]) -> tuple[str, ...]:
        owned = self.scopes
        return tuple(sorted(s for s in required if s not in owned))

    # --- ABAC: organizasyon kapsami ----------------------------------------
    @staticmethod
    def _permits(allowed: frozenset[str], value: str | None) -> bool:
        """Bos deger izin ANLAMINA GELMEZ.

        Argumanda tesis verilmemis olmasi, handler'in sistem varsayilanini
        kullanacagi anlamina gelir. O varsayilan degerin de kapsamda olup
        olmadigini policy ayrica kontrol eder (`PolicyDecisionPoint`); burada
        `None` "kontrol edilecek deger yok" demektir, "serbest" demek degildir.
        """
        if value is None or value == "":
            return True
        return ORG_WILDCARD in allowed or value in allowed

    def permits_plant(self, plant: str | None) -> bool:
        return self._permits(self.plants, plant)

    def permits_company_code(self, code: str | None) -> bool:
        return self._permits(self.company_codes, code)

    def permits_purchasing_org(self, org: str | None) -> bool:
        return self._permits(self.purchasing_orgs, org)

    def permits(self, kind: str, value: str | None) -> bool:
        return {
            "plant": self.permits_plant,
            "company_code": self.permits_company_code,
            "purchasing_org": self.permits_purchasing_org,
        }[kind](value)

    @property
    def has_any_org_scope(self) -> bool:
        """Actor herhangi bir organizasyon kapsami tasiyor mu?

        Uretim profilinde bos kapsam = hicbir tesis/sirket kodu demektir; bu
        durumda SAP okuma/yazma tool'lari reddedilir (fail-closed).
        """
        return bool(self.company_codes or self.plants or self.purchasing_orgs)

    def with_roles(self, *roles: str) -> ActorContext:
        return replace(self, roles=tuple(r.upper() for r in roles))

    def to_dict(self, *, include_scopes: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subject": self.subject,
            "tenant": self.tenant,
            "roles": list(self.roles),
            "auth_method": self.auth_method,
        }
        if include_scopes:
            payload["scopes"] = sorted(self.scopes)
            payload["org_scope"] = {
                "company_codes": sorted(self.company_codes),
                "plants": sorted(self.plants),
                "purchasing_orgs": sorted(self.purchasing_orgs),
            }
        return payload

    # --- Fabrikalar ---------------------------------------------------------
    @classmethod
    def local_operator(
        cls,
        *,
        subject: str = "local-operator",
        tenant: str = "100",
        roles: Iterable[str] = ("VIEWER", "PURCHASER"),
        company_code: str = "",
        plant: str = "",
        purchasing_org: str = "",
    ) -> ActorContext:
        """CLI/demo gibi yerel kanallarin actor'u.

        Onay gerektiren R3/R4 tool'lar bu actor icin de onay kanitini zorunlu
        kilar; yerel olmak yazma yetkisi vermez.
        """
        return cls(
            subject=subject,
            tenant=tenant,
            roles=tuple(r.upper() for r in roles),
            company_codes=frozenset({company_code} if company_code else {ORG_WILDCARD}),
            plants=frozenset({plant} if plant else {ORG_WILDCARD}),
            purchasing_orgs=frozenset({purchasing_org} if purchasing_org else {ORG_WILDCARD}),
            auth_method="local",
            display_name="Yerel operator",
        )

    @classmethod
    def anonymous(cls, *, tenant: str = "100") -> ActorContext:
        """Kimligi dogrulanmamis cagirici: hicbir kapsam tasimaz."""
        return cls(subject="anonymous", tenant=tenant, roles=(), auth_method="none")


@dataclass
class ExecutionContext:
    """Bir kullanici turunun/isleminin izlenebilir kimligi."""

    actor: ActorContext
    system_alias: str
    execution_id: str = field(default_factory=lambda: f"exec-{uuid.uuid4().hex[:16]}")
    correlation_id: str = field(default_factory=lambda: f"corr-{uuid.uuid4().hex[:16]}")
    channel: str = "cli"
    dry_run: bool = True
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: str = ""
    # Bir turda cagrilan tool'lar; SoD ve iterasyon siniri icin kullanilir.
    tool_trail: list[str] = field(default_factory=list)

    def record_tool(self, name: str) -> None:
        self.tool_trail.append(name)

    def new_step(self) -> str:
        """Adim bazli correlation ID: SAP cagrisi ile audit kaydini eslestirir."""
        return f"{self.correlation_id}.{len(self.tool_trail) + 1}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "correlation_id": self.correlation_id,
            "channel": self.channel,
            "system_alias": self.system_alias,
            "dry_run": self.dry_run,
            "started_at": self.started_at.isoformat(),
            "session_id": self.session_id,
            "actor": self.actor.to_dict(),
        }
