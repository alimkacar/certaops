"""`ImpactProfile`: bir tool'un statik etki sozlesmesi.

Onceki modelde risk tek bir etiketti (`RiskTier.R3`). Bu, "10 kalemlik 500 EUR
tutarli bir PR" ile "300 kalemlik 2 milyon EUR tutarli bir PR"i ayni kefeye
koyuyordu. `ImpactProfile` tool'un **degismeyen** ozelliklerini bildirir;
gercek etki `scoring.py` icinde cagri aninda hesaplanir.

Ayrim onemlidir:
    ImpactProfile  -> gelistiricinin bildirdigi, kod surumuyle degisen taban.
    ImpactSignals  -> cagri aninda SAP'ten/prepare kaydindan dogrulanan olcum.

Statik taban runtime tarafindan **dusurulemez**. Bir tool'un
`RiskTier.R3` olmasi, kucuk tutarli bir cagrida R1'e inebilecegi anlamina
gelmez; yalnizca yukari yonlu yukselme mumkundur.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = [
    "ImpactProfile",
    "MutationKind",
    "Reversibility",
]


class MutationKind(str, Enum):
    """Tool'un SAP uzerindeki eylem tipi ve etki boyutu."""

    READ = "read"
    COMPUTE = "compute"
    DRAFT = "draft"
    WRITE = "write"
    BULK_WRITE = "bulk_write"
    DESTRUCTIVE = "destructive"

    @property
    def action_points(self) -> int:
        """0-60 arasi eylem etkisi puani."""
        return {
            MutationKind.READ: 0,
            MutationKind.COMPUTE: 10,
            MutationKind.DRAFT: 25,
            MutationKind.WRITE: 45,
            MutationKind.BULK_WRITE: 60,
            MutationKind.DESTRUCTIVE: 60,
        }[self]

    @property
    def is_mutating(self) -> bool:
        return self in {
            MutationKind.WRITE,
            MutationKind.BULK_WRITE,
            MutationKind.DESTRUCTIVE,
        }

    @property
    def forces_max_tier(self) -> bool:
        """Bulk ve geri donussuz islemler icin R4 tabanini zorlar."""
        return self in {MutationKind.BULK_WRITE, MutationKind.DESTRUCTIVE}


class Reversibility(str, Enum):
    """Risk skorundaki islemin geri alinabilirlik boyutu."""

    EASY = "true"  # taslak silinir, cache temizlenir
    COMPENSATING = "compensating"  # ters kayit/iptal belgesi gerekir
    IRREVERSIBLE = "false"  # geri donusu yok

    @property
    def points(self) -> int:
        return {
            Reversibility.EASY: 0,
            Reversibility.COMPENSATING: 5,
            Reversibility.IRREVERSIBLE: 15,
        }[self]


@dataclass(frozen=True)
class ImpactProfile:
    """Bir tool'un statik etki sozlesmesi.

    Alanlar:
        mutation             Eylem tipi.
        reversible           Geri alinabilirlik.
        financial_fields     Tutarin okunacagi sonuc/arguman alanlari. Sirayla
                             denenir; **model argumani tek basina yeterli
                             kanit sayilmaz** (bkz. scoring.ImpactSignals).
        record_count_field   Kayit sayisinin okunacagi alan.
        external_commitment  Islem uculuncu tarafa taahhut uretiyor mu
                             (PO tedarikciye taahhuttur, PR degildir).
        privileged_master_data  Vendor/banka/yetki gibi ayricalikli ana veriye
                             dokunuyor mu.
        period_sensitive     Donem kapanisi/yasal belge etkisi olabilir mi.
    """

    mutation: MutationKind = MutationKind.READ
    reversible: Reversibility = Reversibility.EASY
    financial_fields: tuple[str, ...] = ()
    record_count_field: str = ""
    external_commitment: bool = False
    privileged_master_data: bool = False
    period_sensitive: bool = False

    @property
    def is_mutating(self) -> bool:
        return self.mutation.is_mutating

    def validate(self, *, risk_tier_level: int) -> list[str]:
        """Sozlesme tutarliligi; tool kaydi sirasinda cagrilir.

        Amac: bir yazma tool'unun kendini `mutation=read` bildirerek runtime
        skorlamasindan kacmasini engellemek.
        """
        problems: list[str] = []
        if risk_tier_level >= 3 and not self.is_mutating:
            problems.append(
                f"R{risk_tier_level} bir tool impact_profile.mutation='{self.mutation.value}' "
                "bildiremez; write/bulk_write/destructive olmali."
            )
        if self.is_mutating and risk_tier_level < 2:
            problems.append(
                f"mutation='{self.mutation.value}' bildiren tool en az R2 olmali "
                f"(su an R{risk_tier_level})."
            )
        if self.mutation is MutationKind.DESTRUCTIVE and self.reversible is Reversibility.EASY:
            problems.append(
                "destructive bir islem reversible='true' olamaz."
            )
        return problems

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutation": self.mutation.value,
            "reversible": self.reversible.value,
            "financial_fields": list(self.financial_fields),
            "record_count_field": self.record_count_field,
            "external_commitment": self.external_commitment,
            "privileged_master_data": self.privileged_master_data,
            "period_sensitive": self.period_sensitive,
        }


# Sik kullanilan hazir profiller: her salt-okunur tool'un ayni dort satiri
# tekrarlamasi gerekmesin.
READ_ONLY = ImpactProfile(mutation=MutationKind.READ, reversible=Reversibility.EASY)
COMPUTE_ONLY = ImpactProfile(mutation=MutationKind.COMPUTE, reversible=Reversibility.EASY)
