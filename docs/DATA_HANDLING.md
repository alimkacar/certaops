# Veri isleme, saklama ve silme

Bu belge CertaOps'un hangi veriyi nerede, ne kadar sure tuttugunu ve neyi hic
tutmadigini tek yerde toplar. Rehberin Ucuncu Asama checklist maddesi
("Veri saklama, silme ve API ayarlarini belgeleyin") bu belgeyle karsilanir.

> **Sinir.** Bu belge kodun ne yaptigini anlatir. Kurulusun KVKK, GDPR, veri
> yerlesimi ve sektorel saklama yukumlulukleri ayrica hukuk ve bilgi guvenligi
> ekipleriyle dogrulanmalidir. CertaOps bir uyum urunu degildir.

---

## 1. Veri siniflandirmasi

Her alan D0–D3 arasinda siniflandirilir (`privacy/classification.py`). Sinif
alanin **adindan** ve degerinin **iceriginden** bagimsiz olarak belirlenir;
ikisi de kontrol edilir.

| Sinif | Anlam | Ornek | Modele gider mi | Cache'lenir mi | Log'a yazilir mi |
|---|---|---|---|---|---|
| D0 | Genel | Sistem durumu, malzeme tipi | Evet | Evet | Evet |
| D1 | Ic kullanim | Stok, ATP, siparis durumu | Evet | Evet | Sayim/kimlik olarak |
| D2 | Hassas is verisi | Fiyat, tedarikci skoru, e-posta, telefon | Maskeli | Evet (tavan D2) | Hayir |
| D3 | Kisisel/gizli | IBAN, vergi/kimlik no, maas, parola, token | **Hayir** (tokenlastirilir) | **Asla** | **Asla** |

D3 hicbir kosulda cache'e yazilmaz (`CachePolicy.allows`) ve
`AGENT_D3_CACHE_ENABLED=false` varsayilandir.

---

## 2. Nerede ne saklanir

| Depo | Icerik | Konum | TTL / temizlik |
|---|---|---|---|
| Oturum deposu | Konusma transcripti (DLP'den gecmis) | `state/agent_state.sqlite3` | `AGENT_SESSION_TTL_HOURS` (24s) |
| Onay kayitlari | Onay kimligi, payload hash'i, onaylayanlar | ayni veritabani | `AGENT_APPROVAL_TTL_MIN` (60dk) |
| Idempotency | Anahtar, durum, is nesnesi kimligi | ayni veritabani | Yazma yasam dongusu boyunca |
| Evidence | Butce nedeniyle kirpilan tam tool sonuclari | ayni veritabani | `AGENT_EVIDENCE_TTL_MIN` (120dk) |
| Audit defteri | Karar zinciri, hash'ler, maskelenmis ozet | ayni veritabani + `audit_ledger.jsonl` | **Silinmez** (bkz. §5) |
| Audit checkpoint | Zincir basi ozeti | `AGENT_AUDIT_CHECKPOINT_PATH` | Harici hedefin politikasi |
| Cache | SAP okuma sonuclari | Bellek (varsayilan) | `AGENT_CACHE_DEFAULT_TTL_SECONDS` (60sn) |
| Artefakt | Uretilen rapor dosyalari | `OUTPUT_DIR` | `AGENT_ARTIFACT_TTL_HOURS` (24s) |
| Telemetri | Sayim ve siniflandirma; **deger yok** | Bellek (son 200 tur) | Surec omru |

Temizlik dongusu `AGENT_RETENTION_SWEEP_SECONDS` (900sn) araliginda calisir
(`privacy/retention.py`). Sureci sonlanan kayitlar silinir; audit haric.

---

## 3. Model saglayicisina ne gider

CertaOps varsayilan olarak **stateless** calisir.

| Ayar | Varsayilan | Etkisi |
|---|---|---|
| `GEMINI_STORE_INTERACTIONS` | `false` | Saglayicida etkilesim kaydi tutulmaz |
| Reasoning/thought adimlari | Bellekte, tur sonunda atilir | Transcript, oturum, audit ve log'a **hic** yazilmaz |
| `MODEL_PROVIDER=anthropic` | opsiyonel | Ayni DLP kapilari uygulanir |

`GEMINI_STORE_INTERACTIONS=true` uretim profilinde **fail-closed blocker**dir:
servis baslamaz.

Modele giden her sey `privacy/dlp.py` icinden `sink="model"` ile gecer:

- Alan adi D3 ise deger tokenlastirilir (deger desene uymasa bile).
- Serbest metinde IBAN, kart (Luhn dogrulamali), vergi/kimlik no (baglam
  kontrollu), Bearer token, e-posta ve telefon yakalanir.
- Gorunmez Unicode karakterler analiz oncesi temizlenir.
- System prompt'a SAP endpoint'i, destination adi veya client kimligi
  **konmaz** — model bunlara ihtiyac duymaz.

Modele **hicbir zaman** gitmeyenler: parola, API anahtari, refresh token,
sertifika ozel anahtari, `Authorization` basligi, connection string.

---

## 4. SAP'tan ne okunur

Veri minimizasyonu kaynakta baslar:

- `$select` **zorunlu** ve sabit allowlist'tir (`sap/odata.py::SELECT_FIELDS`).
  `$select` verilmeseydi `A_Supplier` vergi numarasi, banka hesabi ve adres
  alanlarini da dondururdu.
- `$filter` ifadelerini yalniz backend kurar; model semantik parametre verir.
- Sayfalama ve satir siniri: `SAP_PAGE_SIZE`, `SAP_MAX_PAGES`, tool basina
  `max_records`.
- Sonuc token butcesi asilirsa tam kayit evidence store'a tasinir, konusmaya
  ozet + `evidence_id` girer.

---

## 5. Audit: neden silinmez, sinirlari ne

Audit defteri **silinmez**. Saklama politikasi kurulusa aittir; teknik olarak
kayitlar hash zinciriyle baglidir ve silme zinciri kirar.

Deftere **yazilmayanlar**: tam prompt, tam SAP payload'i, parola/token
(`redact()` ile maskelenir), IBAN/kimlik/maas, ham dosya ekleri, modelin gizli
reasoning icerigi. 600 karakterden uzun degerler hash + boyut olarak saklanir.

**Butunluk sinirini acikca yaziyoruz:** defter *tamper-evident*'tir, *immutable*
degildir. Veritabanina yazma yetkisi olan biri zinciri bastan uretebilir.
Bunun karsi tedbiri harici checkpoint'tir:

```ini
AGENT_AUDIT_CHECKPOINT_PATH=/var/worm/certaops-checkpoints.jsonl
AGENT_AUDIT_CHECKPOINT_EVERY=50
```

Hedefin **uygulama kullanicisi tarafindan degistirilemez** olmasi gerekir
(object-lock'lu bucket, baska bir hesabin log servisi). Ayni makinede duran bir
dosya yalnizca kaza sonucu bozulmayi yakalar, kotu niyetli bir yoneticiyi
degil. `GET /health` altindaki `audit_checkpoint_export.configured` alani hedef
tanimli degilse bunu acikca bildirir.

---

## 6. Kisisel veri talepleri

| Talep | Yol |
|---|---|
| Erisim | `GET /sessions` (actor'a ait oturumlar), `sap_get_execution_audit` |
| Silme | `DELETE /sessions/{id}` — transcript ve evidence silinir |
| Duzeltme | Kaynak SAP sisteminde yapilir; CertaOps kopya tutmaz |
| Itiraz / kisitlama | Actor'un yetki kapsami daraltilir; cache anahtari kapsam hash'i icerdigi icin eski cevaplar otomatik gecersizlesir |

Audit kayitlari silme talebinin **kapsami disindadir**: yasal saklama
yukumlulugu ve butunluk zinciri nedeniyle. Audit'te zaten ham kisisel veri
degil, maskelenmis alanlar ve hash'ler tutulur.

Surrogate ID: `privacy/pseudonymization.py` oturumluk takma kimlik uretir;
gercek SAP anahtarina cevirme backend'de yapilir, model gercek kimligi gormez.

---

## 7. Tenant izolasyonu

- Cache anahtari tenant + yetki kapsami hash'i + sistem + organizasyon +
  tool surumu icerir; iki tenant yapisal olarak ayni anahtara dusemez.
- Cache silme/invalidation her zaman tenant sinirlidir.
- Audit, oturum, onay ve idempotency kayitlari tenant kolonuyla ayrilir.
- Kullanicinin yetki kapsami degisirse anahtar degisir; eski cevap
  dondurulemez.

---

## 8. Uretim kontrol listesi

`APP_ENV=production` ile servis, asagidakiler saglanmadan **baslamaz**
(`config.py::production_blockers`):

- [ ] `SAP_BACKEND` mock degil
- [ ] `SAP_ALLOWED_HOSTS` dolu (egress allowlist)
- [ ] `SAP_AUTH_MODE` basic/apikey degil
- [ ] `AGENT_AUTH_MODE` none degil
- [ ] `GEMINI_STORE_INTERACTIONS=false`
- [ ] Oturum backend'i sqlite (bellek degil)
- [ ] Evidence sifreleme anahtari tanimli

Bloker olmayan ama **onerilen**:

- [ ] `AGENT_AUDIT_CHECKPOINT_PATH` harici WORM hedefine bakiyor
- [ ] `MODEL_COST_*` degerleri girilmis (maliyet raporlamasi icin)
- [ ] `AGENT_CACHE_BACKEND` coklu worker'da paylasimli bir backend

---

## 9. Ilgili kod

| Konu | Dosya |
|---|---|
| Siniflandirma | `privacy/classification.py` |
| DLP motoru | `privacy/dlp.py` |
| Alan erisimi | `privacy/field_policy.py` |
| Takma kimlik | `privacy/pseudonymization.py` |
| Saklama/temizlik | `privacy/retention.py` |
| Cache anahtari | `cache/base.py` |
| Audit + checkpoint | `core/audit.py` |
| Uretim kapilari | `config.py` |
