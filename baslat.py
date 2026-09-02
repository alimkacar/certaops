#!/usr/bin/env python3
"""CertaOps yerel operatör konsolunu başlatır.

`baslat.command` bu dosyaya devreder. Mantığın burada olmasının sebebi
macOS'un /bin/bash'inin 3.2 sürümünde olması: modern bash'te sorunsuz
çalışan yapılar orada ayrıştırılamıyor ve betik çalışmadan patlıyor.
Python sürüm farkı bu ölçekte sorun çıkarmaz.

Yaptıkları:
  1. Sanal ortamı bulur veya kurar
  2. Eksik bağımlılıkları kurar
  3. Üç kimlik üretir (görev ayrımı) — bir kez, sonra sabit kalır
  4. Ayarları ortam değişkeni olarak hazırlar (.env dosyasına DOKUNMAZ)
  5. Boş bir port seçer
  6. Servisi ÖN PLANDA başlatır, tarayıcıyı açar

Kullanım:
    ./baslat.command                         # mod ve rolü menüden seç
    ./baslat.command --sap --rol denetci     # canlı SAP, denetçi rolü
    ./baslat.command --sim --rol satinalmaci # mock SAP, satın almacı rolü
    PORT=8080 ./baslat.command               # port seçerek

`--sap` yalnız OKUMA yolunu gerçek sisteme çevirir. SAP_READ_ONLY ve
SAP_DRY_RUN kilitleri her iki modda da açık kalır.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJE = Path(__file__).resolve().parent
YEREL_ENV = PROJE / ".env.local"
PRINCIPALS = PROJE / "config" / "principals.json"
VENV = PROJE / ".venv"

# --------------------------------------------------------------------------
# Kimlikler
#
# Üç ayrı kimlik üretilir çünkü sistemin bütün mesele ettiği şey görev
# ayrımı: aynı principal'a hem yazma hem onay yetkisi vermek onay zincirini
# anlamsızlaştırır (bkz. config/principals.example.json).
# --------------------------------------------------------------------------
KIMLIKLER = [
    # AUDITOR: sap.read + audit.read → bütün sekmeler görünür. Yazma yetkisi
    # YOK; bir yazma denemesi policy kapısında durur ve defterde kırık ray
    # olarak görünür. Arayüzü gezmek için en iyi kimlik.
    ("DENETCI", "denetci@local", "Denetci", ["AUDITOR"],
     "bütün sekmeler · yazma policy kapısında durur"),
    # PURCHASER: salt-okunur analiz ve talep taslağı hazırlar; SAP'a gönderemez.
    ("SATINALMACI", "satinalmaci@local", "Satinalma Uzmani", ["PURCHASER"],
     "analiz ve talep taslağı hazırlar · SAP yazması kapalı"),
    # APPROVER rolü gelecekteki write paketi regresyonları için korunur.
    ("ONAYLAYICI", "onaylayici@local", "Satinalma Muduru", ["APPROVER"],
     "gelecekteki onay akışı rolü · mevcut sürüm salt-okunur"),
]

GEREKLI_MODULLER = ("fastapi", "uvicorn", "pydantic", "dotenv", "httpx")


# ==========================================================================
# Çıktı
# ==========================================================================
class R:
    """Renkler. Terminal desteklemiyorsa hepsi boş dizge olur."""

    _acik = sys.stdout.isatty() and os.environ.get("TERM") not in (None, "dumb")
    KIRMIZI = "\033[31m" if _acik else ""
    YESIL = "\033[32m" if _acik else ""
    SARI = "\033[33m" if _acik else ""
    MAVI = "\033[36m" if _acik else ""
    KALIN = "\033[1m" if _acik else ""
    SOLUK = "\033[2m" if _acik else ""
    SIFIR = "\033[0m" if _acik else ""


def baslik(metin: str) -> None:
    print(f"\n{R.KALIN}{metin}{R.SIFIR}")


def ok(metin: str) -> None:
    print(f"  {R.YESIL}✓{R.SIFIR} {metin}")


def bilgi(metin: str) -> None:
    print(f"  {R.SOLUK}·{R.SIFIR} {metin}")


def uyari(metin: str) -> None:
    print(f"  {R.SARI}!{R.SIFIR} {metin}")


def hata(metin: str) -> None:
    print(f"  {R.KIRMIZI}✕{R.SIFIR} {metin}")


def dur(kod: int = 1) -> None:
    """Çift tıklamayla açılan pencere hemen kapanmasın: mesaj okunabilsin."""
    print(f"\n{R.KIRMIZI}Başlatılamadı.{R.SIFIR} Yukarıdaki mesaja bakın.")
    print("Kapatmak için bu pencereyi kapatın veya Enter'a basın.")
    with contextlib.suppress(EOFError, KeyboardInterrupt):
        input()
    sys.exit(kod)


# ==========================================================================
# Başlangıç seçimi
# ==========================================================================
CANLI_BAYRAKLARI = {"--sap", "--canli", "--live"}
SIMULASYON_BAYRAKLARI = {"--sim", "--simulasyon", "--mock"}

ROL_ESLEME = {
    "1": "DENETCI",
    "denetci": "DENETCI",
    "denetçi": "DENETCI",
    "auditor": "DENETCI",
    "2": "SATINALMACI",
    "satinalmaci": "SATINALMACI",
    "satınalmacı": "SATINALMACI",
    "satın almacı": "SATINALMACI",
    "purchaser": "SATINALMACI",
    "3": "ONAYLAYICI",
    "onaylayici": "ONAYLAYICI",
    "onaylayıcı": "ONAYLAYICI",
    "approver": "ONAYLAYICI",
}


def _arg_degeri(ad: str) -> str:
    """`--rol deger` ve `--rol=deger` bicimlerini birlikte okur."""
    args = sys.argv[1:]
    for index, arg in enumerate(args):
        if arg == ad and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(ad + "="):
            return arg.split("=", 1)[1]
    return ""


def _acik_mod_secimi() -> bool | None:
    """Arguman/ortam acik bir mod soyluyorsa dondur; yoksa None."""
    bayraklar = {arg.lower() for arg in sys.argv[1:]}
    canli = bool(bayraklar & CANLI_BAYRAKLARI)
    simulasyon = bool(bayraklar & SIMULASYON_BAYRAKLARI)
    if canli and simulasyon:
        raise ValueError("Ayni anda hem --sap hem --sim kullanilamaz.")
    if canli:
        return True
    if simulasyon:
        return False

    ortam = os.environ.get("SAP", "").strip().lower()
    if ortam in {"canli", "live", "hub", "1", "true"}:
        return True
    if ortam in {"sim", "simulasyon", "mock", "0", "false"}:
        return False
    return None


def _acik_rol_secimi() -> str | None:
    ham = _arg_degeri("--rol") or os.environ.get("CERTAOPS_ROLE", "")
    if not ham.strip():
        return None
    rol = ROL_ESLEME.get(ham.strip().lower())
    if rol is None:
        raise ValueError(
            f"Bilinmeyen rol '{ham}'. denetci, satinalmaci veya onaylayici kullanin."
        )
    return rol


def _secim_oku(prompt: str, *, varsayilan: str, gecerli: set[str]) -> str:
    while True:
        try:
            cevap = input(prompt).strip() or varsayilan
        except (EOFError, KeyboardInterrupt):
            print()
            return varsayilan
        if cevap in gecerli:
            return cevap
        uyari("Gecersiz secim; " + ", ".join(sorted(gecerli)) + " degerlerinden birini girin.")


def baslangic_secimleri(*, etkilesimli: bool | None = None) -> tuple[bool, str]:
    """Calisma modu ile giris rolunu argumandan veya terminal menusunden al."""
    if etkilesimli is None:
        etkilesimli = sys.stdin.isatty() and sys.stdout.isatty()

    canli = _acik_mod_secimi()
    if canli is None:
        if etkilesimli:
            baslik("Calisma modu")
            print("  [1] Simulasyon  - mock veri, gercek SAP'a baglanmaz")
            print("  [2] Canli SAP   - .env hedefi, salt-okunur + dry-run")
            canli = _secim_oku(
                "  Seciminiz [1]: ", varsayilan="1", gecerli={"1", "2"}
            ) == "2"
        else:
            canli = False

    rol = _acik_rol_secimi()
    if rol is None:
        if etkilesimli:
            baslik("Giris rolu")
            print("  [1] Denetci       - durum, denetim ve log gorunumu")
            print("  [2] Satinalmaci   - analiz ve talep taslagi")
            print("  [3] Onaylayici    - onay akisi gorunumu")
            cevap = _secim_oku(
                "  Seciminiz [1]: ", varsayilan="1", gecerli={"1", "2", "3"}
            )
            rol = ROL_ESLEME[cevap]
        else:
            rol = "DENETCI"

    return canli, rol


# ==========================================================================
# 1) Python ve sanal ortam
# ==========================================================================
def venv_python() -> Path:
    """Sanal ortamın python'u; yoksa veya bozuksa yeniden kurar."""
    aday = VENV / "bin" / "python"

    # exists() sembolik bağı TAKİP eder: Homebrew Python güncellenince
    # .venv/bin/python kırık bir bağa dönüşür ve exists() False verir.
    # is_symlink() olmadan "bozuk ortam" dalına hiç girilmez.
    if aday.exists() or aday.is_symlink():
        try:
            subprocess.run([str(aday), "-c", "import sys"],
                           check=True, capture_output=True, timeout=30)
            ok("mevcut sanal ortam: .venv")
            return aday
        except (subprocess.SubprocessError, OSError):
            uyari("sanal ortam bozuk, yeniden kuruluyor")

    ok(f"sistem Python: {sys.version.split()[0]}")
    # Dizini SİLEREK başla: `python -m venv` var olan bir dizinde kırık
    # sembolik bağları onarmaz, sessizce bırakır.
    if VENV.exists() or VENV.is_symlink():
        shutil.rmtree(VENV, ignore_errors=True)
    bilgi("sanal ortam kuruluyor (.venv)…")
    try:
        subprocess.run([sys.executable, "-m", "venv", str(VENV)],
                       check=True, timeout=300)
    except (subprocess.SubprocessError, OSError) as exc:
        hata(f"Sanal ortam kurulamadı: {exc}")
        dur()

    if not aday.exists():
        hata("Sanal ortam kuruldu ama python bulunamadı.")
        bilgi("Elle deneyin:  rm -rf .venv && python3 -m venv .venv")
        dur()
    ok("sanal ortam hazır")
    return aday


# ==========================================================================
# 2) Bağımlılıklar
# ==========================================================================
def moduller_var(py: Path) -> bool:
    kod = "import " + ", ".join(GEREKLI_MODULLER)
    try:
        subprocess.run([str(py), "-c", kod], check=True,
                       capture_output=True, timeout=60)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def bagimliliklari_kur(py: Path) -> None:
    if moduller_var(py):
        ok("kurulu")
        return

    bilgi("kuruluyor — ilk çalıştırmada birkaç dakika sürebilir…")
    subprocess.run([str(py), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
                   capture_output=True, timeout=300)
    try:
        sonuc = subprocess.run([str(py), "-m", "pip", "install", "--quiet", "-e", "."],
                               cwd=str(PROJE), capture_output=True, text=True, timeout=1800)
    except (subprocess.SubprocessError, OSError) as exc:
        hata(f"Kurulum çalıştırılamadı: {exc}")
        dur()

    if sonuc.returncode != 0:
        hata("Bağımlılıklar kurulamadı.")
        for satir in (sonuc.stderr or sonuc.stdout or "").strip().splitlines()[-15:]:
            print(f"    {R.SOLUK}{satir}{R.SIFIR}")
        bilgi("İnternet bağlantısını kontrol edip tekrar deneyin.")
        bilgi("Elle kurulum:  .venv/bin/python -m pip install -e .")
        dur()

    if not moduller_var(py):
        hata("Kurulum bitti ama fastapi/uvicorn içe aktarılamıyor.")
        dur()
    ok("kuruldu")


# ==========================================================================
# 3) Kimlikler
# ==========================================================================
def yerel_env_oku() -> dict[str, str]:
    """.env.local dosyasındaki KEY=value satırlarını okur."""
    if not YEREL_ENV.exists():
        return {}
    degerler: dict[str, str] = {}
    for satir in YEREL_ENV.read_text(encoding="utf-8").splitlines():
        satir = satir.strip()
        if not satir or satir.startswith("#") or "=" not in satir:
            continue
        anahtar, _, deger = satir.partition("=")
        degerler[anahtar.strip()] = deger.strip()
    return degerler


def org_kapsami() -> dict[str, list[str]]:
    """Yerel operatorun organizasyon yetki alani.

    Demo kapsamina AKTIF PROFILIN varsayilanlari eklenir. Aksi halde
    `.env` icinde `SAP_PLANT=1100` yazip kapsama yalniz `1010` vermek,
    tesis argumani verilmeyen her cagrinin `ORG_SCOPE` ile reddedilmesine
    yol acar: handler sistem varsayilanini koyar, policy onu yetki alani
    disinda bulur ve SAP'a hic gidilmez.

    Kapsam kontrolu ETKISIZLESTIRILMEZ: yalniz profilin kendi degerleri
    eklenir, baska bir tesis/sirket kodu istendiginde red devam eder.
    """
    demo = {
        "company_codes": ["1000"],
        "plants": ["1010", "1020", "1030"],
        "purchasing_orgs": ["1000"],
    }
    aktif = {
        "company_codes": os.environ.get("SAP_COMPANY_CODE", "")
        or env_dosyasindan_oku("SAP_COMPANY_CODE"),
        "plants": os.environ.get("SAP_PLANT", "") or env_dosyasindan_oku("SAP_PLANT"),
        "purchasing_orgs": os.environ.get("SAP_PURCH_ORG", "")
        or env_dosyasindan_oku("SAP_PURCH_ORG"),
    }
    for anahtar, deger in aktif.items():
        deger = deger.strip()
        if deger and deger not in demo[anahtar]:
            demo[anahtar].append(deger)
    return demo


def org_kapsamini_tazele() -> None:
    """Mevcut `principals.json`in organizasyon kapsamini aktif profile uydurur.

    Token'lara DOKUNMAZ: yalniz `company_codes`/`plants`/`purchasing_orgs`
    alanlari guncellenir. Boylece `.env` profili degistiginde (ornegin mock
    varsayilanindan Hub sandbox degerlerine) kullanicinin kaydettigi
    token'lar gecerli kalir ama cagrilar `ORG_SCOPE` ile reddedilmez.
    """
    if not PRINCIPALS.exists():
        return
    try:
        veri = json.loads(PRINCIPALS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    kayitlar = veri.get("principals")
    if not isinstance(kayitlar, list):
        return
    kapsam = org_kapsami()
    degisti = False
    for kayit in kayitlar:
        if not isinstance(kayit, dict):
            continue
        for alan, deger in kapsam.items():
            if kayit.get(alan) != deger:
                kayit[alan] = list(deger)
                degisti = True
    if not degisti:
        return
    try:
        PRINCIPALS.write_text(
            json.dumps(veri, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        uyari("principals.json guncellenemedi; ORG_SCOPE reddi surebilir")
        return
    bilgi(
        "yetki alani aktif profile uyduruldu: tesis "
        + ", ".join(kapsam["plants"])
    )


def kimlikleri_hazirla() -> dict[str, str]:
    """Token'ları döndürür. Varsa yeniden üretmez — her çalıştırmada sabit."""
    mevcut = yerel_env_oku()
    beklenen = {f"CERTAOPS_TOKEN_{k[0]}" for k in KIMLIKLER}
    if PRINCIPALS.exists() and beklenen.issubset(mevcut.keys()):
        ok("mevcut kimlikler kullanılıyor (.env.local)")
        org_kapsamini_tazele()
        return mevcut

    bilgi("yeni kimlikler üretiliyor…")
    PRINCIPALS.parent.mkdir(parents=True, exist_ok=True)

    kayitlar, tokenlar = [], {}
    kapsam = org_kapsami()
    for anahtar, subject, ad, roller, _ in KIMLIKLER:
        # URL-güvenli 24 baytlık rastgele değer. Düz metin yalnızca
        # .env.local'da; principals.json'a sadece sha256 özeti gider.
        token = "cop_" + secrets.token_urlsafe(24)
        tokenlar[f"CERTAOPS_TOKEN_{anahtar}"] = token
        kayitlar.append({
            "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "subject": subject,
            "display_name": ad,
            "tenant": "100",
            "roles": roller,
            "company_codes": kapsam["company_codes"],
            "plants": kapsam["plants"],
            "purchasing_orgs": kapsam["purchasing_orgs"],
        })

    PRINCIPALS.write_text(json.dumps({
        "_comment": [
            "baslat.py tarafindan uretildi. Duz metin token BU DOSYADA YOK;",
            "yalniz sha256 ozetleri saklanir. Token'lar .env.local icindedir.",
            "Silerseniz bir sonraki calistirmada yenileri uretilir.",
        ],
        "principals": kayitlar,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    govde = [
        "# CertaOps yerel token'lari — baslat.py tarafindan uretildi.",
        "# BU DOSYAYI PAYLASMAYIN. .gitignore kapsamindadir (.env*).",
        "# Yeni token istiyorsaniz bu dosyayi ve config/principals.json'u silin.",
        "",
    ]
    govde += [f"{k}={v}" for k, v in tokenlar.items()]
    YEREL_ENV.write_text("\n".join(govde) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        YEREL_ENV.chmod(0o600)

    ok(f"{len(KIMLIKLER)} kimlik üretildi")
    return tokenlar


# ==========================================================================
# 4) Ayarlar
# ==========================================================================
def env_dosyasinda_dolu(ad: str) -> bool:
    """`.env` içinde bu anahtar dolu bir değerle tanımlı mı?"""
    dosya = PROJE / ".env"
    if not dosya.exists():
        return False
    try:
        icerik = dosya.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for satir in icerik.splitlines():
        satir = satir.strip()
        if not satir.startswith(ad + "="):
            continue
        return bool(satir[len(ad) + 1:].strip().strip("\"'"))
    return False


def anahtar_var(ad: str) -> bool:
    return bool(os.environ.get(ad, "").strip()) or env_dosyasinda_dolu(ad)


def env_dosyasindan_oku(ad: str) -> str:
    """`.env` icindeki bir anahtarin ham degeri. Yoksa bos string."""
    dosya = PROJE / ".env"
    if not dosya.exists():
        return ""
    try:
        icerik = dosya.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for satir in icerik.splitlines():
        satir = satir.strip()
        if satir.startswith("#") or not satir.startswith(ad + "="):
            continue
        return satir[len(ad) + 1:].strip().strip("\"'")
    return ""


# `--sap` verildiginde `.env`den tasinacak baglanti anahtarlari. Yazma
# kilitleri (SAP_READ_ONLY / SAP_DRY_RUN) bilerek DISARIDA: onlar her
# modda acik kalir.
SAP_BAGLANTI_ANAHTARLARI = (
    "SAP_BACKEND",
    "SAP_BASE_URL",
    "SAP_ALLOWED_HOSTS",
    "SAP_AUTH_MODE",
    "SAP_API_KEY",
    "SAP_API_KEY_HEADER",
    "SAP_USERNAME",
    "SAP_PASSWORD",
    "SAP_CLIENT",
    "SAP_TENANT",
    "SAP_SYSTEM_ALIAS",
    "SAP_ODATA_VERSION",
    "SAP_COMPANY_CODE",
    "SAP_PLANT",
    "SAP_PURCH_ORG",
    "SAP_PURCH_GROUP",
    "SAP_CURRENCY",
    "SAP_STORAGE_LOCATION",
    "SAP_DESCRIPTION_LANGUAGE",
    "SAP_OAUTH_TOKEN_URL",
    "SAP_OAUTH_CLIENT_ID",
    "SAP_OAUTH_CLIENT_SECRET",
    "SAP_OAUTH_SCOPE",
    "SAP_DESTINATION_NAME",
    "SAP_DESTINATION_SERVICE_URL",
)


def canli_sap_istendi() -> bool:
    """`./baslat.command --sap` veya `SAP=canli ./baslat.command`."""
    return _acik_mod_secimi() is True


def ayarlari_hazirla(
    *, canli: bool | None = None, rol_anahtari: str = "DENETCI"
) -> tuple[dict[str, str], bool]:
    """Servise verilecek ortam. `.env` dosyasına DOKUNULMAZ.

    config.py `load_dotenv(override=False)` kullandığı için buradaki
    değerler .env'in önüne geçer ama dosyayı değiştirmez.
    """
    env = dict(os.environ)

    try:
        _anahtar, mcp_subject, _ad, mcp_roller, _aciklama = next(
            kimlik for kimlik in KIMLIKLER if kimlik[0] == rol_anahtari
        )
    except StopIteration as exc:
        raise ValueError(f"Bilinmeyen rol: {rol_anahtari}") from exc

    # HTTP isteklerinde kimlik token'dan cozulur. stdio MCP'de istek basina
    # kimlik bulunmadigi icin ayni secimi surec actor'una acikca aktaririz;
    # aksi halde arayuz "Denetci" iken MCP audit'i "local-operator" yazar.
    env["AGENT_LOCAL_SUBJECT"] = mcp_subject
    env["AGENT_LOCAL_ROLES"] = ",".join(mcp_roller)

    def varsayilan(anahtar: str, deger: str) -> None:
        env.setdefault(anahtar, deger)

    varsayilan("APP_ENV", "staging")

    # Yazma kilitleri her iki modda da acik. Kabuktan yanlislikla `false`
    # gelmis olsa bile baslat.command salt-okunur sozlesmesini gevsetmez.
    # Gercek yazma bu yerel operator baslaticisinin kapsami disindadir.
    env["SAP_READ_ONLY"] = "true"              # mutasyon gorunmez ve calismaz
    env["SAP_DRY_RUN"] = "true"                # ikinci guvenlik kilidi

    # Baglanti profili. Varsayilan simulasyondur: `--sap` verilmedikce
    # hicbir gercek SAP adresine cikilmaz ve allowlist localhost'ta kalir.
    if canli is None:
        canli = canli_sap_istendi()
    if canli:
        tasinanlar: list[str] = []
        for anahtar in SAP_BAGLANTI_ANAHTARLARI:
            # Kabuktan verilen deger her zaman kazanir; `.env` onun altinda.
            if os.environ.get(anahtar, "").strip():
                continue
            deger = env_dosyasindan_oku(anahtar)
            if deger:
                env[anahtar] = deger
                tasinanlar.append(anahtar)
        if env.get("SAP_BACKEND", "mock") == "mock" or not env.get("SAP_BASE_URL", ""):
            # Yarim bir canli profille devam etmek yerine simulasyona TAM
            # donulur: tasinan her anahtar geri alinir, yoksa allowlist
            # gercek host'ta kalir ve mod ile ayar birbirini tutmaz.
            for anahtar in tasinanlar:
                env.pop(anahtar, None)
            uyari("--sap verildi ama .env'de canli profil yok — simülasyona dönülüyor")
            bilgi("Gerekli: SAP_BACKEND=odata, SAP_BASE_URL=…, SAP_ALLOWED_HOSTS=…")
            canli = False
        else:
            bilgi(f".env'den {len(tasinanlar)} SAP ayarı taşındı")

    if not canli:
        # Menu secimi kabuktaki/.env'deki canli profilden ustundur. Aksi
        # halde operator "Simulasyon"u secerken miras kalmis SAP_BACKEND
        # degiskeni gercek sisteme cikabilirdi.
        env["SAP_BACKEND"] = "mock"            # gerçek SAP'a bağlanmaz
        env["SAP_ALLOWED_HOSTS"] = "localhost"
    varsayilan("AGENT_SESSION_BACKEND", "sqlite")
    varsayilan("AGENT_STATE_DIR", str(PROJE / "state"))
    varsayilan("OUTPUT_DIR", str(PROJE / "output"))
    varsayilan("AGENT_DLP_MODE", "enforce")
    varsayilan("AGENT_RISK_SCORING_MODE", "enforce")
    varsayilan("LOG_BUFFER_SIZE", "500")
    varsayilan("LOG_MASK", "true")
    varsayilan("AGENT_PSEUDONYMIZATION_KEY_ID", "local://pseudonym-v1")
    varsayilan("AGENT_KMS_KEY_ID", "local://data-key-v1")

    # Bunlar pazarlık konusu değil: arayüz kimlik doğrulamasız sunulmaz.
    env["AGENT_AUTH_MODE"] = "static_token"
    env["AGENT_PRINCIPALS_FILE"] = str(PRINCIPALS)
    env["AGENT_UI_ENABLED"] = "true"
    env["AGENT_UI_LOG_STREAM"] = "true"
    env["AGENT_D3_CACHE_ENABLED"] = "false"

    if canli:
        host = env.get("SAP_ALLOWED_HOSTS", "?")
        ok(f"CANLI SAP · {env.get('SAP_BACKEND')} · {host} · salt-okunur · dry-run kilidi açık")
        uyari("Gerçek SAP okumaları yapılacak. Yazma her iki kilitle de kapalı.")
    else:
        ok("simülasyon modu · dry-run kilidi açık · gerçek SAP'a bağlanmaz")
        bilgi("Gerçek SAP okumaları için:  ./baslat.command --sap")

    # Acik MODEL_PROVIDER secimi, hangi anahtarin dosyada ilk bulundugundan
    # daha ustundur. Onceki kod iki anahtar da varken Gemini'yi kosulsuz
    # seciyordu; `.env` MODEL_PROVIDER=anthropic + MODEL_NAME=claude-sonnet-5
    # olsa bile alt surece MODEL_PROVIDER=gemini yaziliyor ve Gemini'ye Claude
    # model adi gonderilerek her soru `bad_request` ile bitiyordu.
    secilen_saglayici = (
        os.environ.get("MODEL_PROVIDER", "").strip()
        or env_dosyasindan_oku("MODEL_PROVIDER").strip()
    ).lower()
    if not secilen_saglayici:
        if anahtar_var("GEMINI_API_KEY") or anahtar_var("GOOGLE_API_KEY"):
            secilen_saglayici = "gemini"
        elif anahtar_var("ANTHROPIC_API_KEY"):
            secilen_saglayici = "anthropic"

    sohbet = True
    if secilen_saglayici == "gemini":
        env["MODEL_PROVIDER"] = "gemini"
        sohbet = anahtar_var("GEMINI_API_KEY") or anahtar_var("GOOGLE_API_KEY")
    elif secilen_saglayici == "anthropic":
        env["MODEL_PROVIDER"] = "anthropic"
        sohbet = anahtar_var("ANTHROPIC_API_KEY")
    elif secilen_saglayici == "fake":
        env["MODEL_PROVIDER"] = "fake"
    else:
        sohbet = False

    if sohbet:
        ok(f"model: {secilen_saglayici}")
    else:
        uyari(
            f"model sağlayıcısı kullanılamıyor: "
            f"{secilen_saglayici or 'seçim/anahtar yok'} — sohbet sekmesi çalışmaz"
        )
        bilgi("Tool'lar · Durum · Telemetri · Denetim · Log sekmeleri çalışır.")
        bilgi(
            "Seçilen sağlayıcının anahtarını .env'e ekleyin: "
            "GEMINI_API_KEY=… veya ANTHROPIC_API_KEY=…"
        )

    Path(env["AGENT_STATE_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(env["OUTPUT_DIR"]).mkdir(parents=True, exist_ok=True)
    return env, sohbet


# ==========================================================================
# 5) Port
# ==========================================================================
def port_bos(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def port_sec() -> int:
    istenen = int(os.environ.get("PORT", "8000"))
    if port_bos(istenen):
        ok(f"127.0.0.1:{istenen}")
        return istenen

    uyari(f"{istenen} kullanımda")
    for port in range(istenen + 1, istenen + 21):
        if port_bos(port):
            ok(f"127.0.0.1:{port}")
            return port

    hata(f"{istenen}–{istenen + 20} aralığında boş port yok.")
    bilgi("Çalışan servisi kapatın veya:  PORT=9000 ./baslat.command")
    dur()
    return 0  # buraya ulaşılmaz


# ==========================================================================
# 6) Başlat
# ==========================================================================
def panoya_kopyala(metin: str) -> bool:
    if not shutil.which("pbcopy"):
        return False
    try:
        subprocess.run(["pbcopy"], input=metin.encode(), check=True, timeout=10)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def tarayiciyi_ac_hazir_olunca(adres: str, port: int) -> None:
    """Servis ayağa kalkınca tarayıcıyı açar. Ayrı bir thread'de çalışır."""
    for _ in range(120):
        baglanti = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            baglanti.request("GET", "/health")
            if baglanti.getresponse().status == 200:
                break
        except (http.client.HTTPException, OSError):
            time.sleep(0.5)
        finally:
            baglanti.close()
    else:
        return
    acici = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(acici):
        subprocess.Popen([acici, adres],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ozet_yaz(
    adres: str,
    tokenlar: dict[str, str],
    sohbet: bool,
    pano: bool,
    rol_anahtari: str,
) -> None:
    cizgi = "  " + "─" * 60
    print(f"\n{R.KALIN}{cizgi}{R.SIFIR}")
    print(f"  {R.MAVI}{R.KALIN}{adres}{R.SIFIR}")
    print(f"{R.KALIN}{cizgi}{R.SIFIR}\n")

    kimlik = next(k for k in KIMLIKLER if k[0] == rol_anahtari)
    _anahtar, _subject, gorunen_ad, roller, aciklama = kimlik
    token = tokenlar.get(f"CERTAOPS_TOKEN_{rol_anahtari}", "—")

    if pano:
        print(f"  {gorunen_ad} token'ı {R.YESIL}panoya kopyalandı{R.SIFIR} — "
              f"token alanına {R.KALIN}⌘V{R.SIFIR} yapıp Bağlan.\n")

    print(f"  {R.KALIN}SECILEN ROL{R.SIFIR}\n")
    print(f"  {R.KALIN}{gorunen_ad}{R.SIFIR}  [{', '.join(roller)}]")
    print(f"  {R.SOLUK}{aciklama}{R.SIFIR}")
    print(f"  {token}\n")
    bilgi("Farkli bir rol icin servisi durdurup baslat.command'i yeniden calistirin.")

    if not sohbet:
        print(f"  {R.SARI}Sohbet için model anahtarı yok{R.SIFIR} — "
              "diğer sekmeler çalışıyor.\n")

    print(f"  {R.SOLUK}Durdurmak için Ctrl+C · servis kayıtları aşağıda{R.SIFIR}")
    print(f"{R.SOLUK}{cizgi}{R.SIFIR}\n")


def servisi_calistir(py: Path, port: int, env: dict[str, str]) -> int:
    """uvicorn'u ön planda çalıştırır.

    Arka plana alınıp beklenirse Ctrl+C ve pencere kapatma (SIGHUP) sinyalleri
    yalnızca bu sürece gider; uvicorn portu tutan öksüz bir süreç olarak kalır.
    Aynı süreç grubunda ön planda çalıştırıldığında sinyal doğrudan ona da
    ulaşır. `finally` bloğu yine de son bir güvence olarak durdurur.
    """
    komut = [str(py), "-m", "uvicorn", "certaops.api:app",
             "--host", "127.0.0.1", "--port", str(port),
             "--app-dir", str(PROJE / "src")]
    surec = subprocess.Popen(komut, cwd=str(PROJE), env=env)
    try:
        return surec.wait()
    except KeyboardInterrupt:
        print(f"\n{R.SOLUK}Kapatılıyor…{R.SIFIR}")
        return 0
    finally:
        if surec.poll() is None:
            surec.terminate()
            try:
                surec.wait(timeout=10)
            except subprocess.TimeoutExpired:
                surec.kill()
                surec.wait(timeout=5)


# ==========================================================================
def main() -> int:
    # Ctrl+C'yi Python'un traceback'i yerine kendimiz karşılayalım.
    signal.signal(signal.SIGINT, signal.default_int_handler)

    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        print(__doc__ or "CertaOps baslaticisi")
        return 0

    if not (PROJE / "pyproject.toml").exists():
        hata(f"Proje kökü doğrulanamadı: {PROJE}")
        bilgi("baslat.command dosyasını proje klasöründen taşımayın.")
        dur()

    print(f"{R.KALIN}  CERTAOPS{R.SIFIR}")
    print(f"{R.SOLUK}  read-only SAP S/4HANA operatör konsolu{R.SIFIR}")
    print(f"{R.SOLUK}  {PROJE}{R.SIFIR}")

    try:
        canli, rol_anahtari = baslangic_secimleri()
    except ValueError as exc:
        hata(str(exc))
        dur(2)
    ok(
        "secim: "
        + ("canli SAP" if canli else "simulasyon")
        + f" · rol: {rol_anahtari.lower()}"
    )

    baslik("1/5  Python")
    py = venv_python()

    baslik("2/5  Bağımlılıklar")
    bagimliliklari_kur(py)

    baslik("3/5  Kimlikler")
    tokenlar = kimlikleri_hazirla()

    baslik("4/5  Ayarlar")
    env, sohbet = ayarlari_hazirla(canli=canli, rol_anahtari=rol_anahtari)

    baslik("5/5  Servis")
    port = port_sec()
    adres = f"http://127.0.0.1:{port}/ui"

    pano = panoya_kopyala(tokenlar.get(f"CERTAOPS_TOKEN_{rol_anahtari}", ""))
    threading.Thread(target=tarayiciyi_ac_hazir_olunca,
                     args=(adres, port), daemon=True).start()

    ozet_yaz(adres, tokenlar, sohbet, pano, rol_anahtari)

    kod = servisi_calistir(py, port, env)
    if kod in (0, -signal.SIGINT, -signal.SIGTERM, -signal.SIGHUP, 130, 143):
        print(f"\n{R.SOLUK}Servis durduruldu.{R.SIFIR}")
        return 0

    hata(f"Servis beklenmedik şekilde kapandı (çıkış kodu {kod}).")
    dur(kod if 0 < kod < 256 else 1)
    return kod


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(f"\n{R.SOLUK}Servis durduruldu.{R.SIFIR}")
        sys.exit(0)
