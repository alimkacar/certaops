"""Arayuzden degistirilebilecek ayarlarin bildirimsel listesi.

Bu dosya bir **izin listesidir**, bir kolaylik tablosu degil. Burada olmayan
hicbir ayar arayuzden degistirilemez ve yeni bir satir eklemek bilincli bir
guvenlik kararidir.

Listede BULUNMAYANLAR ve nedenleri:

  Kimlik ve yetki   `AGENT_AUTH_MODE`, `AGENT_PRINCIPALS_FILE`, OIDC ayarlari.
                    Bir tarayici ucu kendi kimlik dogrulamasini kapatabilir
                    olamaz; uretim kapisinin korudugu tam olarak budur.
  SAP kimlik bilgisi  kullanici/parola, OAuth sirlari, API anahtari.
                    Sir yazan bir uc, sir sizdiran bir uctur.
  Egress allowlist  `SAP_ALLOWED_HOSTS`. Ajanin hangi hosta baglanabilecegi
                    deployment karari; tarayicidan genisletilemez.
  `SAP_VERIFY_SSL`  Kapatilabilir olmasi TLS'i anlamsizlastirir.
  Maskeleme anahtarlari  `LOG_MASK`, `AGENT_MASK_PREVIEWS`. Bunlar oldurucu
                    anahtar (kill switch); uzaktan kapatilamamalidir.
  Model saglayici   API anahtarlari ve `GEMINI_STORE_INTERACTIONS`.

Deger duzeyinde de sinir var: bir ayarin listede olmasi her degerinin kabul
edildigi anlamina gelmez. `AGENT_DLP_MODE` degistirilebilir ama `off`
yapilamaz; `LOG_LEVEL` degistirilebilir ama `DEBUG` secilemez -- DEBUG,
maskeleme kapsaminin dar oldugu bir hatta sir gorunurlugunu artirir.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "SettingSpec",
    "SETTABLE",
    "spec_for",
    "current_value",
    "coerce",
    "SettingError",
]


class SettingError(ValueError):
    """Gecersiz ayar degeri. Mesaj kullaniciya gosterilebilir."""


@dataclass(frozen=True)
class SettingSpec:
    """Tek bir degistirilebilir ayarin sozlesmesi."""

    key: str                      # ortam degiskeni adi = arayuzdeki kimlik
    section: str                  # gruplama: sap / gozlem / veri / onay
    kind: str                     # bool | int | enum
    reader: Callable[[Any], Any]  # Settings -> mevcut deger
    label_tr: str
    label_en: str
    note_tr: str = ""
    note_en: str = ""
    choices: tuple[str, ...] = ()          # enum icin izin verilen degerler
    minimum: int | None = None             # int icin
    maximum: int | None = None
    # `consequential`: gercek SAP'a yazma kapisini veya bir koruma katmanini
    # etkiler; ikinci bir kimligin onayini ister.
    consequential: bool = False
    # `live`: degisiklik calisan surece gercekten uygulanabilir. Cogu ayar
    # nesneler kuruldugu anda yakalandigi icin uygulanamaz; onlar yeniden
    # baslatmayi bekler. Yarim uygulanmis bir yapilandirma, hic uygulanmamis
    # olandan tehlikelidir - bu yuzden yalan soylemiyoruz.
    live: bool = False
    applier: Callable[[Any], None] | None = field(default=None, compare=False)


def _apply_log_level(value: Any) -> None:
    """Log seviyesi gercekten canli uygulanabilen tek ayar."""
    import logging

    logging.getLogger().setLevel(str(value).upper())


SETTABLE: dict[str, SettingSpec] = {
    # --- SAP calisma kipi -------------------------------------------------
    "SAP_BACKEND": SettingSpec(
        key="SAP_BACKEND", section="sap", kind="enum",
        reader=lambda s: s.sap.backend,
        choices=("mock", "odata", "ecc"),
        label_tr="SAP arka ucu", label_en="SAP backend",
        note_tr="`mock` simülasyon verisi üretir. Canlıya geçmek gerçek SAP "
                "sistemine bağlanmak demektir.",
        note_en="`mock` produces simulated data. Going live connects to the "
                "real SAP system.",
        consequential=True,
    ),
    "SAP_DRY_RUN": SettingSpec(
        key="SAP_DRY_RUN", section="sap", kind="bool",
        reader=lambda s: s.sap.dry_run,
        label_tr="Kuru çalışma", label_en="Dry run",
        note_tr="Kapatmak SAP'a gerçek yazmayı açar. Bu ayar tek başına "
                "geri alınamaz sonuçlar doğurur.",
        note_en="Turning this off enables real writes to SAP. This setting "
                "alone produces irreversible outcomes.",
        consequential=True,
    ),
    "SAP_PAGE_SIZE": SettingSpec(
        key="SAP_PAGE_SIZE", section="sap", kind="int",
        reader=lambda s: s.sap.page_size, minimum=1, maximum=500,
        label_tr="Sayfa boyutu", label_en="Page size",
    ),
    "SAP_MAX_PAGES": SettingSpec(
        key="SAP_MAX_PAGES", section="sap", kind="int",
        reader=lambda s: s.sap.max_pages, minimum=1, maximum=50,
        label_tr="Azami sayfa", label_en="Max pages",
    ),

    # --- Onay ve koruma katmanlari ---------------------------------------
    "AGENT_APPROVAL_GATEWAY": SettingSpec(
        key="AGENT_APPROVAL_GATEWAY", section="onay", kind="enum",
        reader=lambda s: s.security.approval_gateway,
        choices=("local", "bpa"),
        label_tr="Onay geçidi", label_en="Approval gateway",
        note_tr="`local` yalnız geliştirme içindir; gerçek yazma doğrulanmış "
                "bir geçit (bpa) ister.",
        note_en="`local` is for development only; real writes require a "
                "verified gateway (bpa).",
        consequential=True,
    ),
    "AGENT_APPROVAL_TTL_MIN": SettingSpec(
        key="AGENT_APPROVAL_TTL_MIN", section="onay", kind="int",
        reader=lambda s: s.security.approval_ttl_minutes, minimum=5, maximum=1440,
        label_tr="Onay geçerlilik süresi (dk)", label_en="Approval TTL (min)",
    ),
    "AGENT_RISK_SCORING_MODE": SettingSpec(
        key="AGENT_RISK_SCORING_MODE", section="onay", kind="enum",
        reader=lambda s: s.risk.scoring_mode,
        choices=("enforce", "report"),
        label_tr="Risk skorlama kipi", label_en="Risk scoring mode",
        note_tr="`report` skoru hesaplar ama uygulamaz.",
        note_en="`report` computes the score without enforcing it.",
        consequential=True,
    ),

    # --- Veri korumasi ----------------------------------------------------
    "AGENT_DLP_MODE": SettingSpec(
        key="AGENT_DLP_MODE", section="veri", kind="enum",
        reader=lambda s: s.privacy.dlp_mode,
        # `off` bilerek yok: DLP'yi tarayicidan kapatmak mumkun olmamali.
        choices=("enforce", "report"),
        label_tr="DLP kipi", label_en="DLP mode",
        note_tr="`report` bulguları kaydeder ama alanları temizlemez. "
                "`off` bu uçtan seçilemez.",
        note_en="`report` records findings without redacting. `off` cannot "
                "be selected from this endpoint.",
        consequential=True,
    ),
    "AGENT_EVIDENCE_TTL_MIN": SettingSpec(
        key="AGENT_EVIDENCE_TTL_MIN", section="veri", kind="int",
        reader=lambda s: s.state.evidence_ttl_minutes, minimum=5, maximum=1440,
        label_tr="Kanıt saklama (dk)", label_en="Evidence retention (min)",
    ),
    "AGENT_SESSION_TTL_HOURS": SettingSpec(
        key="AGENT_SESSION_TTL_HOURS", section="veri", kind="int",
        reader=lambda s: s.state.session_ttl_hours, minimum=1, maximum=168,
        label_tr="Oturum saklama (saat)", label_en="Session retention (h)",
    ),

    # --- Gozlemlenebilirlik ve basim --------------------------------------
    "LOG_LEVEL": SettingSpec(
        key="LOG_LEVEL", section="gozlem", kind="enum",
        reader=lambda s: s.logging.level,
        # DEBUG bilerek yok: maskeleme kapsami dar oldugu surece DEBUG sir
        # gorunurlugunu artirir.
        choices=("INFO", "WARNING", "ERROR"),
        label_tr="Log seviyesi", label_en="Log level",
        note_tr="`DEBUG` bu uçtan seçilemez.",
        note_en="`DEBUG` cannot be selected from this endpoint.",
        live=True, applier=_apply_log_level,
    ),
    "AGENT_UI_MAX_PAGE_SIZE": SettingSpec(
        key="AGENT_UI_MAX_PAGE_SIZE", section="gozlem", kind="int",
        reader=lambda s: s.ui.max_page_size, minimum=1, maximum=1000,
        label_tr="Arayüz sayfa tavanı", label_en="UI page cap",
    ),
    "AGENT_CACHE_DEFAULT_TTL_SECONDS": SettingSpec(
        key="AGENT_CACHE_DEFAULT_TTL_SECONDS", section="gozlem", kind="int",
        reader=lambda s: s.cache.default_ttl_seconds, minimum=0, maximum=3600,
        label_tr="Önbellek TTL (sn)", label_en="Cache TTL (s)",
    ),
    "AGENT_CACHE_MAX_ENTRIES": SettingSpec(
        key="AGENT_CACHE_MAX_ENTRIES", section="gozlem", kind="int",
        reader=lambda s: s.cache.max_entries, minimum=50, maximum=5000,
        label_tr="Önbellek kayıt tavanı", label_en="Cache max entries",
    ),
}

SECTION_LABELS: dict[str, tuple[str, str]] = {
    "sap": ("SAP calisma kipi", "SAP operating mode"),
    "onay": ("Onay ve risk", "Approval and risk"),
    "veri": ("Veri korumasi", "Data protection"),
    "gozlem": ("Gozlem ve basim", "Observability and paging"),
}


def spec_for(key: str) -> SettingSpec:
    """Izin listesinde olmayan anahtar burada durur."""
    spec = SETTABLE.get(key)
    if spec is None:
        raise SettingError(
            f"'{key}' arayuzden degistirilebilir ayarlar listesinde degil."
        )
    return spec


def current_value(settings: Any, spec: SettingSpec) -> Any:
    try:
        return spec.reader(settings)
    except AttributeError:  # pragma: no cover - ayar agaci degisirse
        return None


def coerce(spec: SettingSpec, raw: Any) -> str:
    """Ham girdiyi ortam degiskeni metnine cevirir; gecersizse yukseltir.

    Donen deger her zaman `str`, cunku ayarlar ortam degiskeni olarak
    saklaniyor ve `Settings` onlari oradan okuyor.
    """
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return "true" if raw else "false"
        metin = str(raw).strip().lower()
        if metin in {"true", "1", "yes", "on"}:
            return "true"
        if metin in {"false", "0", "no", "off"}:
            return "false"
        raise SettingError(f"{spec.key}: 'true' veya 'false' olmali ('{raw}' verildi).")

    if spec.kind == "int":
        try:
            sayi = int(str(raw).strip())
        except (TypeError, ValueError):
            raise SettingError(f"{spec.key}: tam sayi olmali ('{raw}' verildi).") from None
        if spec.minimum is not None and sayi < spec.minimum:
            raise SettingError(f"{spec.key}: en az {spec.minimum} olmali ({sayi} verildi).")
        if spec.maximum is not None and sayi > spec.maximum:
            raise SettingError(f"{spec.key}: en fazla {spec.maximum} olmali ({sayi} verildi).")
        return str(sayi)

    if spec.kind == "enum":
        metin = str(raw).strip().lower()
        # Enum degerleri kucuk harf saklanir; LOG_LEVEL istisnasi buyuk harf.
        secenekler = {c.lower(): c for c in spec.choices}
        if metin not in secenekler:
            raise SettingError(
                f"{spec.key}: gecerli degerler {', '.join(spec.choices)} "
                f"('{raw}' verildi)."
            )
        return secenekler[metin]

    raise SettingError(f"{spec.key}: bilinmeyen tur '{spec.kind}'.")
