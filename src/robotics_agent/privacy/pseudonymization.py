"""Tenant'a ozgu HMAC takma kimliklestirme.

Neden rastgele maske degil: analiz sirasinda "ayni tedarikci mi?" sorusunun
cevaplanabilmesi gerekir. `***` bu bilgiyi yok eder, ham deger ise gizliligi
yok eder. HMAC tabanli takma kimlik ikisinin arasindadir: **deterministik**
(ayni girdi ayni tokeni uretir) ama **geri cozulemez** (anahtar olmadan).

Iki guvenlik ozelligi:
  1. Anahtar tenant'a gore turetilir; iki tenant'taki ayni IBAN farkli token
     uretir, boylece token'lar tenant'lar arasi korelasyon kurmaz.
  2. Geri cozum tablosu **model katmaninda bulunmaz**. Bu modul
     yalnizca ileri yonlu uretim yapar; `resolve` gibi bir fonksiyon bilerek
     yoktur.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from dataclasses import dataclass

__all__ = ["Pseudonymizer", "get_pseudonymizer", "reset_pseudonymizer_cache"]

_PREFIX = "px"


@dataclass(frozen=True)
class Pseudonymizer:
    """Deterministik, tenant-izole takma kimlik uretici.

    `key_id` audit'e yazilir: bir token'in hangi anahtar surumuyle uretildigi
    bilinmeden key rotation sonrasi eski kayitlar yorumlanamaz.
    """

    secret: bytes
    key_id: str = "dev-ephemeral"
    token_length: int = 12

    def token(self, value: object, *, tenant: str, namespace: str = "") -> str:
        """Deger icin tenant'a ozgu takma kimlik uretir.

        Bos/None deger tokenlastirilmaz: `""` zaten bilgi tasimaz ve
        tokenlastirmak bos alani "dolu" gibi gosterir.
        """
        if value is None or value == "":
            return ""
        material = f"{tenant}\x1f{namespace}\x1f{value}".encode()
        digest = hmac.new(self._tenant_key(tenant), material, hashlib.sha256).hexdigest()
        return f"{_PREFIX}_{digest[: self.token_length]}"

    def subject_token(self, subject: str, *, tenant: str) -> str:
        """Telemetry/audit icin kullanici kimligi takma adi."""
        return self.token(subject, tenant=tenant, namespace="subject")

    def _tenant_key(self, tenant: str) -> bytes:
        """Ana anahtardan tenant anahtari turetir (HKDF-benzeri tek adim)."""
        return hmac.new(self.secret, f"tenant:{tenant}".encode(), hashlib.sha256).digest()

    @property
    def is_ephemeral(self) -> bool:
        """Anahtar surec omurlu mu? Uretimde bu True olmamali."""
        return self.key_id == "dev-ephemeral"


_lock = threading.Lock()
_cached: Pseudonymizer | None = None


def get_pseudonymizer(settings: object | None = None) -> Pseudonymizer:
    """Yapilandirilmis pseudonymizer'i dondurur.

    Anahtar kaynagi sirasi:
      1. `AGENT_PSEUDONYMIZATION_SECRET` ortam degiskeni (pilot).
      2. KMS/secret manager referansi (`AGENT_PSEUDONYMIZATION_KEY_ID`) —
         gercek cozumleme deployment'a birakilir; burada yalnizca kimlik
         audit'e tasinir.
      3. Hicbiri yoksa **surec omurlu rastgele** anahtar. Bu bilincli olarak
         kalici degildir: yanlislikla uretimde kullanildiginda token'lar her
         restart'ta degisir ve durum fark edilir. Uretim profili zaten
         `production_blockers` ile bu durumu engeller.
    """
    global _cached
    with _lock:
        if _cached is not None:
            return _cached
        secret = os.getenv("AGENT_PSEUDONYMIZATION_SECRET", "").strip()
        key_id = os.getenv("AGENT_PSEUDONYMIZATION_KEY_ID", "").strip()
        if secret:
            _cached = Pseudonymizer(
                secret=secret.encode(), key_id=key_id or "env-secret"
            )
        else:
            _cached = Pseudonymizer(secret=secrets.token_bytes(32), key_id="dev-ephemeral")
        return _cached


def reset_pseudonymizer_cache() -> None:
    """Testler icin: bir sonraki cagri anahtari yeniden okur."""
    global _cached
    with _lock:
        _cached = None
