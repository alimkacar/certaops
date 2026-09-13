#!/usr/bin/env python3
"""SAP API Business Hub sandbox 401 teshisi.

Uygulamanin HTTP katmanini KULLANMAZ. Yalniz stdlib ile ayni anahtari ayni
header'la birkac farkli sandbox servisine gonderir ve HAM yaniti gosterir.
Amaci tek bir soruyu ayirmak:

    Anahtar mi olu, yoksa yalniz bir servis mi kapali?

Okuma anahtari
--------------
  Apigee JSON  ("Invalid ApiKey" / "Failed to resolve API Key")
      -> anahtar gecersiz veya header hic gitmiyor. api.sap.com > Settings >
         Show API Key ile yenisini alin.

  ABAP HTML    ("Logon failed", "401 Not authorized", "SAP SE")
      -> SAP oturum acmayi reddetti; sorgu sonucu alinamadi.
         Bu yanit tek basina gecit kimliginin neden reddedildigini aciklamaz.

  HTTP 200     -> o servis calisiyor.

Kullanim:
    python scripts/teshis_401.py            # .env'den okur
    SAP_API_KEY=... python scripts/teshis_401.py
"""

from __future__ import annotations

import gzip
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import urlsplit

KOK = Path(__file__).resolve().parents[1]

# (etiket, servis yolu, entity set)  -- hepsi salt okunur $top=1
HEDEFLER = [
    ("tedarikci faturasi (HATA VEREN)",
     "API_SUPPLIERINVOICE_PROCESS_SRV", "A_SuplrInvcItemPurOrdRef"),
    ("tedarikci faturasi basligi",
     "API_SUPPLIERINVOICE_PROCESS_SRV", "A_SupplierInvoice"),
    ("satinalma siparisi",
     "API_PURCHASEORDER_PROCESS_SRV", "A_PurchaseOrder"),
    ("satinalma talebi",
     "API_PURCHASEREQ_PROCESS_SRV", "A_PurchaseRequisitionHeader"),
    ("is ortagi",
     "API_BUSINESS_PARTNER", "A_BusinessPartner"),
    ("malzeme belgesi",
     "API_MATERIAL_DOCUMENT_SRV", "A_MaterialDocumentHeader"),
]


def env_yukle() -> dict[str, str]:
    """`.env` dosyasini kabaca okur. Kabuktaki degerler oncelikli."""
    degerler: dict[str, str] = {}
    dosya = KOK / ".env"
    if dosya.exists():
        for satir in dosya.read_text(encoding="utf-8").splitlines():
            satir = satir.strip()
            if not satir or satir.startswith("#") or "=" not in satir:
                continue
            anahtar, _, deger = satir.partition("=")
            degerler[anahtar.strip()] = deger.strip().strip('"').strip("'")
    degerler.update({k: v for k, v in os.environ.items() if k.startswith("SAP_")})
    return degerler


def coz(govde: bytes, encoding: str) -> bytes:
    """Content-Encoding neyse acar. Acilamazsa ham gövdeyi geri verir.

    urllib gzip'i kendisi acmaz; onceki surumde bu yuzden ekrana ikili copluk
    basiliyordu ve yanit HTML mi JSON mu ayirt edilemiyordu.
    """
    kod = (encoding or "").lower().strip()
    try:
        if "gzip" in kod:
            return gzip.decompress(govde)
        if "deflate" in kod:
            try:
                return zlib.decompress(govde)
            except zlib.error:
                return zlib.decompress(govde, -zlib.MAX_WBITS)
        if "br" in kod:
            import brotli  # type: ignore
            return brotli.decompress(govde)
    except Exception:
        pass
    # Content-Encoding yalan soyluyorsa: gzip sihirli baytina bak.
    if govde[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(govde)
        except Exception:
            pass
    return govde


def ozet(govde: bytes, ctype: str) -> str:
    metin = govde.decode("utf-8", "replace")
    if "json" in ctype.lower() or metin.lstrip()[:1] in "{[":
        try:
            veri = json.loads(metin)
        except Exception:
            return metin[:200].replace("\n", " ")
        hata = (veri.get("fault") or {}).get("faultstring")
        if hata:
            return f"APIGEE: {hata}"
        err = (veri.get("error") or {})
        mesaj = err.get("message")
        if isinstance(mesaj, dict):
            mesaj = mesaj.get("value")
        if mesaj:
            return f"ODATA: {mesaj}"
        return metin[:200].replace("\n", " ")
    duz = re.sub(r"<[^>]+>", " ", metin)
    duz = re.sub(r"\s+", " ", duz).strip()
    return f"HTML: {duz[:200]}"


def tanı(kod: int, govde: bytes, ctype: str, challenge: str = "") -> str:
    metin = govde.decode("utf-8", "replace").lower()
    if kod == 200:
        return "OK"
    if kod == 429 or "quota" in metin or "rate limit" in metin:
        # Kota asimi gecidin kendi cevabidir: 429 + JSON. ABAP logon
        # sayfasiyla karistirilmamali; ikisi farkli katmandan gelir.
        return "KOTA ASILDI"
    if "apikey" in metin and "json" in ctype.lower():
        return "ANAHTAR GECERSIZ"
    if challenge.strip().lower().startswith("basic"):
        return "SAP OTURUM REDDI" if "sap netweaver" in challenge.lower() else "KIMLIK REDDI"
    if "logon failed" in metin or "not authorized" in metin or "oturum" in metin:
        return "SAP OTURUM REDDI"
    return "?"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """API anahtarini yonlendirmeyle baska hedefe tasima."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    cfg = env_yukle()
    taban = (cfg.get("SAP_BASE_URL") or "https://sandbox.api.sap.com/s4hanacloud").rstrip("/")
    target = urlsplit(taban)
    if (target.scheme, target.netloc, target.path.rstrip("/")) != (
        "https", "sandbox.api.sap.com", "/s4hanacloud"
    ):
        print("Bu betik yalniz SAP S/4HANA sandbox icindir. Diger sistemlerde check_real_sap.py kullanin.", file=sys.stderr)
        return 2
    anahtar = cfg.get("SAP_API_KEY", "")
    header = cfg.get("SAP_API_KEY_HEADER") or "APIKey"

    if not anahtar:
        print("SAP_API_KEY bos. .env'i kontrol edin.", file=sys.stderr)
        return 2

    print(f"taban    : {taban}")
    print(f"header   : {header}")
    print("anahtar  : ayarli (gizlendi)")
    print("-" * 78)

    baglam = ssl.create_default_context()
    if (cfg.get("SAP_VERIFY_SSL") or "true").lower() in {"0", "false", "no"}:
        baglam.check_hostname = False
        baglam.verify_mode = ssl.CERT_NONE

    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=baglam), NoRedirect())
    sonuclar = []
    for etiket, servis, entity in HEDEFLER:
        url = f"{taban}/sap/opu/odata/sap/{servis}/{entity}?$top=1&$format=json"
        istek = urllib.request.Request(
            url,
            headers={
                header: anahtar,
                "Accept": "application/json",
                # Sunucu yine de sikistirabilir; asagida her halukarda aciyoruz.
                "Accept-Encoding": "identity",
            },
        )
        try:
            with opener.open(istek, timeout=20) as yanit:
                kod, ham, ctype = yanit.status, yanit.read(8000), yanit.headers.get("content-type", "")
                challenge = yanit.headers.get("www-authenticate", "") or ""
                govde = coz(ham, yanit.headers.get("content-encoding", ""))
        except urllib.error.HTTPError as exc:
            kod, ham, ctype = exc.code, exc.read(8000), exc.headers.get("content-type", "")
            challenge = exc.headers.get("www-authenticate", "") or ""
            govde = coz(ham, exc.headers.get("content-encoding", ""))
        except Exception as exc:  # ag hatasi
            print(f"[ AG HATASI ] {etiket:32s} {servis}\n              {exc}")
            sonuclar.append((etiket, 0, "AG HATASI"))
            continue

        karar = tanı(kod, govde, ctype, challenge)
        print(f"[ {kod} {karar:30s} ] {etiket}")
        print(f"              {servis}/{entity}")
        print(f"              content-type: {ctype or '(yok)'}")
        if challenge:
            print(f"              www-authenticate: {challenge[:110]}")
        print(f"              {ozet(govde, ctype)}")
        sonuclar.append((etiket, kod, karar))
        print()

    # --- Kontrol grubu ------------------------------------------------------
    # Ayni sandbox icinde eksik ve gecersiz anahtar yanitlarini karsilastir.
    # Bu kontrol tek basina SAP teknik kimligindeki arizanin nedenini kanitlamaz.
    print("-" * 78)
    print("KONTROL GRUBU (ayni URL, kasitli bozuk kimlik)")
    print()
    kontrol_url = f"{taban}/sap/opu/odata/sap/API_BUSINESS_PARTNER/A_BusinessPartner?$top=1&$format=json"
    denemeler = [
        ("header HIC yok", None),
        ("uydurma anahtar", "x" * 32),
    ]
    for etiket, sahte in denemeler:
        basliklar = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if sahte is not None:
            basliklar[header] = sahte
        istek = urllib.request.Request(kontrol_url, headers=basliklar)
        try:
            with opener.open(istek, timeout=20) as yanit:
                kod, ham, ctype = yanit.status, yanit.read(8000), yanit.headers.get("content-type", "")
                govde = coz(ham, yanit.headers.get("content-encoding", ""))
        except urllib.error.HTTPError as exc:
            kod, ham, ctype = exc.code, exc.read(8000), exc.headers.get("content-type", "")
            govde = coz(ham, exc.headers.get("content-encoding", ""))
        except Exception as exc:
            print(f"[ AG HATASI ] {etiket}: {exc}")
            continue
        print(f"[ {kod} ] {etiket}")
        print(f"          content-type: {ctype or '(yok)'}")
        print(f"          {ozet(govde, ctype)}")
        print()

    print("-" * 78)
    ok = [s for s in sonuclar if s[1] == 200]
    if len(ok) == len(sonuclar):
        print("SONUC: Kontrol edilen tum servisler erisilebilir.")
        return 0
    if any(s[2] == "ANAHTAR GECERSIZ" for s in sonuclar):
        print("SONUC: API anahtari reddedildi. Hub API Key ayarini kontrol edin.")
    elif any(s[2] == "KOTA ASILDI" for s in sonuclar):
        print("SONUC: Kota/hiz siniri bildirildi. Limitin yenilenmesini bekleyin.")
    elif any(s[2] == "SAP OTURUM REDDI" for s in sonuclar):
        print("SONUC: SAP oturum acmayi reddetti; is verisi dogrulanamadi.")
        print("       Ayni servisi SAP Business Accelerator Hub Try Out ekraninda deneyin.")
        print("       Orada da 401 varsa SAP destek kaydi acin veya erisilebilir bir sistem kullanin.")
        print("       Bu yanit, teknik kimligin eksik/gecersiz/kilitli oldugunu ayirt etmez.")
    else:
        print("SONUC: Bazi servisler erisilemedi; yukaridaki HTTP/ag hatalarini kontrol edin.")
    print(f"       Erisilebilir servis: {len(ok)}/{len(sonuclar)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
