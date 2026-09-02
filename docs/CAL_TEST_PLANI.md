# SAP CAL test planı

Bu plan, SAP S/4HANA 2025 FPS01 Fully-Activated Appliance'ı mümkün olan en
kısa aktif sürede kullanarak CertaOps'un 21 tool'unu kanıtlı biçimde test etmek
içindir. Amaç yalnız HTTP 200 görmek değil; gerçek malzeme, PO, mal kabul,
fatura, yetki reddi ve kontrollü PR yazma sonuçlarını doğrulamaktır.

## CAL hakkında doğrulanan gerçekler

- Trial sözleşmesi kabul edildiği anda 30 günlük süre başlar. Appliance suspend
  edilse de bu takvim durmaz.
- İlk 30 günde CAL ve SAP ürün trial lisansı için SAP'a ücret ödenmez; AWS,
  Azure veya GCP altyapı maliyeti kullanıcıya aittir.
- Suspend işlem maliyetini düşürür. Sistem artık kullanılmayacaksa yalnız
  suspend etmek yerine terminate etmek gerekir; aksi durumda kalıcı kaynak
  maliyetleri devam edebilir.
- 2025 FPS01 Fully-Activated Appliance'ın cloud provisioning'i yaklaşık 1–2
  saat sürer. Bu sürede script çalışmaz; iş büyük ölçüde SAP CAL ve
  hyperscaler tarafında gerçekleşir.
- Örnek Best Practices ve transactional data client `100` içindedir. Yaygın US
  demo organizasyonu company code `1710` kullanır; canlı kesif scripti bunun
  hedef appliance'ta gerçekten geçerli olduğunu ayrıca kontrol eder.
- Appliance, S/4HANA Cloud Public Edition tenant değildir. On-premise/private
  edition karakterinde yönetilen bir S/4HANA sistemidir; V2 Gateway servisleri
  ve `/IWFND/MAINT_SERVICE` aktivasyonu gerekebilir.

Resmî kaynaklar:

- https://pages.community.sap.com/topics/cloud-appliance-library/appliance-templates-faq/faq-trial-templates
- https://help.sap.com/docs/sap-cloud-appliance-library/documentation/working-with-appliances
- https://help.sap.com/doc/576b86629dd7101493c5a810a574e465/SHIP/en-US/SAP_Cloud_Appliance_Library_Documentation_en.pdf
- https://community.sap.com/t5/enterprise-resource-planning-blog-posts-by-sap/sap-s-4hana-fully-activated-appliance-create-your-sap-s-4hana-system-in-a/bc-p/13365970
- https://community.sap.com/t5/technology-blog-posts-by-sap/sap-s-4hana-fully-activated-appliance-demo-guides/bc-p/13389427/highlight/true

## CAL açılmadan tamamlanan işler

- 21 tool envanteri yerel kapıda doğrulanıyor.
- API Hub'da 18 salt-okunur/hazırlık tool'u ayrıca doğrulandı.
- Released API metadata'sına göre aşağıdaki gerçek S/4 OData uygulamaları
  eklendi:
  - PO kalemleri ve schedule line
  - Material Document üzerinden 101/102/122/162 hareketleri
  - Supplier Invoice başlığı ve PO item referansları
  - PO → GR → invoice belge akışı
- Teslim ve fatura miktarları `complete` bayrağından tahmin edilmiyor; gerçek
  MSEG/RSEG referanslarından hesaplanıyor.
- Fatura API'si yalnız ödeme blokaj anahtarını yayımlıyorsa OMR6 toleransı
  uydurulmuyor; raporda eksik kaynak açıkça gösteriliyor.
- Sonuçlar terminal, JSON, Markdown ve JUnit olarak üretiliyor.
- Canlı metadata envanteri, sınırlı veri profili, join kapsama oranları ve
  tool fırsat puanları otomatik kaydediliyor. Ham SAP satırları, tedarikçi
  adları ve tutarlar bu analiz dosyalarına yazılmıyor.
- Generated `artifacts/` ve `.env.cal` Git dışında tutuluyor.

Daha önce iki dış bağımlılık (`sap_workflow_status` ve `sap_project_cost_status`)
CAL kabulünü `BLOCKED` bırakıyordu. Bu iki tool ile `sap_atp_check` projeden
kaldırıldı: hiçbirinin standart S/4 released API karşılığı yoktu. Kalan 21
tool'un tamamı gerçek bir released servise dayanır.

## Aşamalar ve gerçek süre beklentisi

| Aşama | Script | Normal otomatik süre | Ne kanıtlar |
|---|---|---:|---|
| CAL-00 | `cal_00_preflight.py` | 10–30 sn | Kod, manifest, pytest ve ruff |
| CAL-01 | `cal_01_connection.py` | 5–30 sn | TLS, auth, client, allowlist, ping |
| CAL-02 | `cal_02_contracts.py` | 1–5 dk | 13 released servisin canlı metadata'sı |
| CAL-03 | `cal_03_read_scenarios.py` | 3–10 dk | 15 çekirdek read/prepare tool'u |
| CAL-04 | `cal_04_p2p_scenarios.py` | 2–8 dk | PO–GR–invoice ve raporlama |
| CAL-05 | `cal_05_security_scenarios.py` | 1–4 dk | RBAC, negatif kayıt, SSRF, DLP, audit |
| CAL-06 | `cal_06_write_pr.py` | 1–5 dk | Dry-run veya tek PR + idempotency |
| CAL-07 | `cal_07_service_inventory.py` | 1–4 dk | 14 alias, fiziksel servisler, entity/alan/join seması |
| CAL-08 | `cal_08_data_profile.py` | 1–5 dk | En fazla 20 satırlı doluluk, join ve gecikme profili |
| CAL-09 | `cal_09_tool_opportunities.py` | < 5 sn | Kanıta dayal yeni tool puanı ve geliştirme kapısı |

Her aşamaya bir saat gerekmez. Önceki bir saatlik bloklar manuel servis
aktivasyonu ve veri arama için emniyet payıydı. Servisler hazır ve gerçek test
ID'leri verilmişse otomatik canlı koşunun hedefi yaklaşık **10–30 dakika**dır.

Provisioning 1–2 saat sürebilir ama kullanıcı emeği değildir. Servis eksikse
orkestratör fail-fast durur; CAL suspend edilir, eksik aktivasyon planlanır ve
sonraki aktif pencerede devam edilir. Böylece saatlerce hata tekrarlanmaz.

## Çalıştırma

CAL açılmadan:

```bash
python scripts/cal_acceptance.py --pre-cal
```

CAL Connection Details belli olduğunda:

```bash
cp .env.cal.example .env.cal
```

`.env.cal` içine FQDN, kullanıcı ve parola yazıldıktan sonra salt-okunur kabul:

```bash
python scripts/cal_acceptance.py --live \
  --material <MATERIAL> \
  --vendor <VENDOR> \
  --po <PO> \
  --invoice <INVOICE> \
  --wbs <WBS>
```

ID'ler verilmezse script en fazla beş PO'yu kontrollü biçimde tarayarak gerçek
referansları keşfeder. ID vermek daha hızlı ve tekrarlanabilirdir. Canlı
orkestratör CAL-07/08 notlarını çekirdek senaryolardan önce kaydeder; herhangi
bir test kırmızı olursa notlar kalır ve yeni tool geliştirme kapısı açılmaz.

Tek tek aşama çalıştırılabilir:

```bash
python scripts/cal_01_connection.py --env-file .env.cal
python scripts/cal_02_contracts.py --env-file .env.cal
python scripts/cal_03_read_scenarios.py --env-file .env.cal --material <MATERIAL>
python scripts/cal_04_p2p_scenarios.py --env-file .env.cal --po <PO>
python scripts/cal_05_security_scenarios.py --env-file .env.cal --material <MATERIAL>
python scripts/cal_06_write_pr.py --env-file .env.cal --material <MATERIAL>
python scripts/cal_07_service_inventory.py --env-file .env.cal
python scripts/cal_08_data_profile.py --env-file .env.cal --material <MATERIAL> --po <PO>
python scripts/cal_09_tool_opportunities.py --env-file .env.cal
```

CAL-06 varsayılan olarak yalnız dry-run yapar. Tek gerçek PR ancak dört
kapıyla açılır:

```bash
SAP_DRY_RUN=false SAP_INTEGRATION_ALLOW_WRITE=1 \
python scripts/cal_06_write_pr.py \
  --env-file .env.cal \
  --material <MATERIAL> \
  --execute-write \
  --confirm CREATE-ONE-CAL-PR
```

Script onay eşiğinin üstünde otomatik onay üretmez. Yalnız bir PR oluşturur,
geri okur ve aynı idempotency key ile tekrar çağrının ikinci belge
oluşturmadığını doğrular.

## Sonuçların yorumlanması

- `PASS`: Teknik çağrı ve senaryo koşulu kanıtlandı.
- `WARN`: Tool çalıştı fakat seçilen demo verisi örneğin blokeli fatura veya
  iki tedarikçi içermiyor.
- `FAIL`: Kod, kontrat, auth veya gerçek iş sonucu hatalı.
- `BLOCKED`: Gerekli SAP capability/custom servis mevcut değil; başarı gibi
  gösterilmez.
- `SKIP`: Kullanıcı tarafından bilinçli atlandı.

`artifacts/cal/<UTC-run>/` altında şunlar oluşur:

- Aşama JSON raporları
- `summary.json`
- İnsan okunur `summary.md`
- CI uyumlu `junit.xml`
- `service_inventory.json`: OData entity, alan, anahtar, navigation ve action envanteri
- `data_profile.json`: ham değersiz doluluk, benzersizlik, join ve performans profili
- `tool_opportunities.json`: adayların servis/veri/join hazırlık puanı
- `development_notes.md`: başarılı testten sonra geliştirilecek adaylar ve eksikler

Yeni tool kodu bu koşuda otomatik yazılmaz. `development_gate=OPEN` ve aday
durumu `READY` olmadıkça geliştirme başlatılmaz; bu sayede zayıf veya tesadüfi
demo verisine dayalı tool eklenmez.

Raporlar dışa alındıktan sonra appliance suspend edilmelidir. Deneme tamamen
bittiyse ve veri/kanıt gerekmiyorsa CAL ile hyperscaler kaynakları terminate
edilerek kalan altyapı maliyeti durdurulmalıdır.
