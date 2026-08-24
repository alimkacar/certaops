# Gecis notu: multi-agent -> tek runtime + saglayici-bagimsiz model katmani

Surum 0.2.0. Bu belge **ne degisti, neden ve nasil gecilir** sorularini
cevaplar. Mevcut kod bir anda kirilmaz; eski adlar facade olarak korunur.

---

## 1. Mimari: bes agent yerine tek runtime

### Onceki hal

Her SAP domaini ayri bir `SAPDomainAgent` ornegiydi. Her birinin kendi model
istemcisi, kendi konusma gecmisi ve kendi LLM cagrisi vardi. `SAPMultiAgent`
bunlari **sirayla** calistirir, aralarinda `HandoffEnvelope` tasir ve
sonuclari model disinda metin olarak birlestirirdi.

Sonuc: "stok durumu ve proje maliyeti" gibi iki domainli basit bir istek
**iki ayri LLM turu** demekti. Uc domainli bir istek uc tur. Ustelik
agent'lar arasi handoff yolu, ayrica korunmasi gereken bir guvenlik
yuzeyiydi (alan allowlist'i + DLP + prompt injection riski).

### Yeni hal

```
kanal -> kimlik/ActorContext -> deterministik PackRouter -> SAPAgentRuntime
      -> ModelProvider -> guvenli Tool Registry
      -> Policy / Risk / Approval / DLP -> SAP backend
```

Domain ayrimi **kaybolmadi** - calisma zamani nesnesi olmaktan cikip veriye
donustu. Bir domainin gercekte tasidigi sey dorttur ve dordu de veridir:

| Eskiden | Simdi |
|---|---|
| `AgentSpec.mission` + ayri prompt | `DomainProfile.mission` -> tek prompt'ta birlesir |
| Agent'in sabit tool seti | `DomainProfile.packs` -> router'in actigi pack birlesimi |
| Agent basina iterasyon limiti | `DomainProfile.iteration_budget` -> acik domainlerin en genisi |
| Agent'in gorebilecegi kapsam | `visible_tool_names(domains, actor)` (degismedi) |

Cok domainli bir istekte yetkili pack'lerin **birlesimi** hesaplanir, tek
prompt kurulur, **tek** model dongusu calisir.

### Olculebilir sonuc

`tests/unit/test_domain_isolation.py::test_cok_domainli_istek_tek_model_cagrisi_yapar`
iki domainli bir istekte `provider.call_count == 1` oldugunu dogrular.

### Kod gecisi

```python
# Eski
from robotics_agent.compat_agent import SAPMultiAgent
agent = SAPMultiAgent(settings, actor=actor)

# Yeni
from certaops.runtime import SAPAgentRuntime
runtime = SAPAgentRuntime(settings, actor=actor)
```

`SAPMultiAgent` ve `SAPDomainAgent` calismaya devam eder; ikisi de
`SAPAgentRuntime`'a turer ve `DeprecationWarning` uretir. Eski imzadaki
`stream`, `use_prompt_cache`, `two_stage_routing`, `agent_spec`
parametreleri sessizce yutulur (artik saglayici adaptorune aittirler).

### `AgentTurn` alan degisiklikleri

| Eski | Yeni | Durum |
|---|---|---|
| `active_agents` | `active_domains` | eski ad **ozellik olarak korundu** |
| `agent_trace` | `domain_trace` | eski ad **ozellik olarak korundu** |
| - | `provider`, `model`, `model_calls`, `thinking_level` | yeni |

### Kaldirilan calisma yolu

`HandoffEnvelope` **runtime'da kullanilmiyor**. Sinif ve testleri duruyor
(baska bir baglamda tekrar gerekebilir) ama tek runtime'da agent'lar arasi
aktarim diye bir sey yok - tek konusma gecmisi var.

---

## 2. Saglayici-bagimsiz model katmani

Core runtime artik **hicbir SDK tipini gormez**. Anthropic'in `tool_use`
blogu, Gemini'nin `FunctionCall` parcasi - hepsi `certaops.providers.contracts`
icindeki notr tiplere cevrilir.

```
certaops/providers/
  contracts.py   ModelProvider, ModelRequest/Response/Message,
                 FunctionDeclaration/Call/Result, TokenUsage, hata siniflari
  gemini.py      Google Gemini (Developer API + Vertex)
  anthropic.py   Claude (opsiyonel bagimlilik)
  fake.py        testler icin senaryolanabilir saglayici
```

`ToolSpec.to_function_declaration()` saglayici-bagimsiz semayi uretir.
`to_anthropic()` geriye donuk uyumluluk icin duruyor ama **yeni kod
kullanmamalidir**.

### Anthropic'e ozgu mantik nereye gitti

`cache_control` (prompt cache) isaretleri artik yalniz
`certaops/providers/anthropic.py` icinde. Runtime'da izi yok.

---

## 3. Gemini 3.7 Flash

```bash
MODEL_PROVIDER=gemini
MODEL_NAME=gemini-3.7-flash
GEMINI_API_KEY=...
GEMINI_BACKEND=developer      # veya vertex
GEMINI_THINKING_LEVEL=low
GEMINI_STORE_INTERACTIONS=false
```

Vertex icin:

```bash
GEMINI_BACKEND=vertex
GOOGLE_CLOUD_PROJECT=...
GOOGLE_CLOUD_LOCATION=europe-west4
```

### Kritik guvenlik karari: otomatik fonksiyon yurutme KAPALI

`google-genai` SDK'si, Python fonksiyonlarini tool olarak alip modelin
cagrisini **kendisi** calistirabilir. Bu proje icin bu bir aciktir: SAP
handler'i dogrudan cagrilirsa RBAC, ABAC, risk skoru, insan onayi,
idempotency, DLP, audit ve butce katmanlarinin tamami atlanir.

Iki katmanli emniyet uygulanir:

1. SDK'ya **hicbir cagrilabilir Python nesnesi verilmez** - yalnizca
   `types.FunctionDeclaration` (saf sema).
2. Her istekte `AutomaticFunctionCallingConfig(disable=True,
   maximum_remote_calls=0)`.

Testler: `tests/unit/test_gemini_provider.py::test_otomatik_fonksiyon_yurutme_her_zaman_kapali`
ve `test_sdk_ye_cagrilabilir_nesne_gonderilmez`.

### Kaldirilan orneklem parametreleri

Gemini 3'te `temperature`, `top_p`, `top_k`, `candidate_count` kaldirildi.
Adapter bunlari **gondermez**; muhakeme butcesi `thinking_level` ile verilir:

- `low` - basit R0/R1 ve tek tool istekleri
- `medium` - cok adimli veya cok domainli istekler (runtime otomatik yukseltir)
- `high` - yalniz `GEMINI_ALLOW_HIGH_THINKING=true` ile

### Thought signature

Gemini 3, cok adimli function calling'de opak bir `thought_signature` tasir
ve geri gonderilmesini bekler. Bu deger `FunctionCall.provider_state` icinde
tasinir ve:

- denetim defterine **yazilmaz**,
- loglara **basilmaz** (`__repr__` gizler),
- tenant'lar arasinda **paylasilmaz**,
- konusma metnine **cevrilmez**.

### Interactions API notu

`google-genai` 2.18'de `client.interactions` mevcut ama tipsiz/preview
(`request: Any`). Dogrulanabilir ve kararli olan `client.models.generate_content`
kullanildi. `GEMINI_STORE_INTERACTIONS=false` istegi `http_options.extra_body`
ile iletilir; Developer API'de veri saklama **hesap ayarlarina da baglidir**.
Uretimde SAP verisi icin Vertex backend'i onerilir - uretim profili
`GEMINI_BACKEND=developer` icin blocker uretir.

---

## 4. `ANTHROPIC_API_KEY` artik zorunlu degil

`Settings.validate()` bu anahtari **istemiyor**. Yerine
`ModelSettings.validate()` secili saglayiciyi dogrular. `anthropic` paketi
opsiyonel bagimliliktir (`pip install .[anthropic]`).

---

## 5. `sap_list_agents` -> `sap_list_domains`

Eski tool "ayri calisan agent'lar" anlatiyordu; artik yaniltici. Yeni ad
domain yetenek gorunumu dondurur. Eski ad **kaldirilmadi**, ayni icerigi
`deprecated` isaretiyle dondurur.

`/agents` HTTP uc noktasi da korunuyor ama artik `architecture:
certaops-single-runtime` ve `domains` alani dondurur.

---

## 6. Paket adlari

Yeni kod `certaops` altinda:

- `certaops.providers` - saglayici katmani
- `certaops.runtime` - tek runtime + domain profilleri

Mevcut `robotics_agent` modulleri **oldugu yerde durur**. Tam namespace
tasimasi (SAP adaptorleri, policy, privacy, tools) ayri ve daha buyuk bir
istir; provider/runtime donusumuyle karistirmamak icin bilerek
ertelenmistir. `certaops/__init__.py` tembel `__getattr__` kullanir -
dairesel import olusmaz.

### Sonraki adim (bu surumun kapsaminda degil)

`robotics_agent.*` -> `certaops.*` tasimasi icin onerilen sira:

1. `contracts`, `privacy`, `risk` (bagimliligi az)
2. `core`, `cache`, `observability`
3. `adapters`, `sap`
4. `tools`, `channels`, `config`

Her adimda eski modul `from certaops.X import *` seklinde shim birakilmali
ve bir surum boyunca `DeprecationWarning` uretmelidir.
