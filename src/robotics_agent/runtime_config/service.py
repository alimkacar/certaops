"""Ayar degisikliginin gectigi kapi.

Uc kontrol, bu sirayla:

  1. **Izin listesi ve deger siniri** -- `registry.py`. Listede olmayan
     anahtar ve sinir disi deger buraya hic ulasmaz.

  2. **Uretim engeli mandali** -- degisiklik uygulanmadan once aday bir
     `Settings` kurulur ve `production_blockers()` yeniden hesaplanir.
     Degisiklik YENI bir engel doguruyorsa reddedilir. Bu, projenin kendi
     uretim kapisini calisma zamanina tasir: uyumlu bir deployment arayuz
     uzerinden uyumsuz hale getirilemez. Mandal tek yonludur -- var olan bir
     engeli kaldiran degisiklik serbesttir, yeni engel doguran degil.

  3. **Iki kisi kurali** -- sonuc doguran ayarlar (gercek SAP'a yazma
     kapisini veya bir koruma katmanini etkileyenler) ikinci bir kimligin
     onayini bekler. Oneren onaylayamaz; kural tool onaylarindaki SoD ile
     ayni.

Her adim -- oneri, onay, ret, uygulama -- denetim defterine `before`/`after`
ile yazilir. Reddedilen bir degisiklik de yazilir: bir kapinin kac kez
zorlandigi, kapinin calistigi kadar onemli bir bilgidir.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .registry import SETTABLE, SettingSpec, coerce, current_value, spec_for
from .store import (
    overrides_path,
    pinned_by_environment,
    read_document,
    read_overrides,
    write_document,
)

log = logging.getLogger(__name__)

# Ayar dosyasi oku-degistir-yaz dongusuyle guncelleniyor ve FastAPI senkron
# uclari bir is parcacigi havuzunda kosuyor. Kilit olmadan iki es zamanli
# degisiklikten biri sessizce kaybolur -- guvenlik ayarinda kabul edilemez.
_YAZMA_KILIDI = threading.RLock()

__all__ = [
    "ConfigService",
    "ConfigRefused",
    "ChangeOutcome",
]


class ConfigRefused(Exception):
    """Kapi degisikligi reddetti. `code` makine icin, `message` insan icin."""

    def __init__(self, code: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


@dataclass(frozen=True)
class ChangeOutcome:
    """Bir oneri sonrasi ne oldugu."""

    status: str            # applied | pending_approval | staged
    key: str
    before: Any
    after: Any
    change_id: str = ""
    restart_required: bool = False
    message: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConfigService:
    """Ayar okuma/degistirme mantigi. Kanal (HTTP) bilgisi tasimaz."""

    def __init__(self, settings: Any, audit: Any = None) -> None:
        self._settings = settings
        self._audit = audit

    # --- Okuma ------------------------------------------------------------
    def describe(self, actor: Any, lang: str = "tr") -> dict[str, Any]:
        """Arayuzun cizecegi tablo: her ayar, degeri, sinirlari, kilidi."""
        from robotics_agent.contracts.actor import SCOPE_PLATFORM_CONFIG

        yetkili = bool(actor is not None and actor.has_scope(SCOPE_PLATFORM_CONFIG))
        depo = read_overrides()
        bekleyen = self._pending()

        alanlar = []
        for key, spec in SETTABLE.items():
            sabit = pinned_by_environment(key)
            kayit = depo.get(key) or {}
            alanlar.append(
                {
                    "key": key,
                    "section": spec.section,
                    "kind": spec.kind,
                    "label": spec.label_tr if lang == "tr" else spec.label_en,
                    "note": spec.note_tr if lang == "tr" else spec.note_en,
                    "choices": list(spec.choices),
                    "minimum": spec.minimum,
                    "maximum": spec.maximum,
                    "value": current_value(self._settings, spec),
                    "consequential": spec.consequential,
                    "live": spec.live,
                    # Neden degistirilemiyor -- arayuz bunu aynen gosterir.
                    "locked": (not yetkili) or sabit,
                    "locked_reason": (
                        "env_pinned" if sabit else ("" if yetkili else "no_scope")
                    ),
                    "overridden": key in depo,
                    "changed_by": kayit.get("changed_by", ""),
                    "changed_at": kayit.get("changed_at", ""),
                    "pending": bekleyen.get(key, {}).get("change_id", ""),
                }
            )

        return {
            "can_change": yetkili,
            "required_scope": SCOPE_PLATFORM_CONFIG,
            "app_env": self._settings.app_env,
            "production_ready": not self._settings.production_blockers(),
            "production_blockers": self._settings.production_blockers(),
            "overrides_file": str(overrides_path()),
            "settings": alanlar,
            "pending": list(bekleyen.values()),
        }

    # --- Bekleyen degisiklikler -------------------------------------------
    def _pending(self) -> dict[str, dict[str, Any]]:
        """Onay bekleyen degisiklikler. Izin listesi okurken de gecerli."""
        bekleyen = read_document().get("pending")
        if not isinstance(bekleyen, dict):
            return {}
        return {k: v for k, v in bekleyen.items() if k in SETTABLE}

    def _save(self, settings_map: dict[str, Any], pending: dict[str, Any]) -> None:
        """Ayarlari ve bekleyenleri TEK atomik yazmada kaydeder.

        Iki ayri yazma, aralarinda cokme halinde bekleyen bir degisikligin
        uygulanmis ama listede kalmis gorunmesine yol acardi.
        """
        write_document(
            {
                "settings": {k: v for k, v in settings_map.items() if k in SETTABLE},
                "pending": {k: v for k, v in pending.items() if k in SETTABLE},
            }
        )

    # --- Mandal -----------------------------------------------------------
    def _ratchet(self, key: str, deger: str) -> None:
        """Aday yapilandirmayi kurup uretim engellerini yeniden hesaplar.

        Bu, izin listesinden BAGIMSIZ ikinci bir savunma. Izin listesi
        "hangi ayar" sorusunu, mandal "bu deger bizi guvensiz hale getirir
        mi" sorusunu yanitlar.
        """
        from robotics_agent.config import Settings

        onceki_engeller = set(self._settings.production_blockers())

        eski = os.environ.get(key)
        os.environ[key] = deger
        try:
            aday = Settings()
            yeni_engeller = set(aday.production_blockers())
        except Exception as exc:  # noqa: BLE001 - gecersiz kombinasyon
            raise ConfigRefused(
                "INVALID_COMBINATION",
                f"Bu deger yapilandirmayi gecersiz kiliyor: {exc}",
            ) from exc
        finally:
            if eski is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = eski

        dogan = yeni_engeller - onceki_engeller
        if dogan:
            raise ConfigRefused(
                "WOULD_BREAK_PRODUCTION_GATE",
                "Bu degisiklik uretim kapisini ihlal eden yeni bir engel dogurur "
                "ve reddedildi: " + " | ".join(sorted(dogan)) +
                " -- Mandal tek yonludur: arayuz yapilandirmayi guvenli yonde "
                "degistirebilir, guvensiz yonde degistiremez. Bu degisiklik "
                "gercekten gerekiyorsa .env uzerinden yapilip servis yeniden "
                "baslatilmalidir.",
                detail=sorted(dogan),
            )

    # --- Yazma ------------------------------------------------------------
    def propose(self, key: str, raw: Any, *, actor: Any, reason: str = "") -> ChangeOutcome:
        """Bir ayar degisikligi onerir. Uc kapidan da gecerse uygular."""
        from robotics_agent.contracts.actor import SCOPE_PLATFORM_CONFIG

        if actor is None or not actor.has_scope(SCOPE_PLATFORM_CONFIG):
            self._log_refusal(key, "MISSING_SCOPE", actor)
            raise ConfigRefused(
                "MISSING_SCOPE",
                f"Ayar degistirmek icin '{SCOPE_PLATFORM_CONFIG}' kapsami gerekiyor.",
            )

        with _YAZMA_KILIDI:
            return self._propose_locked(key, raw, actor=actor, reason=reason)

    def _propose_locked(self, key: str, raw: Any, *, actor: Any, reason: str) -> ChangeOutcome:
        spec = spec_for(key)                      # 1. izin listesi
        if pinned_by_environment(key):
            raise ConfigRefused(
                "ENV_PINNED",
                f"{key} dis ortam degiskeniyle sabitlenmis; arayuzden "
                "degistirilemez. Kabuk/container tanimini degistirin.",
            )
        deger = coerce(spec, raw)                 # 1. deger siniri
        onceki = current_value(self._settings, spec)
        if str(onceki).lower() == deger.lower():
            return ChangeOutcome(status="unchanged", key=key, before=onceki, after=onceki,
                                 message="Deger zaten bu.")

        self._ratchet(key, deger)                 # 2. mandal

        if spec.consequential:                    # 3. iki kisi kurali
            return self._stage_for_approval(spec, deger, onceki, actor=actor, reason=reason)

        return self._commit(spec, deger, onceki, actor=actor, approver=None, reason=reason)

    def _stage_for_approval(
        self, spec: SettingSpec, deger: str, onceki: Any, *, actor: Any, reason: str
    ) -> ChangeOutcome:
        change_id = "cfg_" + secrets.token_urlsafe(9)
        bekleyen = self._pending()
        bekleyen[spec.key] = {
            "change_id": change_id,
            "key": spec.key,
            "value": deger,
            "before": onceki,
            "requested_by": getattr(actor, "subject", ""),
            "requested_at": _now(),
            "reason": reason,
        }
        self._save(read_overrides(), bekleyen)
        self._audit_event(
            "platform.config.proposed", spec.key, onceki, deger, actor,
            outcome="needs_review", change_id=change_id, reason=reason,
        )
        log.info("ayar degisikligi onaya sunuldu | %s | %s", spec.key, change_id)
        return ChangeOutcome(
            status="pending_approval", key=spec.key, before=onceki, after=deger,
            change_id=change_id, restart_required=not spec.live,
            message="Sonuc doguran ayar: ikinci bir kimligin onayi bekleniyor.",
        )

    def approve(self, change_id: str, *, actor: Any) -> ChangeOutcome:
        """Bekleyen bir degisikligi ikinci kimlikle onaylar."""
        from robotics_agent.contracts.actor import SCOPE_PLATFORM_CONFIG

        if actor is None or not actor.has_scope(SCOPE_PLATFORM_CONFIG):
            raise ConfigRefused(
                "MISSING_SCOPE",
                f"Onay icin '{SCOPE_PLATFORM_CONFIG}' kapsami gerekiyor.",
            )

        with _YAZMA_KILIDI:
            return self._approve_locked(change_id, actor=actor)

    def _approve_locked(self, change_id: str, *, actor: Any) -> ChangeOutcome:
        bekleyen = self._pending()
        kayit = next((v for v in bekleyen.values() if v.get("change_id") == change_id), None)
        if kayit is None:
            raise ConfigRefused("UNKNOWN_CHANGE", f"Bekleyen degisiklik yok: {change_id}")

        if kayit.get("requested_by") and kayit["requested_by"] == getattr(actor, "subject", ""):
            self._audit_event(
                "platform.config.rejected", kayit["key"], kayit.get("before"),
                kayit.get("value"), actor, outcome="denied", change_id=change_id,
                reason="SoD",
            )
            raise ConfigRefused(
                "SOD_VIOLATION",
                "Gorevler ayrimi: degisikligi oneren kisi onaylayamaz.",
            )

        spec = spec_for(kayit["key"])
        # Onay aninda mandal YENIDEN calisir: oneriyle onay arasinda baska bir
        # ayar degismis olabilir ve kombinasyon artik guvensiz olabilir.
        self._ratchet(spec.key, str(kayit["value"]))

        bekleyen.pop(spec.key, None)
        sonuc = self._commit(
            spec, str(kayit["value"]), kayit.get("before"), actor=actor,
            approver=getattr(actor, "subject", ""), reason=kayit.get("reason", ""),
            requested_by=kayit.get("requested_by", ""), change_id=change_id,
            pending_after=bekleyen,
        )
        return sonuc

    def cancel(self, change_id: str, *, actor: Any) -> ChangeOutcome:
        """Bekleyen bir degisikligi geri ceker.

        Yetki kurali `propose`/`approve` ile ayni cizgide olmak zorundadir.
        Onceki hali HICBIR kontrol yapmiyordu: salt okuyucu bir kullanici bile
        bekleyen herhangi bir degisikligi iptal edebiliyordu. Bu bir veri
        sizintisi degil, **kontrol reddi**dir - yoneticinin uygulamaya
        calistigi bir sertlestirme ayari tekrar tekrar iptal edilebilirdi.

        Iki mesru iptal edici vardir:
          - Oneriyi yapan kisi (kendi onerisini geri cekmek dogal haktir),
          - `platform.config` kapsamina sahip bir yonetici.
        """
        from robotics_agent.contracts.actor import SCOPE_PLATFORM_CONFIG

        with _YAZMA_KILIDI:
            bekleyen = self._pending()
            kayit = next(
                (v for v in bekleyen.values() if v.get("change_id") == change_id), None
            )
            if kayit is None:
                raise ConfigRefused("UNKNOWN_CHANGE", f"Bekleyen degisiklik yok: {change_id}")

            subject = getattr(actor, "subject", "")
            sahibi = bool(subject) and kayit.get("requested_by") == subject
            yetkili = bool(actor is not None and actor.has_scope(SCOPE_PLATFORM_CONFIG))
            if not (sahibi or yetkili):
                raise ConfigRefused(
                    "MISSING_SCOPE",
                    "Baskasinin onerisini geri cekmek icin "
                    f"'{SCOPE_PLATFORM_CONFIG}' kapsami gerekiyor.",
                )
            bekleyen.pop(kayit["key"], None)
        self._save(read_overrides(), bekleyen)
        self._audit_event(
            "platform.config.cancelled", kayit["key"], kayit.get("before"),
            kayit.get("value"), actor, outcome="cancelled", change_id=change_id,
        )
        return ChangeOutcome(status="cancelled", key=kayit["key"],
                             before=kayit.get("before"), after=kayit.get("value"),
                             change_id=change_id)

    def _commit(
        self, spec: SettingSpec, deger: str, onceki: Any, *, actor: Any,
        approver: str | None, reason: str = "", requested_by: str = "",
        change_id: str = "", pending_after: dict[str, Any] | None = None,
    ) -> ChangeOutcome:
        depo = read_overrides()
        depo[spec.key] = {
            "value": deger,
            "changed_by": requested_by or getattr(actor, "subject", ""),
            "changed_at": _now(),
            "approved_by": approver or "",
            "reason": reason,
        }
        self._save(depo, pending_after if pending_after is not None else self._pending())

        uygulandi = False
        if spec.live and spec.applier is not None:
            try:
                spec.applier(deger)
                os.environ[spec.key] = deger
                uygulandi = True
            except Exception as exc:  # noqa: BLE001
                log.warning("ayar canli uygulanamadi | %s | %s", spec.key, exc)

        self._audit_event(
            "platform.config.changed", spec.key, onceki, deger, actor,
            outcome="ok", change_id=change_id, reason=reason, approver=approver,
        )
        log.info(
            "ayar degisti | %s | %s -> %s | %s",
            spec.key, onceki, deger, "canli" if uygulandi else "yeniden baslatmada",
        )
        return ChangeOutcome(
            status="applied" if uygulandi else "staged",
            key=spec.key, before=onceki, after=deger, change_id=change_id,
            restart_required=not uygulandi,
            message=("Uygulandi." if uygulandi
                     else "Kaydedildi; yeniden baslatildiginda gecerli olacak."),
        )

    # --- Denetim ----------------------------------------------------------
    def _audit_event(
        self, event: str, key: str, before: Any, after: Any, actor: Any,
        *, outcome: str = "", change_id: str = "", reason: str = "",
        approver: str | None = None,
    ) -> None:
        if self._audit is None:
            return
        detay: dict[str, Any] = {"setting": key}
        if change_id:
            detay["change_id"] = change_id
        if reason:
            detay["reason"] = reason
        if approver:
            detay["approved_by"] = approver
        try:
            self._audit.append(
                event, actor=actor, outcome=outcome,
                before={key: before}, after={key: after}, detail=detay,
            )
        except Exception as exc:  # noqa: BLE001 - denetim yazamamak islemi durdurmaz
            log.error("ayar degisikligi denetime yazilamadi | %s | %s", key, exc)

    def _log_refusal(self, key: str, code: str, actor: Any) -> None:
        log.warning(
            "ayar degisikligi reddedildi | %s | %s | %s",
            key, code, getattr(actor, "subject", "?"),
        )
        self._audit_event("platform.config.refused", key, None, None, actor,
                          outcome="denied", reason=code)
