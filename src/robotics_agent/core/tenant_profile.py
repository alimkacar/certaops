"""Sirkete ozgu SAP gerceklerinin KOD DEGIL VERI hali.

Neden var
---------
Ayni SAP modulu her sirkette ayni sozlesmeye sahiptir - `API_PURCHASEREQUISITION_2`
her S/4HANA'da ayni entity set'leri ve alan adlarini tasir. Degisen sozlesme
degil **yapilandirmadir**:

  * hangi belge tipi kullaniliyor (NB / ZNB / Z01),
  * hangi alanlar zorunlu (field selection sirkete gore ayarlanir),
  * hangi varsayilanlar dolduruluyor (kaynak tayini kategorisi gibi),
  * hangi Z-alani hangi ise yariyor.

Bunlarin hicbiri `$metadata`'dan okunamaz: metadata bir alanin VAR oldugunu
soyler, sirketin onu ZORUNLU yaptigini soylemez. Dolayisiyla iki secenek
vardir - ya her musteri icin kod degistirilir, ya da bu gercekler veri olarak
tutulur. Bu modul ikincisini secer.

Ogrenme
-------
Ongorulemeyen tek sinif BAdI davranisidir: bir sirketin enhancement'i baska
yerde gecerli olan bir talebi reddedebilir. Bu tahmin edilemez, yalnizca
**gozlenebilir**. `SapRejectionLog` SAP'in reddini kaydeder; operator bunu
gorup profile zorunlu alan olarak yukseltebilir. Boylece ikinci kez ayni
duvara carpilmaz: red, yazmadan ONCE blocking finding'e doner.

Otomatik yukseltme bilerek YAPILMAZ. Tek seferlik bir red bir kural degildir;
kurala donusturme karari insanindir.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

#: Profil verilmemis kurulumlarda kullanilan SAP standardi.
DEFAULT_DOCUMENT_TYPE = "NB"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SapTenantProfile:
    """Tek bir tenant'in SAP yapilandirma gercekleri.

    Degismez: bir tur boyunca sabit kalir, degisiklik depoya yazilir ve
    sonraki turda cozulur.
    """

    tenant: str = ""
    #: Satinalma talebi belge tipi. SAP standardi NB'dir; sirketler kendi
    #: tiplerini tanimlar (ZNB, Z01...). Yanlis tip ilk yazmada reddedilir.
    document_type: str = DEFAULT_DOCUMENT_TYPE
    #: Yazmadan ONCE dolu olmasi gereken alanlar. Kaynagi ya operator
    #: tanimidir ya da SAP'in gecmis reddinden yukseltilmis bir kuraldir.
    required_fields: tuple[str, ...] = ()
    #: Eksikse otomatik doldurulacak degerler (or. kaynak tayini "K").
    defaults: tuple[tuple[str, str], ...] = ()
    #: Bizim alan adimiz -> hedef sistemdeki SAP alan adi. Z-alanlari ve
    #: extensibility alanlari icin; tahmin edilemez, bildirilmek zorundadir.
    field_map: tuple[tuple[str, str], ...] = ()
    updated_at: str = ""

    # --- Turetilmis gorunumler ---------------------------------------------
    @property
    def default_values(self) -> dict[str, str]:
        return dict(self.defaults)

    @property
    def field_mapping(self) -> dict[str, str]:
        return dict(self.field_map)

    def sap_field(self, name: str) -> str:
        """Bizim alan adimizin hedef sistemdeki karsiligi."""
        return self.field_mapping.get(name, name)

    def missing_required(self, payload: Mapping[str, Any]) -> tuple[str, ...]:
        """Zorunlu olup payload'da bos olan alanlar.

        Bos string ve None eksik sayilir; 0 ve False **sayilmaz** (gecerli
        is degerleridir).
        """
        missing: list[str] = []
        for name in self.required_fields:
            value = payload.get(self.sap_field(name), payload.get(name))
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(name)
        return tuple(missing)

    def apply_defaults(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Eksik alanlari profil varsayilanlariyla doldurur (uzerine yazmaz)."""
        out = dict(payload)
        for name, value in self.defaults:
            key = self.sap_field(name)
            if out.get(key) in (None, ""):
                out[key] = value
        return out

    def describe(self) -> dict[str, Any]:
        """Teshis gorunumu (health/capability ciktisi icin)."""
        return {
            "tenant": self.tenant,
            "document_type": self.document_type,
            "required_fields": list(self.required_fields),
            "defaults": self.default_values,
            "field_map": self.field_mapping,
            "updated_at": self.updated_at,
            "source": "profile" if self.updated_at else "default",
        }

    # --- Kurucular ----------------------------------------------------------
    @classmethod
    def default(cls, tenant: str = "") -> SapTenantProfile:
        """Profil tanimlanmamis kurulum: SAP standardi davranis."""
        return cls(tenant=tenant)

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> SapTenantProfile:
        def _load(raw: Any, fallback: Any) -> Any:
            try:
                return json.loads(raw) if raw else fallback
            except (TypeError, ValueError):
                log.warning("Tenant profilinde bozuk JSON; varsayilan kullanilacak.")
                return fallback

        defaults = _load(row.get("defaults_json"), {})
        field_map = _load(row.get("field_map_json"), {})
        required = _load(row.get("required_fields_json"), [])
        return cls(
            tenant=str(row.get("tenant") or ""),
            document_type=str(row.get("document_type") or DEFAULT_DOCUMENT_TYPE),
            required_fields=tuple(str(x) for x in required),
            defaults=tuple((str(k), str(v)) for k, v in dict(defaults).items()),
            field_map=tuple((str(k), str(v)) for k, v in dict(field_map).items()),
            updated_at=str(row.get("updated_at") or ""),
        )


@dataclass
class RejectionRecord:
    """SAP'in bir yazmayi neden reddettigi - gozlemlenmis gercek."""

    tenant: str
    tool: str
    sap_code: str = ""
    field: str = ""
    message: str = ""
    seen_count: int = 1
    # NOT: bu sinifin `field` adli bir alani var ve sinif govdesinde
    # `dataclasses.field`i golgeliyor. Tam adiyla cagirmak zorunlu - aksi
    # halde `''(default_factory=...)` denenir ve sinif kurulurken patlar.
    first_seen: str = dataclasses.field(default_factory=_now)
    last_seen: str = dataclasses.field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "sap_code": self.sap_code,
            "field": self.field,
            "message": self.message,
            "seen_count": self.seen_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }
