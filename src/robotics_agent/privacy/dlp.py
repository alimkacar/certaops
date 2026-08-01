"""Merkezi, model-oncesi DLP servisi.

Onceki surumde maskeleme yalnizca regex tabanliydi ve tek bir yerde (tool
sonucu) uygulaniyordu. Bu modul ikisini de duzeltir:

  1. **Alan adina duyarli.** `supplier_iban` degeri desene uymasa bile alan adi
     D3 oldugu icin tokenlastirilir. Regex "gordugunu" yakalar; alan
     siniflandirmasi "ne oldugunu" bilir.
  2. **Tek servis, cok hedef.** Girdi, tool sonucu, handoff, log ve rapor
     ciktisi ayni motoru kullanir. Hedef (`sink`) karari
     degistirir, kod yolunu degil.

Karar kumesi: allow / mask / tokenize / drop / deny.

`deny` istegin tamamini durdurur (ornegin yetkisiz D3 export). Digerleri
payload'i degistirir ve bulgu uretir; bulgular gizlilik telemetrisine beslenir.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..contracts.actor import ActorContext
from .classification import DataClass, DataPolicy, classify_field
from .field_policy import FieldAccessPolicy, PrivacyAction, Sink
from .pseudonymization import Pseudonymizer, get_pseudonymizer

__all__ = ["DLPEngine", "DLPFinding", "DLPResult", "build_dlp_engine"]

MASK = "***"
_MAX_DEPTH = 12

# --- Serbest metin dedektorleri --------------------------------------------
# Alan adi bilgi vermediginde (ornegin `item_text`, `header_text`, model
# cevabi) devreye girer. Sira onemli: once daha spesifik desenler.
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_IBAN = re.compile(r"\b([A-Z]{2}\d{2})[ ]?(?:[A-Z0-9]{4}[ ]?){2,7}[A-Z0-9]{1,4}\b")
_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
# Son hane ayrac tuketmesin: aksi halde maskeleme sonrasi kelimeler birlesir.
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_TR_TAX = re.compile(r"(?<!\d)(\d{10}|\d{11})(?!\d)")
_PHONE = re.compile(r"(?<!\d)(\+?\d[\d\s()-]{8,}\d)(?!\d)")

# Unicode obfuscation: sifir genislikli ve yon degistirici karakterler prompt
# injection ve veri sizdirmada kullanilir. Metin analizinden once
# temizlenir ki "I​B​A​N" gibi yazimlar dedektoru atlatmasin.
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿]")


def _luhn_valid(match: re.Match[str], _text: str, _field: str) -> bool:
    """Kart numarasi dogrulamasi.

    13-19 haneli her sayi kart degildir; SAP'te bu uzunlukta belge/parti
    numaralari vardir. Luhn kontrolu yanlis pozitifleri neredeyse tamamen
    keser ve gercek kart numarasini kacirmaz.
    """
    digits = [int(c) for c in match.group(0) if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


_TAX_CONTEXT = re.compile(r"(?i)(vergi|vkn|tckn|tax\s*(no|id|number)|vat|kimlik\s*no)")


def _has_tax_context(match: re.Match[str], text: str, field_name: str) -> bool:
    """10/11 haneli sayiyi yalniz vergi/kimlik baglaminda D3 sayar.

    SAP belge numaralari (EBELN, BELNR, MBLNR) tam 10 hanedir. Baglamsiz her
    10 haneli sayiyi vergi kimligi sayan bir kural, siparis numarasini
    tokenlastirip sonucu kullanilamaz hale getirir. Baglam ya alan adindan ya
    da metnin cevresinden gelir.
    """
    if _TAX_CONTEXT.search(field_name):
        return True
    start, end = match.span()
    window = text[max(0, start - 32) : end + 16]
    return bool(_TAX_CONTEXT.search(window))


_PHONE_HINTS = ("phone", "tel", "mobile", "fax", "gsm")


def _looks_like_phone(match: re.Match[str], _text: str, field_name: str) -> bool:
    """Telefon numarasi mi, yoksa tireli bir SAP kodu mu?

    `R-2026-014-1` gibi WBS elemanlari ve proje kodlari telefon desenine
    uyar. Ayirt etmek icin uc olcut:
      - alan adi telefon ipucu tasiyorsa kabul,
      - `+` ulke on eki varsa kabul,
      - aksi halde en az 10 hane **ve** her ayrac grubunda en az iki hane
        aranir. `2026-014-1` son grubu tek haneli oldugu icin elenir.
    """
    raw = match.group(0).strip()
    if any(hint in field_name.lower() for hint in _PHONE_HINTS):
        return True
    if raw.startswith("+"):
        return True
    digits = [c for c in raw if c.isdigit()]
    if len(digits) < 10:
        return False
    groups = [g for g in re.split(r"[\s()-]+", raw) if g]
    if len(groups) < 3:
        # Ayracsiz uzun sayi: SAP'te belge numarasi olma ihtimali daha yuksek.
        return False
    return all(len(g) >= 2 for g in groups)


# (kural adi, desen, sinif, dogrulayici). Dogrulayici `None` ise desen tek
# basina yeterlidir (IBAN ve e-posta yeterince ozgun).
_TEXT_RULES: tuple[
    tuple[str, re.Pattern[str], DataClass, Any], ...
] = (
    ("secret_header", _BEARER, DataClass.D3, None),
    ("iban", _IBAN, DataClass.D3, None),
    ("card_number", _CARD, DataClass.D3, _luhn_valid),
    ("tax_number", _TR_TAX, DataClass.D3, _has_tax_context),
    ("email", _EMAIL, DataClass.D2, None),
    ("phone", _PHONE, DataClass.D2, _looks_like_phone),
)

# Anahtar adinda gorulunce degerin tamamen gizlendigi ipuclari.
#
# Eslesme **parca bazlidir**, alt-dize degil. Alt-dize eslesmesi
# `likely_missing_authorizations` gibi mesru bir alani "authorization" sanip
# maskeler ve teshis ciktisini kullanilamaz hale getirir.
_SECRET_HINTS = frozenset(
    {
        "password", "passwd", "secret", "token", "authorization", "apikey",
        "credential", "cookie", "privatekey", "clientsecret", "accesstoken",
        "refreshtoken",
    }
)


def _is_secret_field(field_name: str) -> bool:
    """Alan adi bir sir tasiyicisi mi?

    Ad ya tek parca olarak ipucuna esittir (`Authorization`), ya da
    parcalarindan biri ipucudur (`sap_password` -> {sap, password}).
    """
    lowered = field_name.lower()
    tokens = {t for t in re.split(r"[^a-z0-9]+", lowered) if t}
    if tokens & _SECRET_HINTS:
        return True
    return re.sub(r"[^a-z0-9]+", "", lowered) in _SECRET_HINTS


@dataclass(frozen=True)
class DLPFinding:
    """Tek bir alan/desen uzerinde alinan karar."""

    path: str
    field_name: str
    data_class: DataClass
    action: PrivacyAction
    rule: str

    def to_dict(self) -> dict[str, Any]:
        # Deger **hicbir kosulda** bulguya yazilmaz; bulgular loga gider.
        return {
            "path": self.path,
            "field": self.field_name,
            "class": self.data_class.value,
            "action": self.action.value,
            "rule": self.rule,
        }


@dataclass
class DLPResult:
    """DLP uygulamasinin sonucu."""

    payload: Any
    findings: list[DLPFinding] = field(default_factory=list)
    denied_reason: str = ""
    # Payload'da **fiilen gorulen** en yuksek veri sinifi. `findings` yalnizca
    # islem yapilan alanlari icerir; izin verilen bir D2 alan bulgu uretmez ama
    # cache karari icin bilinmesi gerekir. Bu alan tum taranan alanlari kapsar.
    observed_max_class: DataClass = DataClass.D0

    def observe(self, data_class: DataClass) -> None:
        if data_class.level > self.observed_max_class.level:
            self.observed_max_class = data_class

    @property
    def denied(self) -> bool:
        return bool(self.denied_reason)

    @property
    def modified(self) -> bool:
        return any(f.action.modifies_value for f in self.findings)

    @property
    def max_class(self) -> DataClass:
        best = DataClass.D0
        for finding in self.findings:
            if finding.data_class.level > best.level:
                best = finding.data_class
        return best

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for finding in self.findings:
            out[finding.action.value] = out.get(finding.action.value, 0) + 1
        return out

    def summary(self) -> dict[str, Any]:
        """Hassas deger icermeyen gizlilik telemetrisi ozeti."""
        return {
            "findings": len(self.findings),
            "max_class": self.max_class.value,
            "observed_class": self.observed_max_class.value,
            "actions": self.counts(),
            "denied": self.denied,
        }


@dataclass
class DLPEngine:
    """Merkezi DLP motoru.

    `mode`:
        enforce  Kararlar uygulanir (uretim varsayilani).
        report   Bulgular uretilir, payload degistirilmez. Yeni bir tool'un
                 gizlilik etkisini olcmek icin; uretimde kullanilmaz.
        off      Yalnizca gelistirme; hicbir sey yapilmaz.
    """

    field_policy: FieldAccessPolicy = field(default_factory=FieldAccessPolicy)
    pseudonymizer: Pseudonymizer = field(default_factory=get_pseudonymizer)
    mode: str = "enforce"

    # --- Ana giris noktasi --------------------------------------------------
    def apply(
        self,
        payload: Any,
        *,
        actor: ActorContext,
        sink: Sink,
        policy: DataPolicy | None = None,
        detail: str = "standard",
        purpose: str = "",
    ) -> DLPResult:
        """Payload'i hedefe gore temizler ve bulgulari dondurur."""
        result = DLPResult(payload=payload)
        if self.mode == "off":
            return result
        cleaned = self._walk(
            payload,
            path="",
            parent_field="",
            depth=0,
            actor=actor,
            sink=sink,
            policy=policy,
            detail=detail,
            purpose=purpose,
            result=result,
        )
        if self.mode == "enforce" and not result.denied:
            result.payload = cleaned
        return result

    def apply_text(
        self,
        text: str,
        *,
        actor: ActorContext,
        sink: Sink,
        purpose: str = "",
    ) -> DLPResult:
        """Serbest metin icin kisayol (model cevabi, handoff ozeti, log satiri)."""
        result = DLPResult(payload=text)
        if self.mode == "off" or not text:
            return result
        cleaned = self._scan_text(
            text,
            path="text",
            field_name="text",
            actor=actor,
            sink=sink,
            policy=None,
            detail="standard",
            purpose=purpose,
            result=result,
        )
        if self.mode == "enforce":
            result.payload = cleaned
        return result

    # --- Ic gezinme ---------------------------------------------------------
    def _walk(
        self,
        node: Any,
        *,
        path: str,
        parent_field: str,
        depth: int,
        actor: ActorContext,
        sink: Sink,
        policy: DataPolicy | None,
        detail: str,
        purpose: str,
        result: DLPResult,
    ) -> Any:
        if depth > _MAX_DEPTH:
            return "[DEPTH_LIMIT]"
        if isinstance(node, Mapping):
            out: dict[str, Any] = {}
            for key, value in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                cleaned = self._walk(
                    value,
                    path=child_path,
                    parent_field=str(key),
                    depth=depth + 1,
                    actor=actor,
                    sink=sink,
                    policy=policy,
                    detail=detail,
                    purpose=purpose,
                    result=result,
                )
                if cleaned is _DROPPED:
                    continue
                out[key] = cleaned
            return out
        if isinstance(node, list | tuple):
            items: list[Any] = []
            for index, value in enumerate(node):
                cleaned = self._walk(
                    value,
                    path=f"{path}[{index}]",
                    parent_field=parent_field,
                    depth=depth + 1,
                    actor=actor,
                    sink=sink,
                    policy=policy,
                    detail=detail,
                    purpose=purpose,
                    result=result,
                )
                if cleaned is _DROPPED:
                    continue
                items.append(cleaned)
            return items
        return self._handle_scalar(
            node,
            path=path,
            field_name=parent_field,
            actor=actor,
            sink=sink,
            policy=policy,
            detail=detail,
            purpose=purpose,
            result=result,
        )

    def _handle_scalar(
        self,
        value: Any,
        *,
        path: str,
        field_name: str,
        actor: ActorContext,
        sink: Sink,
        policy: DataPolicy | None,
        detail: str,
        purpose: str,
        result: DLPResult,
    ) -> Any:
        if value is None or value == "":
            return value

        # 1. Secret ipucu tasiyan anahtarlar deger bakilmadan gizlenir.
        if _is_secret_field(field_name):
            result.findings.append(
                DLPFinding(path, field_name, DataClass.D3, PrivacyAction.MASK, "secret_key")
            )
            return MASK

        # 2. Alan adina duyarli siniflandirma.
        data_class = (
            policy.classify(field_name, strict=self.field_policy.strict_unknown)
            if policy is not None
            else classify_field(field_name)
        )
        result.observe(data_class)
        action = self.field_policy.decide(
            actor=actor,
            field_name=field_name,
            data_class=data_class,
            sink=sink,
            policy=policy,
            detail=detail,
            purpose=purpose,
        )
        if action is not PrivacyAction.ALLOW:
            result.findings.append(
                DLPFinding(path, field_name, data_class, action, "field_class")
            )
            applied = self._apply_action(
                action, value, actor=actor, field_name=field_name, result=result
            )
            if applied is not value:
                return applied

        # 3. Deger metinse serbest metin dedektorlerini de calistir.
        if isinstance(value, str):
            return self._scan_text(
                value,
                path=path,
                field_name=field_name,
                actor=actor,
                sink=sink,
                policy=policy,
                detail=detail,
                purpose=purpose,
                result=result,
            )
        return value

    def _apply_action(
        self,
        action: PrivacyAction,
        value: Any,
        *,
        actor: ActorContext,
        field_name: str,
        result: DLPResult,
    ) -> Any:
        if action is PrivacyAction.DENY:
            result.denied_reason = (
                f"'{field_name}' alani icin veri erisim politikasi reddetti "
                "(yetersiz kapsam veya amac kodu)."
            )
            return _DROPPED
        if action is PrivacyAction.DROP:
            return _DROPPED
        if action is PrivacyAction.TOKENIZE:
            return self.pseudonymizer.token(value, tenant=actor.tenant, namespace=field_name)
        if action is PrivacyAction.MASK:
            return MASK
        return value

    # --- Serbest metin ------------------------------------------------------
    def _scan_text(
        self,
        text: str,
        *,
        path: str,
        field_name: str,
        actor: ActorContext,
        sink: Sink,
        policy: DataPolicy | None,
        detail: str,
        purpose: str,
        result: DLPResult,
    ) -> str:
        if not text:
            return text
        # Unicode obfuscation temizligi: gorunmez karakterler
        # dedektorleri atlatmak icin kullanilir.
        cleaned = _INVISIBLE.sub("", text)
        if cleaned != text:
            result.findings.append(
                DLPFinding(path, field_name, DataClass.D1, PrivacyAction.MASK, "invisible_chars")
            )

        for rule_name, pattern, data_class, validator in _TEXT_RULES:
            matches = [
                m
                for m in pattern.finditer(cleaned)
                if validator is None or validator(m, cleaned, field_name)
            ]
            if not matches:
                continue
            action = self.field_policy.decide(
                actor=actor,
                field_name=field_name,
                data_class=data_class,
                sink=sink,
                policy=policy,
                detail=detail,
                purpose=purpose,
                # Serbest metinde bulunan e-posta/telefon, alan adi ne olursa
                # olsun kisisel veridir.
                personal=rule_name in {"email", "phone"},
            )
            if action is PrivacyAction.ALLOW:
                continue
            result.findings.append(DLPFinding(path, field_name, data_class, action, rule_name))
            if action is PrivacyAction.DENY:
                result.denied_reason = (
                    f"Serbest metinde '{rule_name}' tespit edildi ve politika reddetti."
                )
                return MASK
            cleaned = self._redact_matches(
                cleaned, matches, rule_name, action, actor=actor
            )
        return cleaned

    def _redact_matches(
        self,
        text: str,
        matches: list[re.Match[str]],
        rule_name: str,
        action: PrivacyAction,
        *,
        actor: ActorContext,
    ) -> str:
        """Dogrulanmis eslesmeleri sondan basa degistirir.

        Sondan basa gidilir ki bir onceki degistirme sonraki eslesmenin
        indeksini kaydirmasin.
        """
        out = text
        for match in reversed(matches):
            start, end = match.span()
            out = out[:start] + self._replacement(match, rule_name, action, actor=actor) + out[end:]
        return out

    def _replacement(
        self,
        match: re.Match[str],
        rule_name: str,
        action: PrivacyAction,
        *,
        actor: ActorContext,
    ) -> str:
        if action is PrivacyAction.DROP:
            return ""
        if action is PrivacyAction.TOKENIZE:
            return self.pseudonymizer.token(
                match.group(0), tenant=actor.tenant, namespace=rule_name
            )
        # MASK: tanimlayici olmayan on ek korunur ki metin okunabilir kalsin.
        if rule_name == "email":
            return f"{match.group(1)}{MASK}{match.group(2)}"
        if rule_name == "iban":
            return f"{match.group(1)}{MASK}"
        if rule_name == "secret_header":
            return f"{match.group(1)} {MASK}"
        return MASK


class _Dropped:
    """Alanin tamamen dusuruldugunu belirten sentinel."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - teshis kolayligi
        return "<dropped>"


_DROPPED = _Dropped()


def build_dlp_engine(settings: Any) -> DLPEngine:
    """Ayarlardan DLP motoru kurar.

    Uretim profilinde `strict_unknown` her zaman acilir: siniflandirilmamis
    alan D3 kabul edilir.
    """
    privacy = getattr(settings, "privacy", None)
    mode = getattr(privacy, "dlp_mode", "enforce")
    strict = bool(getattr(settings, "is_production", False)) or bool(
        getattr(privacy, "strict_unknown_fields", False)
    )
    return DLPEngine(
        field_policy=FieldAccessPolicy(strict_unknown=strict),
        pseudonymizer=get_pseudonymizer(settings),
        mode=mode,
    )
