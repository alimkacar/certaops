"""Diskteki hassas kayitlar icin simetrik sifreleme.

Neden bu dosya var
------------------
`AGENT_EVIDENCE_ENCRYPTION`, `AGENT_SESSION_ENCRYPTION` ve
`AGENT_AUDIT_ENCRYPTION` ayarlari
tanimliydi ama **hicbir yerde okunmuyordu**. `.env.example`den kopyalayip
`true` yapan bir operator, kanit ve konusma kayitlarinin diskte sifreli
durdugunu sanip duz metin sakliyordu. Islevsiz bir guvenlik ayari,
olmayan bir ayardan daha kotudur: yanlis guven uretir.

Tasarim kurali: **sessizce duz metne dusmek yok.**
Ayar acik ama anahtar yoksa ya da kutuphane kurulu degilse surec
BASLAMAZ (`config.validate()` hata dondurur). Boylece "acik ama calismiyor"
durumu var olamaz.

Sifre cozme geriye donuk uyumludur: sifreleme sonradan acildiginda
depodaki eski duz metin kayitlar okunmaya devam eder. Ters yon (sifreli
kaydi anahtarsiz okumak) sessizce bosa dusmez, hata verir.
"""

from __future__ import annotations

import base64
import os
import secrets

#: Sifreli govdenin onune yazilan etiket. Duz metin kayitlardan ayirt
#: etmeyi ve ileride algoritma degistirmeyi mumkun kilar.
ENVELOPE_PREFIX = "encv1:"

#: AES-256-GCM: 32 bayt anahtar, 12 bayt nonce (NIST SP 800-38D onerisi).
KEY_BYTES = 32
NONCE_BYTES = 12

#: Anahtar materyali. `AGENT_KMS_KEY_ID` bir KIMLIKTIR, anahtar degildir;
#: ondan anahtar turetmek sahte guvenlik olurdu.
KEY_ENV = "AGENT_AT_REST_KEY"


class AtRestConfigError(RuntimeError):
    """Sifreleme istendi ama kurulamadi. Fail-closed: surec baslamamali."""


def _load_aesgcm():
    """`cryptography` opsiyoneldir; yoklugu sessiz duz metne DONUSMEZ."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - kurulum ortamina bagli
        raise AtRestConfigError(
            "Sifreleme acik ama 'cryptography' paketi kurulu degil. "
            "`pip install cryptography` ya da ayari kapatin."
        ) from exc
    return AESGCM


def load_key(env_var: str = KEY_ENV) -> bytes:
    """Ortamdan 32 baytlik anahtari okur (base64 ya da hex).

    Anahtar uretmek icin: `python -c "import os,base64;
    print(base64.b64encode(os.urandom(32)).decode())"`.
    Uretimde bu deger bir secret manager'dan gelmelidir.
    """
    raw = (os.environ.get(env_var) or "").strip()
    if not raw:
        raise AtRestConfigError(
            f"Sifreleme acik ama {env_var} tanimli degil. 32 baytlik bir anahtari "
            "base64 olarak verin (KMS/secret manager onerilir)."
        )
    for decode in (base64.b64decode, bytes.fromhex):
        try:
            key = decode(raw)
        except Exception:  # noqa: BLE001 - iki kodlama da denenir
            continue
        if len(key) == KEY_BYTES:
            return key
    raise AtRestConfigError(
        f"{env_var} cozulemedi ya da {KEY_BYTES} bayt degil. base64 ya da hex bekleniyor."
    )


class RecordCipher:
    """Tek bir amac icin (kanit / oturum / audit) AES-GCM zarfi.

    `purpose` ek kimlik dogrulama verisi (AAD) olarak baglanir: kanit
    deposundan alinan bir sifreli govde oturum deposuna tasinip cozulemez.
    """

    def __init__(self, key: bytes, *, purpose: str) -> None:
        aesgcm = _load_aesgcm()
        if len(key) != KEY_BYTES:
            raise AtRestConfigError(f"Anahtar {KEY_BYTES} bayt olmali.")
        self._aead = aesgcm(key)
        self._aad = purpose.encode()

    def encrypt(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(NONCE_BYTES)
        blob = self._aead.encrypt(nonce, plaintext.encode(), self._aad)
        return ENVELOPE_PREFIX + base64.b64encode(nonce + blob).decode()

    def decrypt(self, stored: str) -> str:
        """Sifreliyse cozer; etiketsiz (eski, duz metin) kaydi aynen dondurur."""
        if not stored.startswith(ENVELOPE_PREFIX):
            return stored
        raw = base64.b64decode(stored[len(ENVELOPE_PREFIX) :])
        nonce, blob = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
        return self._aead.decrypt(nonce, blob, self._aad).decode()


def maybe_cipher(enabled: bool, *, purpose: str) -> RecordCipher | None:
    """Ayar acikken sifreleyici kurar; kapaliyken `None` doner.

    Ayar acik ama kurulum eksikse `AtRestConfigError` YUKSELIR - cagiran
    taraf bunu yutmamalidir. `config.validate()` ayni yolu onceden calistirip
    hatayi baslangicta gorunur kilar.
    """
    if not enabled:
        return None
    return RecordCipher(load_key(), purpose=purpose)


def decrypt_if_needed(cipher: RecordCipher | None, stored: str) -> str:
    """Depodan okunan degeri cozer.

    Sifreleme kapaliyken sifreli bir kayitla karsilasmak SESSIZ gecilmez:
    anahtar kaybi ya da yanlis yapilandirma demektir ve "veri yok" gibi
    davranmak durumu gizlerdi.
    """
    if cipher is not None:
        return cipher.decrypt(stored)
    if stored.startswith(ENVELOPE_PREFIX):
        raise AtRestConfigError(
            "Depoda sifreli kayit var ama sifreleme kapali. Ayari acin ve "
            f"{KEY_ENV} degerini kayitlarin yazildigi anahtarla ayni yapin."
        )
    return stored
