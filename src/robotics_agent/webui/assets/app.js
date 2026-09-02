/* ==========================================================================
   CertaOps — Ray konsolu

   Üç kural:

   1) innerHTML YOK. SAP alanlarından gelen her metin createTextNode ile
      girer. CSP satır içi scripti zaten engellerdi; tek savunmaya güvenilmez.

   2) Dış bağımlılık YOK. `connect-src 'self'` altında bir operatör ekranı
      üçüncü tarafa istek atamaz.

   3) Hiçbir değer "olduğu gibi" basılmaz. Her alan tipine göre çizilir —
      bölüm 3'teki sınıflandırma tabloya, ızgaraya ve ölçülere kadar her
      yerde aynı kararı verir. `[object Object]` üretebilecek tek bir yol
      bırakılmadı.
   ========================================================================== */
'use strict';

/* ==========================================================================
   1. SÖZLÜK
   ========================================================================== */
var I18N = {
  tr: {
    'gate.sub': 'Konsola bağlanmak için erişim token\'ınızı girin.',
    'gate.token': 'Erişim token\'ı', 'gate.ph': 'Bearer token', 'gate.go': 'Bağlan',
    'gate.eye': 'Token\'ı göster/gizle',
    'gate.note': 'Token yalnızca bu sekmenin belleğinde tutulur, sekme kapanınca silinir ve sunucuya yalnızca Authorization başlığında gönderilir.',
    'gate.bad': 'Token geçersiz veya süresi dolmuş.',
    'gate.scope': 'Bu token konsolu açabilir ama gerekli kapsamlara sahip değil.',
    'gate.down': 'Servise ulaşılamıyor.',

    'mode.wait': 'BAĞLANIYOR', 'mode.sim': 'SİMÜLASYON', 'mode.live': 'CANLI SAP',
    'mode.dry': 'DRY-RUN KİLİDİ',

    'nav.ledger': 'defter', 'nav.tools': 'tool\'lar', 'nav.health': 'durum',
    'nav.telemetry': 'telemetri', 'nav.audit': 'denetim', 'nav.logs': 'log',
    'nav.mcp': 'mcp',

    'ui.lang': 'Dil', 'ui.theme': 'Tema', 'ui.out': 'Çıkış', 'ui.reload': 'Yenile',
    'ui.copy': 'Kopyala', 'ui.close': 'Kapat', 'ui.toEnd': 'En alta',
    'ui.copied': 'Panoya kopyalandı', 'ui.copyFail': 'Kopyalanamadı',
    'ui.loading': 'Yükleniyor…', 'ui.failed': 'Yüklenemedi',
    'ui.toolToggle': 'Bu tool\u2019un çalışma detayını aç/kapat',
    'ui.forbidden': 'Bu görünüm için yetkiniz yok.',
    'ui.yes': 'evet', 'ui.no': 'hayır', 'ui.unset': 'değerlendirilmedi',
    'ui.more': 'daha', 'ui.rows': 'satır', 'ui.cols': 'sütun', 'ui.full': 'Tam kayıt',
    'ui.raw': 'Ham JSON',
    'nav.settings': 'ayarlar',
    'cfg.title': 'Çalışma zamanı ayarları',
    'cfg.apply': 'Uygula',
    'cfg.saved': 'Kaydedildi',
    'cfg.restart': 'yeniden başlatmada',
    'cfg.overridden': 'değiştirilmiş',
    'cfg.consequential': 'sonuç doğuran',
    'cfg.consequentialNote': 'Gerçek SAP\u2019a yazma kapısını ya da bir koruma katmanını etkiler; ikinci bir kimliğin onayını ister.',
    'cfg.envPinned': 'ortamda sabit',
    'cfg.envPinnedNote': 'Bu ayar kabuk/container ortam değişkeniyle verilmiş. Dış ortam her zaman arayüzü yener; değiştirmek için deployment tanımını düzenleyin.',
    'cfg.noScope': 'yetki yok',
    'cfg.noScopeNote': 'Değiştirmek platform.config kapsamı gerektiriyor.',
    'cfg.readOnly': 'Salt okunur',
    'cfg.readOnlyNote': 'Ayarları görebilirsiniz ama değiştirmek için {scope} kapsamı gerekiyor.',
    'cfg.posture': 'Yapılandırma duruşu',
    'cfg.compliant': 'üretim engeli yok',
    'cfg.blockersPresent': 'açık üretim engeli var',
    'cfg.pending': 'Onay bekleyen değişiklikler',
    'cfg.requestedBy': 'öneren',
    'cfg.approve': 'Onayla',
    'cfg.cancel': 'Geri çek',
    'cfg.sec.sap': 'SAP çalışma kipi',
    'cfg.sec.onay': 'Onay ve risk',
    'cfg.sec.veri': 'Veri koruması',
    'cfg.sec.gozlem': 'Gözlem ve basım',
    'cfg.never': 'Buradan değiştirilemeyenler',
    'cfg.never1': 'Kimlik doğrulama kipi, principal dosyası ve OIDC ayarları \u2014 bir arayüz kendi kapısını açamaz.',
    'cfg.never2': 'SAP kimlik bilgileri, OAuth sırları ve model API anahtarları \u2014 sır yazan bir uç, sır sızdıran bir uçtur.',
    'cfg.never3': 'Egress allowlist ve TLS doğrulaması \u2014 nereye bağlanıldığı deployment kararı.',
    'cfg.never4': 'Log ve önizleme maskeleme anahtarları \u2014 koruma uzaktan kapatılamaz.',
    'ui.clipped': '{n} karakter gizlendi',
    'ui.bigJson': 'Kayıt büyük olduğu için renklendirme kapatıldı.', 'ui.days': 'gün', 'ui.none': 'kayıt yok',

    'intro.h': 'Defter boş.', 'intro.p': 'Bir komut verin. Her yürütme aşağıdaki raya işlenir; policy kapısında duran bir çağrı rayı kırar.',
    'entry.ph': 'komut…', 'entry.run': 'Çalıştır', 'entry.gates': 'Kapı şeridi',
    'entry.hint': 'Enter çalıştırır · Shift+Enter satır',

    'seed.1': 'MAT-1001 stok durumu nedir?',
    'seed.2': '4500000012 numaralı siparişin faturası kesildi mi?',
    'seed.3': 'MAT-1001 için tedarikçileri toplam maliyete göre karşılaştır',
    'seed.4': 'Bloke tedarikçi faturalarını ve nedenlerini listele',

    'post.h': 'Oturum duruşu',
    'post.mode': 'MOD', 'post.write': 'YAZMA', 'post.deny': 'POLICY REDDİ',
    'post.review': 'İNCELEME BEKLEYEN', 'post.chain': 'DENETİM ZİNCİRİ',
    'post.budget': 'TUR BÜTÇESİ', 'post.sap': 'SAP ÇAĞRISI', 'post.tools': 'TOOL',
    'post.model': 'MODEL', 'post.turns': 'TUR',
    'post.open': 'açık', 'post.locked': 'dry-run kilidi', 'post.cached': 'önbellek',
    'post.na': '—',

    'g.POL': 'POL', 'g.CCH': 'CCH', 'g.EXE': 'EXE', 'g.DLP': 'DLP', 'g.BUD': 'BÜT', 'g.AUD': 'AUD',
    'g.stopPol': '↑ ilk kapıda durdu · SAP\'a hiç gidilmedi',
    'g.stopDlp': '↑ veri politikası kapısında durdu',
    'g.stopExe': '↑ yürütmede durdu',
    'g.notStarted': '↑ çağrı hiç başlamadı',
    'g.hit': 'önbellek isabeti · SAP\'a gidilmedi',
    'g.trim': 'bütçe kırptı · tam kayıt evidence\'ta',
    'g.pass': 'altı kapıdan geçti',

    'v.ok': 'TAMAM', 'v.deny': 'REDDEDİLDİ', 'v.dataDeny': 'VERİ POLİTİKASI',
    'v.timeout': 'ZAMAN AŞIMI', 'v.saturated': 'DOYGUN', 'v.unsupported': 'DESTEKLENMİYOR',
    'v.param': 'PARAMETRE', 'v.sap': 'SAP HATASI', 'v.err': 'HATA',
    'v.sign': 'İMZA BEKLİYOR', 'v.sim': 'SİMÜLE', 'v.written': 'SAP\'A YAZILDI',
    'v.reconciled': 'MUTABAKAT', 'v.empty': 'SONUÇ YOK', 'v.review': 'İNCELEME',

    't.deny': 'Bu işlem policy tarafından durduruldu',
    't.dataDeny': 'İstenen alanlar veri politikası gereği döndürülemez',
    't.timeout': 'Tool sözleşme süresini aştı',
    't.timeoutWrite': 'Bu bir yazma tool\'u: zaman aşımı yazmanın YAPILMADIĞI anlamına gelmez. Tekrar göndermeyin; sap_reconcile_execution ile aynı idempotency_key üzerinden doğrulayın.',
    't.saturated': 'Çok fazla tool çağrısı asılı durumda; yeni çağrı başlatılmadı',
    't.unsupported': 'Bağlı SAP sistemi bu yeteneği sunmuyor',
    't.param': 'Tool geçersiz parametrelerle çağrıldı',
    't.sap': 'SAP işlemi reddetti',
    't.sign': 'Yazma için insan onayı gerekiyor',
    't.sim': 'Policy ve onay geçti, SAP\'a yazma simüle edildi',
    't.written': 'SAP\'a yazıldı ve okuma ile doğrulandı',
    't.empty': 'Eşleşen kayıt bulunamadı',
    't.review': 'İnsan incelemesi gerekiyor',
    't.reviewX': 'Bu turda sonucu kesin olmayan bir işlem var. SAP\'ta gerçekleşip gerçekleşmediğini doğrulamadan tekrarlamayın.',
    't.denials': 'policy reddi bu turda',
    't.direct': 'Model kullanılmadı',
    't.directX': 'Bu yanıt doğrudan SAP verisinden üretildi; hiçbir veri model sağlayıcısına gönderilmedi.',
    't.failed': 'İstek tamamlanamadı',
    't.noStruct': 'Sonuç yapısal olarak çözülemedi; kırpılmış metin gösteriliyor.',

    's.input': 'Girdi', 's.result': 'Sonuç', 's.fix': 'Ne yapmalı',
    's.scopes': 'Eksik kapsamlar', 's.findings': 'Bulgular', 's.next': 'Sonraki adımlar',
    's.warn': 'Uyarılar', 's.notes': 'Notlar', 's.evidence': 'Kanıt',
    's.schema': 'Beklenen şema', 's.approval': 'Onay', 's.diff': 'Değişiklik',
    's.artifacts': 'Üretilen dosyalar', 's.messages': 'Mesajlar',
    's.stages': 'Zincir', 's.gaps': 'Veri boşlukları', 's.assume': 'Varsayımlar',
    's.insights': 'Değerlendirme', 's.alerts': 'Uyarılar', 's.summary': 'Özet',

    'e.source': 'kaynak', 'e.read': 'okuma', 'e.age': 'yaş', 'e.rows': 'kayıt',
    'e.etag': 'etag', 'e.detail': 'detay', 'e.est': 'TAHMİN', 'e.trim': 'KIRPILDI',
    'e.estX': 'gerçek SAP verisi değil',
    'e.trimX': 'token bütçesine sığmadı; tam kayıt evidence store\'da',

    'tools.find': 'tool ara…', 'tools.all': 'tümü', 'tools.visible': 'görünür',
    'tools.scopes': 'kapsam', 'tools.impact': 'etki', 'tools.budget': 'bütçe',
    'tools.cache': 'önbellek', 'tools.approval': 'onay', 'tools.none': 'eşleşen tool yok',
    'tools.timeout': 'zaman aşımı',

    'health.note': 'Servis, model, SAP bağlantısı ve güvenlik duruşu.',
    'health.ready': 'Üretime hazır', 'health.blockers': 'Üretim engelleri',
    'health.chain': 'Denetim zinciri', 'health.valid': 'geçerli', 'health.broken': 'BOZUK',
    'health.disabled': 'Kapalı tool\'lar',

    'tel.note': 'Bu süreçteki token, tool ve policy sayaçları.',
    'audit.note': 'Son denetim kayıtları ve hash zinciri.', 'audit.count': 'kayıt',
    'logs.level': 'seviye', 'logs.follow': 'otomatik yenile',
    'logs.masked': 'maskeleme açık', 'logs.unmasked': 'MASKELEME KAPALI',

    'mcp.note': 'Yerel MCP stdio sunucusu, yetkileri ve istemci bağlantısı.',
    'mcp.test': 'Bağlantıyı test et', 'mcp.testing': 'Test ediliyor…',
    'mcp.copy': 'İstemci ayarını kopyala',
    'mcp.ready': 'MCP sunucusu hazır', 'mcp.missing': 'MCP paketi kurulu değil',
    'mcp.channel': 'Kanal ayrımı',
    'mcp.channelNote': 'Bu web arayüzü güvenli HTTP API üzerinden çalışır. MCP, harici istemciler için ayrı bir stdio kanalıdır.',
    'mcp.safe': 'Salt-okunur MCP duruşu', 'mcp.write': 'MCP yazma tool\'ları görünür',
    'mcp.safeNote': 'Bağlantı testi salt-okunur ve dry-run kilitleriyle çalışır; SAP tool çağrısı yapmaz.',
    'mcp.identity': 'MCP kimliği', 'mcp.sap': 'SAP hedefi', 'mcp.security': 'Güvenlik',
    'mcp.tools': 'Dışarı açılan tool\'lar', 'mcp.config': 'İstemci yapılandırması',
    'mcp.notes': 'Sınırlar', 'mcp.probe': 'Son bağlantı testi',
    'mcp.probeOk': 'Initialize + tools/list başarılı',
    'mcp.noSecrets': 'Bu yapılandırma parola, API anahtarı veya OAuth sırrı içermez.',
  },

  en: {
    'gate.sub': 'Enter your access token to connect to the console.',
    'gate.token': 'Access token', 'gate.ph': 'Bearer token', 'gate.go': 'Connect',
    'gate.eye': 'Show/hide token',
    'gate.note': 'The token is kept only in this tab\'s memory, cleared when the tab closes, and sent to the server only in the Authorization header.',
    'gate.bad': 'Token is invalid or expired.',
    'gate.scope': 'This token can open the console but lacks the required scopes.',
    'gate.down': 'Cannot reach the service.',

    'mode.wait': 'CONNECTING', 'mode.sim': 'SIMULATION', 'mode.live': 'LIVE SAP',
    'mode.dry': 'DRY-RUN LOCK',

    'nav.ledger': 'ledger', 'nav.tools': 'tools', 'nav.health': 'health',
    'nav.telemetry': 'telemetry', 'nav.audit': 'audit', 'nav.logs': 'logs',
    'nav.mcp': 'mcp',

    'ui.lang': 'Language', 'ui.theme': 'Theme', 'ui.out': 'Sign out', 'ui.reload': 'Reload',
    'ui.copy': 'Copy', 'ui.close': 'Close', 'ui.toEnd': 'To end',
    'ui.copied': 'Copied to clipboard', 'ui.copyFail': 'Copy failed',
    'ui.loading': 'Loading…', 'ui.failed': 'Failed to load',
    'ui.toolToggle': 'Show/hide this tool\u2019s execution detail',
    'ui.forbidden': 'You are not authorized for this view.',
    'ui.yes': 'yes', 'ui.no': 'no', 'ui.unset': 'not evaluated',
    'ui.more': 'more', 'ui.rows': 'rows', 'ui.cols': 'cols', 'ui.full': 'Full record',
    'ui.raw': 'Raw JSON',
    'nav.settings': 'settings',
    'cfg.title': 'Runtime settings',
    'cfg.apply': 'Apply',
    'cfg.saved': 'Saved',
    'cfg.restart': 'on restart',
    'cfg.overridden': 'overridden',
    'cfg.consequential': 'consequential',
    'cfg.consequentialNote': 'Affects the real SAP write gate or a protection layer; requires a second identity to approve.',
    'cfg.envPinned': 'pinned by env',
    'cfg.envPinnedNote': 'This setting comes from a shell/container environment variable. The environment always beats the UI; change the deployment definition instead.',
    'cfg.noScope': 'no permission',
    'cfg.noScopeNote': 'Changing this requires the platform.config scope.',
    'cfg.readOnly': 'Read-only',
    'cfg.readOnlyNote': 'You can view settings, but changing them requires the {scope} scope.',
    'cfg.posture': 'Configuration posture',
    'cfg.compliant': 'no production blockers',
    'cfg.blockersPresent': 'open production blockers',
    'cfg.pending': 'Changes awaiting approval',
    'cfg.requestedBy': 'requested by',
    'cfg.approve': 'Approve',
    'cfg.cancel': 'Withdraw',
    'cfg.sec.sap': 'SAP operating mode',
    'cfg.sec.onay': 'Approval and risk',
    'cfg.sec.veri': 'Data protection',
    'cfg.sec.gozlem': 'Observability and paging',
    'cfg.never': 'What cannot be changed here',
    'cfg.never1': 'Authentication mode, principals file and OIDC settings \u2014 an interface cannot open its own gate.',
    'cfg.never2': 'SAP credentials, OAuth secrets and model API keys \u2014 an endpoint that writes secrets is an endpoint that leaks them.',
    'cfg.never3': 'Egress allowlist and TLS verification \u2014 where the agent connects is a deployment decision.',
    'cfg.never4': 'Log and preview masking switches \u2014 protection cannot be turned off remotely.',
    'ui.clipped': '{n} characters hidden',
    'ui.bigJson': 'Syntax colouring is off because this record is large.', 'ui.days': 'd', 'ui.none': 'no entries',

    'intro.h': 'The ledger is empty.', 'intro.p': 'Issue a command. Every execution is written to the rail below; a call stopped at the policy gate breaks the rail.',
    'entry.ph': 'command…', 'entry.run': 'Run', 'entry.gates': 'Gate strip',
    'entry.hint': 'Enter runs · Shift+Enter newline',

    'seed.1': 'What is the stock position of MAT-1001?',
    'seed.2': 'Has purchase order 4500000012 been invoiced?',
    'seed.3': 'Compare vendors for MAT-1001 by total cost of ownership',
    'seed.4': 'List blocked supplier invoices and the reason for each',

    'post.h': 'Session posture',
    'post.mode': 'MODE', 'post.write': 'WRITES', 'post.deny': 'POLICY DENIALS',
    'post.review': 'AWAITING REVIEW', 'post.chain': 'AUDIT CHAIN',
    'post.budget': 'TURN BUDGET', 'post.sap': 'SAP CALLS', 'post.tools': 'TOOLS',
    'post.model': 'MODEL', 'post.turns': 'TURNS',
    'post.open': 'open', 'post.locked': 'dry-run lock', 'post.cached': 'cached',
    'post.na': '—',

    'g.POL': 'POL', 'g.CCH': 'CCH', 'g.EXE': 'EXE', 'g.DLP': 'DLP', 'g.BUD': 'BUD', 'g.AUD': 'AUD',
    'g.stopPol': '↑ stopped at the first gate · never reached SAP',
    'g.stopDlp': '↑ stopped at the data-policy gate',
    'g.stopExe': '↑ stopped during execution',
    'g.notStarted': '↑ the call never started',
    'g.hit': 'cache hit · never reached SAP',
    'g.trim': 'budget trimmed · full record in evidence',
    'g.pass': 'passed all six gates',

    'v.ok': 'OK', 'v.deny': 'DENIED', 'v.dataDeny': 'DATA POLICY',
    'v.timeout': 'TIMEOUT', 'v.saturated': 'SATURATED', 'v.unsupported': 'UNSUPPORTED',
    'v.param': 'PARAMETER', 'v.sap': 'SAP ERROR', 'v.err': 'ERROR',
    'v.sign': 'AWAITING SIGNATURE', 'v.sim': 'SIMULATED', 'v.written': 'WRITTEN TO SAP',
    'v.reconciled': 'RECONCILED', 'v.empty': 'NO RESULT', 'v.review': 'REVIEW',

    't.deny': 'This operation was stopped by policy',
    't.dataDeny': 'The requested fields cannot be returned under the data policy',
    't.timeout': 'The tool exceeded its contract timeout',
    't.timeoutWrite': 'This is a write tool: a timeout does NOT mean the write did not happen. Do not resubmit; verify with sap_reconcile_execution using the same idempotency_key.',
    't.saturated': 'Too many tool calls are still hanging; no new call was started',
    't.unsupported': 'The connected SAP system does not offer this capability',
    't.param': 'The tool was called with invalid parameters',
    't.sap': 'SAP rejected the operation',
    't.sign': 'Human approval is required before writing',
    't.sim': 'Policy and approval passed; the SAP write was simulated',
    't.written': 'Written to SAP and verified by read-back',
    't.empty': 'No matching records found',
    't.review': 'Human review required',
    't.reviewX': 'This turn contains an operation with an uncertain outcome. Do not repeat it before verifying whether it landed in SAP.',
    't.denials': 'policy denials this turn',
    't.direct': 'No model used',
    't.directX': 'This answer came straight from SAP data; nothing was sent to the model provider.',
    't.failed': 'The request could not be completed',
    't.noStruct': 'The result could not be parsed structurally; showing truncated text.',

    's.input': 'Input', 's.result': 'Result', 's.fix': 'What to do',
    's.scopes': 'Missing scopes', 's.findings': 'Findings', 's.next': 'Next steps',
    's.warn': 'Warnings', 's.notes': 'Notes', 's.evidence': 'Evidence',
    's.schema': 'Expected schema', 's.approval': 'Approval', 's.diff': 'Change',
    's.artifacts': 'Generated files', 's.messages': 'Messages',
    's.stages': 'Chain', 's.gaps': 'Data gaps', 's.assume': 'Assumptions',
    's.insights': 'Assessment', 's.alerts': 'Alerts', 's.summary': 'Summary',

    'e.source': 'source', 'e.read': 'read', 'e.age': 'age', 'e.rows': 'records',
    'e.etag': 'etag', 'e.detail': 'detail', 'e.est': 'ESTIMATED', 'e.trim': 'TRUNCATED',
    'e.estX': 'not real SAP data',
    'e.trimX': 'exceeded the token budget; full record in the evidence store',

    'tools.find': 'search tools…', 'tools.all': 'all', 'tools.visible': 'visible',
    'tools.scopes': 'scopes', 'tools.impact': 'impact', 'tools.budget': 'budget',
    'tools.cache': 'cache', 'tools.approval': 'approval', 'tools.none': 'no matching tools',
    'tools.timeout': 'timeout',

    'health.note': 'Service, model, SAP connection and security posture.',
    'health.ready': 'Production ready', 'health.blockers': 'Production blockers',
    'health.chain': 'Audit chain', 'health.valid': 'valid', 'health.broken': 'BROKEN',
    'health.disabled': 'Disabled tools',

    'tel.note': 'Token, tool and policy counters for this process.',
    'audit.note': 'Recent audit entries and the hash chain.', 'audit.count': 'entries',
    'logs.level': 'level', 'logs.follow': 'auto refresh',
    'logs.masked': 'masking on', 'logs.unmasked': 'MASKING OFF',

    'mcp.note': 'Local MCP stdio server, permissions and client connection.',
    'mcp.test': 'Test connection', 'mcp.testing': 'Testing…',
    'mcp.copy': 'Copy client config',
    'mcp.ready': 'MCP server is ready', 'mcp.missing': 'MCP package is not installed',
    'mcp.channel': 'Channel separation',
    'mcp.channelNote': 'This web UI uses the authenticated HTTP API. MCP is a separate stdio channel for external clients.',
    'mcp.safe': 'Read-only MCP posture', 'mcp.write': 'MCP write tools are exposed',
    'mcp.safeNote': 'The connection test forces read-only and dry-run locks and makes no SAP tool call.',
    'mcp.identity': 'MCP identity', 'mcp.sap': 'SAP target', 'mcp.security': 'Security',
    'mcp.tools': 'Exposed tools', 'mcp.config': 'Client configuration',
    'mcp.notes': 'Boundaries', 'mcp.probe': 'Latest connection test',
    'mcp.probeOk': 'Initialize + tools/list succeeded',
    'mcp.noSecrets': 'This configuration contains no password, API key or OAuth secret.',
  }
};

var LANG = 'tr';
function t(k) {
  var d = I18N[LANG] || I18N.tr;
  return d[k] !== undefined ? d[k] : (I18N.tr[k] !== undefined ? I18N.tr[k] : k);
}

/* Alan adı sözlüğü — bilinmeyen anahtar okunur hale getirilir, uydurulmaz. */
var FIELD = {
  tr: {
    next_step: 'Sonraki adım', approval_instruction: 'Onay talimatı',
    materials: 'Malzemeler', shortages: 'Eksikler', orders: 'Siparişler',
    candidates: 'Adaylar', insights: 'Değerlendirme', assumptions: 'Varsayımlar',
    wbs_elements: 'WBS kalemleri', chain: 'Belge zinciri', stages: 'Aşamalar',
    items: 'Kalemler', sources: 'Tedarik kaynakları', open_orders: 'Açık siparişler',
    service_manifest: 'Servis listesi', probe: 'Sonda sonucu', by_project: 'Projeye göre',
    timeline: 'Zaman çizelgesi', steps: 'Adımlar', invoices: 'Faturalar',
    results: 'Sonuçlar', entries: 'Kayıtlar', by_tool: 'Tool bazında',
    by_outcome: 'Sonuca göre', verification: 'Doğrulama', retention_policy: 'Saklama politikası',
    best_tco_vendor: 'En iyi TCO tedarikçisi', best_tco_vendor_name: 'En iyi TCO tedarikçi adı',
    savings_vs_worst: 'En kötüye göre kazanç', delay_cost_per_day: 'Günlük gecikme maliyeti',
    matched: 'Eşleşti', checked_fields: 'Kontrol edilen alanlar', tolerance: 'Tolerans',
    task_id: 'Görev', assigned_to: 'Atanan', due: 'Termin',
    contract_ok: 'Kontrat uygun', fields_checked: 'Kontrol edilen alan', missing: 'Eksik',
    started_at: 'Başlangıç', completed_at: 'Bitiş', host: 'Sunucu', auth: 'Kimlik doğrulama',
    principal_propagation: 'Principal propagation', sap_attribution: 'SAP atıfı',
    calls: 'Çağrı', errors: 'Hata', p95_ms: 'p95 (ms)', cache_hits: 'Önbellek isabeti',
    sap_calls: 'SAP çağrısı', dlp_findings: 'DLP bulgusu', tool_invocations: 'Tool çağrısı',
    policy_denials: 'Policy reddi', turns: 'Tur', input_tokens: 'Girdi token',
    output_tokens: 'Çıktı token', abandoned_tool_threads: 'Asılı tool thread',
    material_id: 'Malzeme', vendor_id: 'Tedarikçi', vendor_name: 'Tedarikçi adı',
    po_id: 'Sipariş', requisition_id: 'Talep', invoice_id: 'Fatura',
    document_id: 'Belge', draft_id: 'Taslak', wbs_element: 'WBS',
    description: 'Açıklama', plant: 'Tesis', currency: 'Para birimi',
    quantity: 'Miktar', unit: 'Birim', status: 'Durum', risk: 'Risk',
    unrestricted: 'Serbest stok', reserved: 'Rezerve', unreserved: 'Rezervesiz',
    quality_inspection: 'Kalite kontrol', blocked: 'Bloke', on_order: 'Siparişte',
    safety_stock: 'Emniyet stoğu', below_safety_stock: 'Emniyet altında',
    shortfall: 'Eksik', required: 'Gereken', delivered: 'Teslim', open_qty: 'Açık miktar',
    net_price: 'Birim fiyat', unit_price: 'Birim fiyat', price: 'Fiyat',
    base_cost: 'Temel maliyet', total_cost_of_ownership: 'Toplam sahip olma maliyeti',
    logistics_and_duty: 'Lojistik + gümrük', quality_cost: 'Kalite maliyeti',
    delay_risk_cost: 'Gecikme riski', financing_benefit: 'Finansman kazancı',
    total_value: 'Toplam tutar', open_value: 'Açık tutar', net_value: 'Net tutar',
    total_open_value: 'Toplam açık tutar', delayed_open_value: 'Geciken tutar',
    approval_threshold: 'Onay eşiği', item_count: 'Kalem', order_count: 'Sipariş',
    lead_time_days: 'Tedarik süresi', best_lead_time_days: 'En iyi tedarik süresi',
    planned_delivery_days: 'Planlı teslim', delay_days: 'Gecikme', overdue_days: 'Vadesi geçen',
    late_by_days: 'Geç kalma', horizon_days: 'Ufuk',
    requested_delivery: 'Talep edilen teslim', confirmed_delivery: 'Teyitli teslim',
    earliest_available: 'En erken', required_date: 'İhtiyaç tarihi', eta: 'Tahmini varış',
    as_of: 'Tarih', checked_on: 'Kontrol tarihi', read_at: 'Okuma zamanı',
    risk_flags: 'Risk işaretleri', warnings: 'Uyarılar', certifications: 'Sertifikalar',
    feasible: 'Uygulanabilir', meets_required_date: 'Termin tutuyor',
    on_time_delivery_pct: 'Zamanında teslim', quality_ppm: 'Kalite (ppm)',
    vendor_score: 'Tedarikçi skoru', tco_vs_price_delta_pct: 'TCO / fiyat farkı',
    incoterms: 'Teslim şekli', payment_terms: 'Ödeme koşulu', min_order_qty: 'Min. sipariş',
    completion_pct: 'Tamamlanma', plan: 'Plan', actual: 'Fiili', commitment: 'Taahhüt',
    remaining_budget: 'Kalan bütçe', eac: 'EAC', etc: 'ETC', cpi: 'CPI',
    forecast_variance: 'Tahmin sapması', forecast_variance_pct: 'Tahmin sapması',
    total_plan: 'Toplam plan', total_actual: 'Toplam fiili', total_commitment: 'Toplam taahhüt',
    total_eac: 'Toplam EAC', delayed_share_pct: 'Geciken pay',
    shortage_count: 'Eksik sayısı', blocked_count: 'Bloke sayısı', overdue_count: 'Geciken',
    invoice_count: 'Fatura sayısı', wbs_count: 'WBS sayısı', result_count: 'Sonuç',
    source_count: 'Kaynak sayısı', open_order_count: 'Açık sipariş',
    single_source: 'Tek kaynak', used_in_projects: 'Kullanıldığı projeler',
    material_group: 'Malzeme grubu', procurement_type: 'Tedarik tipi',
    mrp_controller: 'MRP sorumlusu', abc: 'ABC sınıfı', weight_kg: 'Ağırlık (kg)',
    price_source: 'Fiyat kaynağı', base_unit: 'Temel birim',
    written_to_sap: 'SAP\'a yazıldı', write_status: 'Yazma durumu', verified: 'Doğrulandı',
    idempotency_key: 'Idempotency anahtarı', approval_id: 'Onay kimliği',
    payload_sha256: 'Payload SHA-256', denial_code: 'Red kodu', sap_code: 'SAP kodu',
    risk_tier: 'Risk basamağı', timeout_s: 'Süre sınırı (sn)', retryable: 'Tekrar denenebilir',
    submittable: 'Gönderilebilir', requires_human_approval: 'İnsan onayı gerekli',
    business_object_id: 'İş nesnesi', safe_to_retry: 'Tekrar güvenli',
    conclusion: 'Sonuç', pending_count: 'Bekleyen', chain_complete: 'Zincir tam',
    resolved_type: 'Çözülen tip', workflow_found: 'İş akışı bulundu',
    total_steps: 'Toplam adım', completed_steps: 'Tamamlanan', pending_steps: 'Bekleyen',
    final_decision: 'Nihai karar', has_shortage: 'Eksik var',
    supply_total: 'Toplam arz', demand_total: 'Toplam talep', net_position: 'Net pozisyon',
    shortage_date: 'İlk eksik tarihi', max_shortage_qty: 'En büyük eksik',
    critical_item: 'Kritik kalem', critical_path_item: 'Kritik yol kalemi',
    backend: 'Backend', system_alias: 'Sistem', source_api: 'Kaynak servis',
    source_system: 'Kaynak sistem', version: 'Sürüm', alias: 'Alias', odata: 'OData servisi',
    purpose: 'Amaç', linked_by: 'Bağ alanı', predecessor: 'Önceki belge', item_no: 'Kalem no',
    stage: 'Aşama', type: 'Tip', date: 'Tarih', amount: 'Tutar', country: 'Ülke',
    class_name: 'Sınıf', characteristics: 'Karakteristikler', note: 'Not',
    po_count: 'Sipariş sayısı', delayed_value: 'Geciken tutar', open_order_count2: '',
    mrp_element: 'MRP öğesi', gap: 'Fark', available_qty: 'Mevcut', required_qty: 'Gereken',
    processor_name: 'İşleyen', requested_by: 'Talep eden', payment_block: 'Ödeme blokesi',
    block_count: 'Bloke sayısı', block_reasons: 'Bloke nedenleri', method_note: 'Yöntem notu',
    basis: 'Dayanak', not_found: 'Bulunamayan', recommendation: 'Öneri',
    interpretation: 'Yorum', suggestion: 'Öneri', hint: 'İpucu', caution: 'Dikkat',
    contract_gaps: 'Kontrat farkları', unsupported_capabilities: 'Desteklenmeyen yetenekler',
    backend_capabilities: 'Backend yetenekleri', preferred_order: 'Tercih sırası',
    service_manifest: 'Servis listesi', detail_hint: 'Detay ipucu', probe: 'Sonda',
    connection: 'Bağlantı', received_status: 'Alınan durum', sap_record: 'SAP kaydı',
    created_files: 'Üretilen dosyalar', source_references: 'Kaynak referansları',
  },
  en: {
    next_step: 'Next step', approval_instruction: 'Approval instruction',
    materials: 'Materials', shortages: 'Shortages', orders: 'Orders',
    candidates: 'Candidates', insights: 'Assessment', assumptions: 'Assumptions',
    wbs_elements: 'WBS elements', chain: 'Document chain', stages: 'Stages',
    items: 'Items', sources: 'Supply sources', open_orders: 'Open orders',
    service_manifest: 'Service manifest', probe: 'Probe result', by_project: 'By project',
    timeline: 'Timeline', steps: 'Steps', invoices: 'Invoices',
    results: 'Results', entries: 'Entries', by_tool: 'By tool',
    by_outcome: 'By outcome', verification: 'Verification', retention_policy: 'Retention policy',
    best_tco_vendor: 'Best TCO vendor', best_tco_vendor_name: 'Best TCO vendor name',
    savings_vs_worst: 'Savings vs worst', delay_cost_per_day: 'Delay cost per day',
    matched: 'Matched', checked_fields: 'Checked fields', tolerance: 'Tolerance',
    task_id: 'Task', assigned_to: 'Assigned to', due: 'Due',
    contract_ok: 'Contract ok', fields_checked: 'Fields checked', missing: 'Missing',
    started_at: 'Started', completed_at: 'Completed', host: 'Host', auth: 'Auth',
    principal_propagation: 'Principal propagation', sap_attribution: 'SAP attribution',
    calls: 'Calls', errors: 'Errors', p95_ms: 'p95 (ms)', cache_hits: 'Cache hits',
    sap_calls: 'SAP calls', dlp_findings: 'DLP findings', tool_invocations: 'Tool calls',
    policy_denials: 'Policy denials', turns: 'Turns', input_tokens: 'Input tokens',
    output_tokens: 'Output tokens', abandoned_tool_threads: 'Abandoned tool threads',
    material_id: 'Material', vendor_id: 'Vendor', vendor_name: 'Vendor name',
    po_id: 'Purchase order', requisition_id: 'Requisition', invoice_id: 'Invoice',
    document_id: 'Document', draft_id: 'Draft', wbs_element: 'WBS',
    description: 'Description', plant: 'Plant', currency: 'Currency',
    quantity: 'Quantity', unit: 'Unit', status: 'Status', risk: 'Risk',
    unrestricted: 'Unrestricted', reserved: 'Reserved', unreserved: 'Unreserved',
    quality_inspection: 'Quality inspection', blocked: 'Blocked', on_order: 'On order',
    safety_stock: 'Safety stock', below_safety_stock: 'Below safety stock',
    shortfall: 'Shortfall', required: 'Required', delivered: 'Delivered', open_qty: 'Open qty',
    net_price: 'Unit price', unit_price: 'Unit price', price: 'Price',
    base_cost: 'Base cost', total_cost_of_ownership: 'Total cost of ownership',
    logistics_and_duty: 'Logistics + duty', quality_cost: 'Quality cost',
    delay_risk_cost: 'Delay risk cost', financing_benefit: 'Financing benefit',
    total_value: 'Total value', open_value: 'Open value', net_value: 'Net value',
    total_open_value: 'Total open value', delayed_open_value: 'Delayed value',
    approval_threshold: 'Approval threshold', item_count: 'Items', order_count: 'Orders',
    lead_time_days: 'Lead time', best_lead_time_days: 'Best lead time',
    planned_delivery_days: 'Planned delivery', delay_days: 'Delay', overdue_days: 'Overdue',
    late_by_days: 'Late by', horizon_days: 'Horizon',
    requested_delivery: 'Requested delivery', confirmed_delivery: 'Confirmed delivery',
    earliest_available: 'Earliest', required_date: 'Required date', eta: 'ETA',
    as_of: 'As of', checked_on: 'Checked on', read_at: 'Read at',
    risk_flags: 'Risk flags', warnings: 'Warnings', certifications: 'Certifications',
    feasible: 'Feasible', meets_required_date: 'Meets required date',
    on_time_delivery_pct: 'On-time delivery', quality_ppm: 'Quality (ppm)',
    vendor_score: 'Vendor score', tco_vs_price_delta_pct: 'TCO vs price',
    incoterms: 'Incoterms', payment_terms: 'Payment terms', min_order_qty: 'Min order qty',
    completion_pct: 'Completion', plan: 'Plan', actual: 'Actual', commitment: 'Commitment',
    remaining_budget: 'Remaining budget', eac: 'EAC', etc: 'ETC', cpi: 'CPI',
    forecast_variance: 'Forecast variance', forecast_variance_pct: 'Forecast variance',
    total_plan: 'Total plan', total_actual: 'Total actual', total_commitment: 'Total commitment',
    total_eac: 'Total EAC', delayed_share_pct: 'Delayed share',
    shortage_count: 'Shortages', blocked_count: 'Blocked', overdue_count: 'Overdue',
    invoice_count: 'Invoices', wbs_count: 'WBS elements', result_count: 'Results',
    source_count: 'Sources', open_order_count: 'Open orders',
    single_source: 'Single source', used_in_projects: 'Used in projects',
    material_group: 'Material group', procurement_type: 'Procurement type',
    mrp_controller: 'MRP controller', abc: 'ABC class', weight_kg: 'Weight (kg)',
    price_source: 'Price source', base_unit: 'Base unit',
    written_to_sap: 'Written to SAP', write_status: 'Write status', verified: 'Verified',
    idempotency_key: 'Idempotency key', approval_id: 'Approval id',
    payload_sha256: 'Payload SHA-256', denial_code: 'Denial code', sap_code: 'SAP code',
    risk_tier: 'Risk tier', timeout_s: 'Timeout (s)', retryable: 'Retryable',
    submittable: 'Submittable', requires_human_approval: 'Human approval required',
    business_object_id: 'Business object', safe_to_retry: 'Safe to retry',
    conclusion: 'Conclusion', pending_count: 'Pending', chain_complete: 'Chain complete',
    resolved_type: 'Resolved type', workflow_found: 'Workflow found',
    total_steps: 'Total steps', completed_steps: 'Completed', pending_steps: 'Pending',
    final_decision: 'Final decision', has_shortage: 'Has shortage',
    supply_total: 'Total supply', demand_total: 'Total demand', net_position: 'Net position',
    shortage_date: 'First shortage', max_shortage_qty: 'Max shortage',
    critical_item: 'Critical item', critical_path_item: 'Critical path item',
    backend: 'Backend', system_alias: 'System', source_api: 'Source service',
    source_system: 'Source system', version: 'Version', alias: 'Alias', odata: 'OData service',
    purpose: 'Purpose', linked_by: 'Linked by', predecessor: 'Predecessor', item_no: 'Item no',
    stage: 'Stage', type: 'Type', date: 'Date', amount: 'Amount', country: 'Country',
    class_name: 'Class', characteristics: 'Characteristics', note: 'Note',
    po_count: 'PO count', delayed_value: 'Delayed value',
    mrp_element: 'MRP element', gap: 'Gap', available_qty: 'Available', required_qty: 'Required',
    processor_name: 'Processor', requested_by: 'Requested by', payment_block: 'Payment block',
    block_count: 'Block count', block_reasons: 'Block reasons', method_note: 'Method note',
    basis: 'Basis', not_found: 'Not found', recommendation: 'Recommendation',
    interpretation: 'Interpretation', suggestion: 'Suggestion', hint: 'Hint', caution: 'Caution',
    contract_gaps: 'Contract gaps', unsupported_capabilities: 'Unsupported capabilities',
    backend_capabilities: 'Backend capabilities', preferred_order: 'Preferred order',
    service_manifest: 'Service manifest', detail_hint: 'Detail hint', probe: 'Probe',
    connection: 'Connection', received_status: 'Received status', sap_record: 'SAP record',
    created_files: 'Created files', source_references: 'Source references',
  }
};

function label(key) {
  var table = FIELD[LANG] || FIELD.tr;
  if (table[key]) return table[key];
  if (FIELD.tr[key] && LANG === 'tr') return FIELD.tr[key];
  // Bilinmeyen anahtar: okunur hale getirilir, anlamı uydurulmaz.
  return String(key).replace(/_/g, ' ').replace(/^./, function (c) { return c.toUpperCase(); });
}

/* ==========================================================================
   2. DOM
   Metin daima textContent üzerinden girer.
   ========================================================================== */
function $(s) { return document.querySelector(s); }
function $$(s) { return Array.prototype.slice.call(document.querySelectorAll(s)); }
function el(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function add(parent) {
  for (var i = 1; i < arguments.length; i++) if (arguments[i]) parent.appendChild(arguments[i]);
  return parent;
}
function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); }
function frag() { return document.createDocumentFragment(); }

function toast(msg) {
  var box = $('#toasts'), n = el('div', 'toast', msg);
  box.appendChild(n);
  setTimeout(function () { if (n.parentNode) n.parentNode.removeChild(n); }, 2400);
}
function copy(text) {
  var ok = function () { toast(t('ui.copied')); }, no = function () { toast(t('ui.copyFail')); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(ok, no); return;
  }
  var ta = el('textarea'); ta.value = text; ta.className = 'sr-only';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy') ? ok() : no(); } catch (e) { no(); }
  document.body.removeChild(ta);
}

/* ==========================================================================
   3. DEĞER SINIFLANDIRMASI  —  render kararlarının tek kaynağı

   Buradaki iş, tool sonuçlarının GERÇEK şekillerinden çıkarıldı:
   `by_project` gibi anahtarı bilinmeyen sözlük haritaları, `risk_flags` gibi
   satır içi diziler, `meets_required_date` gibi üç durumlu boolean'lar,
   `error` gibi bazen metin bazen sözlük olan alanlar, `sap_record` gibi
   tamamen serbest gövdeler.
   ========================================================================== */
var RE = {
  money: /(^|_)(value|cost|price|amount|budget|savings|benefit|threshold|plan|actual|commitment|eac|etc|gross|revenue)($|_)/,
  percent: /(_pct|_percent|_percentage|_share)$/,
  days: /_days$/,
  qty: /(qty|quantity|^delivered$|^required$|^shortfall$|^unrestricted$|^reserved$|^unreserved$|^on_order$|^safety_stock$|^quality_inspection$|^blocked$|stock)/,
  count: /(_count$|^count$|^total_steps$|^completed_steps$|^pending_steps$|_ppm$)/,
  ratio: /(^cpi$|score$)/,
  date: /(_date$|^date$|^eta$|^as_of$|^checked_on$|^read_at$|_at$|^at$|_delivery$|^earliest_available$|^expires|^shortage_date$|^due$)/,
  ident: /(_id$|^id$|^etag$|_sha256$|_key$|^wbs_element$|correlation|^alias$|^odata$|^payload_sha256$)/,
  name: /(^name$|_name$|^description$|^purpose$|^title$)/,
  statusish: /(^status$|^risk$|^state$|^outcome$|^verdict$|_flags$|^feasible$|^blocked$|^warnings$)/
};

function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }

/**
 * Payload'in para birimi. Ust seviyede yoksa satirlardan cikarilir:
 * `compare_vendors` para birimini yalniz aday satirlarinda tasir, ama
 * `recommendation.savings_vs_worst` de o para biriminde bir tutardir.
 */
function guessCurrency(p) {
  if (!isObj(p)) return '';
  if (typeof p.currency === 'string' && p.currency) return p.currency;
  var keys = Object.keys(p);
  for (var i = 0; i < keys.length; i++) {
    var v = p[keys[i]];
    if (Array.isArray(v)) {
      for (var j = 0; j < v.length; j++) {
        if (isObj(v[j]) && typeof v[j].currency === 'string' && v[j].currency) return v[j].currency;
      }
    } else if (isObj(v) && typeof v.currency === 'string' && v.currency) {
      return v.currency;
    }
  }
  return '';
}
function isEmptyish(v) { return v === null || v === undefined || v === ''; }

/** Bir dizinin bütün elemanları düz sözlük mü? (tablo adayı) */
function allObjects(a) {
  return Array.isArray(a) && a.length > 0 && a.every(isObj);
}
/** Bütün elemanlar skaler mi? (rozet listesi adayı) */
function allScalars(a) {
  return Array.isArray(a) && a.every(function (x) { return x === null || typeof x !== 'object'; });
}
/** Sözlüğün bütün değerleri skaler mi? (tanım ızgarası) */
function objAllScalar(o) {
  return Object.keys(o).every(function (k) {
    return o[k] === null || typeof o[k] !== 'object';
  });
}
/** Sözlüğün bütün değerleri sözlük mü? (anahtarlı harita tablosu: by_project) */
function objAllObjects(o) {
  var keys = Object.keys(o);
  return keys.length > 0 && keys.every(function (k) { return isObj(o[k]); });
}
/** Sözlüğün bütün değerleri boolean mı? (yetenek listesi: backend_capabilities) */
function objAllBool(o) {
  var keys = Object.keys(o);
  return keys.length > 1 && keys.every(function (k) { return typeof o[k] === 'boolean'; });
}

/**
 * Bir alanın nasıl çizileceğine karar verir.
 * Önce DEĞERİN tipine, sonra anahtar adına bakar — anahtar adı tek başına
 * güvenilmez (`earliest_available` bir tarih olabilir de "stokta" olabilir de).
 */
function kindOf(key, v) {
  if (v === null || v === undefined) return 'null';
  if (typeof v === 'boolean') return 'bool';

  if (typeof v === 'number') {
    if (!isFinite(v)) return 'text';
    if (RE.percent.test(key)) return 'percent';
    if (RE.days.test(key)) return 'days';
    if (RE.money.test(key)) return 'money';
    if (RE.ratio.test(key)) return 'ratio';
    if (RE.count.test(key)) return 'count';
    if (RE.qty.test(key)) return 'qty';
    return Number.isInteger(v) ? 'count' : 'ratio';
  }

  if (typeof v === 'string') {
    if (v === '') return 'null';
    // Tarih ANCAK gerçekten ayrıştırılabiliyorsa tarihtir.
    if (RE.date.test(key) && /^\d{4}-\d{2}-\d{2}/.test(v)) return 'date';
    if (RE.ident.test(key)) return 'ident';
    if (v.length > 90) return 'longtext';
    return 'text';
  }

  if (Array.isArray(v)) {
    if (v.length === 0) return 'emptylist';
    if (allObjects(v)) return 'rows';
    if (allScalars(v)) return 'flags';
    return 'mixedlist';
  }

  if (isObj(v)) {
    if (Object.keys(v).length === 0) return 'null';
    if (objAllBool(v)) return 'caps';
    if (objAllObjects(v)) return 'map';
    if (objAllScalar(v)) return 'flat';
    return 'deep';
  }
  return 'text';
}

/* --- Sayı biçimlendirme --------------------------------------------------- */
function locale() { return LANG === 'tr' ? 'tr-TR' : 'en-US'; }

function num(v, min, max) {
  if (typeof v !== 'number' || !isFinite(v)) return String(v);
  try {
    return v.toLocaleString(locale(), {
      minimumFractionDigits: min === undefined ? 0 : min,
      maximumFractionDigits: max === undefined ? (Number.isInteger(v) ? 0 : 2) : max
    });
  } catch (e) { return String(v); }
}

/**
 * Bir değerin görünen metni. `ctx.currency` satırın kendi para birimidir;
 * yoksa payload'ınki kullanılır — bir tablo satırı kendi para birimini
 * taşıyorsa üstteki asla onun yerine geçmez.
 */
function textOf(key, v, ctx) {
  ctx = ctx || {};
  var kind = ctx.kind || kindOf(key, v);
  var cur = ctx.currency || '';

  switch (kind) {
    case 'null': return '—';
    case 'bool': return v ? t('ui.yes') : t('ui.no');
    case 'money': return cur ? num(v, 0, 2) + ' ' + cur : num(v, 0, 2);
    case 'percent': return LANG === 'tr' ? '%' + num(v, 0, 2) : num(v, 0, 2) + '%';
    case 'days': return num(v, 0, 0) + ' ' + t('ui.days');
    case 'ratio': return num(v, 0, 3);
    case 'count': return num(v, 0, 0);
    case 'qty': return ctx.unit ? num(v, 0, 3) + ' ' + ctx.unit : num(v, 0, 3);
    case 'date': return fmtDate(v);
    case 'ident': return String(v);
    case 'flags': return v.join(', ');
    case 'emptylist': return '—';
    default: return String(v);
  }
}

function fmtDate(v) {
  var d = new Date(v);
  if (isNaN(d.getTime())) return String(v);
  var hasTime = /T\d{2}:/.test(String(v));
  try {
    return hasTime
      ? d.toLocaleString(locale(), { year: 'numeric', month: '2-digit', day: '2-digit',
                                     hour: '2-digit', minute: '2-digit', hour12: false })
      : d.toLocaleDateString(locale(), { year: 'numeric', month: '2-digit', day: '2-digit' });
  } catch (e) { return String(v); }
}
function fmtClock(v) {
  var d = new Date(v);
  if (isNaN(d.getTime())) return String(v || '');
  try { return d.toLocaleTimeString(locale(), { hour12: false }); } catch (e) { return String(v); }
}
function fmtAge(s) {
  var n = Number(s);
  if (!isFinite(n)) return String(s);
  if (n < 60) return Math.round(n) + ' s';
  if (n < 3600) return Math.round(n / 60) + ' min';
  return (n / 3600).toFixed(1) + ' h';
}

/** Bir sayının anlamlı olup olmadığına göre renk sınıfı. */
function toneFor(key, v) {
  if (typeof v === 'boolean') {
    if (/^(blocked|below_safety_stock|single_source|needs_review|payment_block)$/.test(key)) {
      return v ? 'review' : '';
    }
    if (/^(verified|feasible|submittable|safe_to_retry|chain_complete|workflow_found|meets_required_date)$/.test(key)) {
      return v ? 'ok' : 'review';
    }
    if (/^(has_shortage|requires_human_approval)$/.test(key)) return v ? 'review' : 'ok';
    return '';
  }
  if (typeof v === 'number') {
    if (/^(shortage_count|blocked_count|overdue_count|delay_days|overdue_days|late_by_days|shortfall|block_count)$/.test(key)) {
      return v > 0 ? 'review' : '';
    }
    if (/(_variance_pct|_variance)$/.test(key)) return v > 0 ? 'review' : '';
  }
  if (typeof v === 'string') {
    if (/^(kritik|critical|tükendi|blocked|bloke|asim riski)/i.test(v)) return 'deny';
    if (/^(yuksek|yüksek|high|orta|medium|izlemede|kritik)/i.test(v)) return 'review';
    if (/^(dusuk|düşük|low|yeterli|ok|plan dahilinde|stokta)/i.test(v)) return 'ok';
  }
  return '';
}

/* ==========================================================================
   4. ÇİZİCİLER
   ========================================================================== */
var MAX_DEPTH = 3;
var TABLE_ROWS = 25;
var COL_ORNEK = 250;   // sutun puanlamasi icin ornek buyuklugu
var TABLE_COLS = 9;
var FLAG_MAX = 6;

/* Metin tavanları — bir alanın uzunluğu SAP tarafından belirlenir ve
   sınırsızdır: SAP hata gövdesi başarısız alanın değerini yankılar ve o
   yol sonuç bütçesinden geçmez. Tavansız `textContent` doğrudan yerleşim
   maliyetine dönüşür (ölçüm: 2 MB tek alan = 205 ms layout, 633 ms duvar
   saati). Kırpılan metnin tamamı her zaman bir tık uzakta kalır. */
var METIN_TAVANI = 4000;    // gövdedeki düzyazı bloğu
var HUCRE_TAVANI = 300;     // tablo/ızgara hücresi
var OZET_TAVANI = 90;       // istasyon başlığındaki tek satırlık özet
var TITLE_TAVANI = 2000;    // title özniteliği (tarayıcı zaten kırpar)

/** Uzun metni tavana indirir; kırpılmışsa ikinci değer kaç karakter
    gizlendiğidir. Kırpma asla sessiz değildir — çağıran bunu gösterir. */
function kirp(s, tavan) {
  s = String(s);
  return s.length <= tavan ? [s, 0] : [s.slice(0, tavan), s.length - tavan];
}

/** Düzyazı bloğu. Metin tavanı aşıyorsa kırpılır ve tamamı için bir
    düğme konur; değerin kendisi kaybolmaz, yalnız DOM'a girmez. */
function proseNode(text, baslik) {
  var tam = String(text);
  var kesit = kirp(tam, METIN_TAVANI);
  var p = el('p', 'gloss', kesit[0]);
  if (!kesit[1]) return p;

  var kutu = el('div');
  p.appendChild(el('span', 'trunc-mark', '…'));
  var bar = el('div', 'trunc');
  bar.appendChild(el('span', null,
    t('ui.clipped').replace('{n}', num(kesit[1]))));
  var btn = el('button', 'btn', t('ui.full'));
  btn.type = 'button';
  btn.addEventListener('click', function () {
    openSheet(baslik || t('ui.full'), tam);
  });
  bar.appendChild(btn);
  add(kutu, p, bar);
  return kutu;
}

/** Tek bir değeri, tipine uygun bir DOM düğümü olarak döndürür. */
function valueNode(key, v, ctx, depth) {
  ctx = ctx || {}; depth = depth || 0;
  var kind = kindOf(key, v);

  if (kind === 'rows')  return tableOf(v, ctx);
  if (kind === 'map')   return mapTableOf(v, ctx);
  if (kind === 'caps')  return capsOf(v);
  if (kind === 'flags') return flagsOf(v, key);
  if (kind === 'mixedlist') return listOf(v);
  if (kind === 'flat')  return depth < MAX_DEPTH ? kvOf(v, ctx, depth + 1) : jsonOf(v);
  if (kind === 'deep')  return depth < MAX_DEPTH ? objectOf(v, ctx, depth + 1) : jsonOf(v);

  var span = el('span', null, textOf(key, v, { kind: kind, currency: ctx.currency, unit: ctx.unit }));
  var tone = toneFor(key, v);
  if (tone) span.className = 'cell-' + tone;
  if (kind === 'null') span.className = 'cell-null';
  return span;
}

/** Anahtar/değer ızgarası — bütün değerleri skaler olan sözlükler için. */
function kvOf(obj, ctx, depth) {
  ctx = ctx || {}; depth = depth || 0;
  var dl = el('dl', 'kv');
  var cur = ctx.currency || obj.currency || '';
  Object.keys(obj).forEach(function (k) {
    var v = obj[k];
    if (isEmptyish(v) && v !== 0 && v !== false) return;
    dl.appendChild(el('dt', null, label(k)));
    var kind = kindOf(k, v);
    var dd = el('dd');
    if (kind === 'longtext' || kind === 'text') {
      var kes = kirp(v, METIN_TAVANI);
      if (String(v).length > 40) dd.className = 'prose';
      dd.textContent = kes[0] + (kes[1] ? ' …' : '');
      if (kes[1]) dd.setAttribute('title', t('ui.clipped').replace('{n}', num(kes[1])));
    } else if (kind === 'null') {
      dd.className = 'null'; dd.textContent = '—';
      dd.setAttribute('title', t('ui.unset'));
    } else {
      dd.appendChild(valueNode(k, v, { currency: cur, unit: obj.unit }, depth));
      var tone = toneFor(k, v);
      if (tone) dd.classList.add('cell-' + tone);
    }
    dl.appendChild(dd);
  });
  return dl;
}

/** Karma sözlük — skalerler ızgaraya, yapılar kendi bölümüne. */
function objectOf(obj, ctx, depth) {
  ctx = ctx || {}; depth = depth || 0;
  var box = el('div', 'sec');
  var scalars = {}, structured = [];
  var cur = ctx.currency || obj.currency || '';

  Object.keys(obj).forEach(function (k) {
    var v = obj[k];
    if (isEmptyish(v) && v !== 0 && v !== false) return;
    var kind = kindOf(k, v);
    if (kind === 'rows' || kind === 'map' || kind === 'caps' || kind === 'deep' ||
        kind === 'flat' || kind === 'mixedlist' ||
        (kind === 'flags' && v.length > FLAG_MAX)) {
      structured.push([k, v, kind]);
    } else {
      scalars[k] = v;
    }
  });

  if (Object.keys(scalars).length) box.appendChild(kvOf(scalars, { currency: cur }, depth));
  structured.forEach(function (row) {
    var sec = el('div', 'sec');
    sec.appendChild(el('p', 'lbl', label(row[0])));
    sec.appendChild(valueNode(row[0], row[1], { currency: cur }, depth));
    box.appendChild(sec);
  });
  return box;
}

/** Skaler dizi → rozet listesi. Uzunsa kırpılır, tamamı title'da. */
function flagsOf(arr, key) {
  var box = el('div', 'flags');
  var tone = /risk_flags|warnings|alerts|block_reasons|data_gaps|contract_gaps|not_found|unsupported/.test(key || '')
    ? 'flag-review' : '';
  arr.slice(0, FLAG_MAX).forEach(function (x) {
    var chip = el('span', 'flag' + (tone ? ' ' + tone : ''), x === null ? '—' : String(x));
    if (String(x).length > 28) chip.setAttribute('title', kirp(x, TITLE_TAVANI)[0]);
    box.appendChild(chip);
  });
  if (arr.length > FLAG_MAX) {
    var rest = el('span', 'flag', '+' + (arr.length - FLAG_MAX));
    rest.setAttribute('title', kirp(arr.join('\n'), TITLE_TAVANI)[0]);
    box.appendChild(rest);
  }
  return box;
}

/** str→bool haritası → yetenek listesi (✓ / üstü çizili). */
function capsOf(obj) {
  var box = el('div', 'caps');
  Object.keys(obj).forEach(function (k) {
    box.appendChild(el('span', 'cap ' + (obj[k] ? 'on' : 'off'), label(k)));
  });
  return box;
}

function listOf(arr) {
  var ul = el('ul', 'ul');
  arr.forEach(function (x) {
    var li = el('li');
    if (isObj(x) || Array.isArray(x)) li.appendChild(jsonOf(x));
    else li.textContent = x === null ? '—' : String(x);
    ul.appendChild(li);
  });
  return ul;
}

/* --- Tablo ---------------------------------------------------------------
   Sütun seçimi bir sıralama işidir, ilk N anahtar değil: `candidates` 23
   alan taşır, hepsini basmak tabloyu okunmaz yapar. Kimlik ve durum
   sütunları önce gelir; kalanlar satır açılarak görülür. */
function columnScore(key, values) {
  var nonNull = values.filter(function (v) { return !isEmptyish(v); });
  if (!nonNull.length) return -1000;                // tamamen boş sütun düşer

  var sample = nonNull[0];
  var kind = kindOf(key, sample);
  if (kind === 'deep' || kind === 'map' || kind === 'caps') return -500;

  var s = 0;
  if (RE.ident.test(key)) s += 100;
  if (RE.name.test(key)) s += 80;
  if (RE.statusish.test(key)) s += 60;
  if (kind === 'money') s += 50;
  if (kind === 'percent') s += 38;
  if (kind === 'qty') s += 36;
  if (kind === 'count') s += 30;
  if (kind === 'days') s += 28;
  if (kind === 'date') s += 24;
  if (kind === 'bool') s += 20;
  if (kind === 'ratio') s += 18;
  if (kind === 'rows' || kind === 'flat') s -= 60;
  if (kind === 'flags') s -= 10;
  if (kind === 'longtext') s -= 20;
  // Az doldurulmuş sütun daha az değerli.
  s += Math.round((nonNull.length / values.length) * 10);
  return s;
}

function tableOf(rows, ctx) {
  ctx = ctx || {};
  var keys = [];
  rows.forEach(function (r) {
    Object.keys(r).forEach(function (k) { if (keys.indexOf(k) === -1) keys.push(k); });
  });

  // Sutun puanlamasi icin BUTUN satirlari taramak gerekmez: 10 k satirlik
  // bir sonucta sutun basina 10 k'lik dizi kurmak olcumde 668 ms tutuyordu.
  // Bastan ve araliklarla alinan bir ornek ayni karari veriyor.
  var ornek = rows;
  if (rows.length > COL_ORNEK) {
    ornek = rows.slice(0, 50);
    var adim = Math.ceil(rows.length / (COL_ORNEK - 50));
    for (var oi = 50; oi < rows.length; oi += adim) ornek.push(rows[oi]);
  }
  var scored = keys.map(function (k, i) {
    return {
      key: k, i: i,
      score: columnScore(k, ornek.map(function (r) { return r[k]; }))
    };
  }).filter(function (c) { return c.score > -400; });

  scored.sort(function (a, b) { return b.score - a.score || a.i - b.i; });
  var show = scored.slice(0, TABLE_COLS).sort(function (a, b) { return a.i - b.i; })
                   .map(function (c) { return c.key; });
  var hiddenCols = scored.length - show.length;

  // Her sütunun hizası, o sütundaki ilk dolu değerin tipinden gelir.
  var kinds = {};
  show.forEach(function (k) {
    var first = null;
    for (var i = 0; i < rows.length; i++) {
      if (!isEmptyish(rows[i][k])) { first = rows[i][k]; break; }
    }
    kinds[k] = kindOf(k, first);
  });

  var box = el('div', 'tbl-box');
  var table = el('table', 'tbl');
  var thead = el('thead'), hr = el('tr');
  show.forEach(function (k) {
    var numeric = /^(money|percent|days|qty|count|ratio)$/.test(kinds[k]);
    hr.appendChild(el('th', numeric ? 'n' : null, label(k)));
  });
  thead.appendChild(hr);

  var tbody = el('tbody');
  rows.slice(0, TABLE_ROWS).forEach(function (row) {
    var tr = el('tr');
    var rowCur = row.currency || ctx.currency || '';
    show.forEach(function (k) {
      var v = row[k];
      var kind = kindOf(k, v);
      var numeric = /^(money|percent|days|qty|count|ratio)$/.test(kind);
      var td = el('td', numeric ? 'n' : null);

      if (kind === 'null') {
        td.className = 'cell-null'; td.textContent = '—';
        if (typeof row[k] !== 'string') td.setAttribute('title', t('ui.unset'));
      } else if (kind === 'flags') {
        td.appendChild(flagsOf(v, k));
      } else if (kind === 'rows' || kind === 'map' || kind === 'flat' ||
                 kind === 'deep' || kind === 'caps' || kind === 'mixedlist') {
        // Yapısal değer hücreye sığmaz: sayısını göster, tamamı satır
        // açıldığında görünür.
        var n = Array.isArray(v) ? v.length : Object.keys(v).length;
        td.appendChild(el('span', 'flag', n + '×'));
      } else {
        // `unit` kendi sütunu olarak gösteriliyorsa miktara tekrar eklenmez.
        var text = textOf(k, v, { kind: kind, currency: rowCur,
                                  unit: show.indexOf('unit') === -1 ? row.unit : '' });
        // Hucre `nowrap`: kirpilmamis metin, gorunmese bile tek satir
        // olarak sekillendiriliyor ve maliyeti yerlesime binıyor.
        var kes = kirp(text, HUCRE_TAVANI);
        td.textContent = kes[0] + (kes[1] ? '…' : '');
        var tone = toneFor(k, v);
        if (tone) td.classList.add('cell-' + tone);
        if (kind === 'longtext' || (kind === 'text' && text.length > 30)) {
          td.classList.add('t');
          td.setAttribute('title', kirp(text, TITLE_TAVANI)[0]);
        }
      }
      tr.appendChild(td);
    });

    // Satırın tamamı — gizli sütunlar dahil — panelde açılır.
    tr.addEventListener('click', function () {
      openSheet(ctx.title || t('s.result'), row, t('ui.full'));
    });
    tbody.appendChild(tr);
  });

  add(table, thead, tbody);
  box.appendChild(table);

  var bits = [];
  if (rows.length > TABLE_ROWS) bits.push('+' + (rows.length - TABLE_ROWS) + ' ' + t('ui.rows'));
  if (hiddenCols > 0) bits.push('+' + hiddenCols + ' ' + t('ui.cols'));
  if (bits.length) {
    var more = el('tr', 'more');
    var td = el('td', null, bits.join(' · '));
    td.setAttribute('colspan', String(show.length));
    more.appendChild(td);
    tbody.appendChild(more);
  }
  return box;
}

/** Anahtarı bilinmeyen sözlük haritası (by_project) → anahtar sütunlu tablo. */
function mapTableOf(obj, ctx) {
  var rows = Object.keys(obj).map(function (k) {
    var row = { '#': k };
    Object.keys(obj[k]).forEach(function (kk) { row[kk] = obj[k][kk]; });
    return row;
  });
  return tableOf(rows, ctx);
}

/* --- JSON (DOM ile renklendirme, innerHTML yok) ---
   Renklendirme her token icin bir DOM dugumu uretir. Buyuk bir payload'da
   bu yuz binlerce dugum demek ve tarayici kilitlenir; olculdu: 10 k satirlik
   bir sonucta 290 bin dugum / 10,6 saniye. Bu tavanin ustunde renklendirme
   birakilir ve tek bir textContent atamasi yapilir - okunabilirlik biraz
   duser, ama panel aninda acilir. */
var JSON_RENK_TAVANI = 40000;    // ustunde renklendirme birakilir
var JSON_METIN_TAVANI = 400000;  // ustunde metnin kendisi de kirpilir

function jsonOf(value) {
  var pre = el('pre', 'raw');
  var metin;
  try {
    metin = JSON.stringify(value, null, 2);
  } catch (e) {
    // Dairesel yapi vb. Yine de bir sey goster.
    pre.textContent = String(value);
    return pre;
  }
  if (metin === undefined) { pre.textContent = String(value); return pre; }

  if (metin.length > JSON_RENK_TAVANI) {
    var not = el('div', 'band band-mute');
    var kes = kirp(metin, JSON_METIN_TAVANI);
    not.appendChild(el('span', null, t('ui.bigJson')
      + (kes[1] ? ' ' + t('ui.clipped').replace('{n}', num(kes[1])) : '')));
    // Kopyala düğmesi tam kaydı taşır; DOM yalnız tavana kadarını taşır.
    var kutu = el('div');
    pre.textContent = kes[0] + (kes[1] ? '\n…' : '');
    add(kutu, not, pre);
    return kutu;
  }
  writeJson(pre, value, 0);
  return pre;
}
function writeJson(target, value, depth) {
  var pad = function (n) { return new Array(n + 1).join('  '); };
  var put = function (text, cls) {
    target.appendChild(cls ? el('span', cls, text) : document.createTextNode(text));
  };
  if (value === null) { put('null', 'j-x'); return; }
  if (typeof value === 'boolean') { put(String(value), 'j-b'); return; }
  if (typeof value === 'number') { put(String(value), 'j-n'); return; }
  if (typeof value === 'string') { put(JSON.stringify(value), 'j-s'); return; }
  if (Array.isArray(value)) {
    if (!value.length) { put('[]'); return; }
    put('[\n');
    value.forEach(function (item, i) {
      put(pad(depth + 1)); writeJson(target, item, depth + 1);
      put(i < value.length - 1 ? ',\n' : '\n');
    });
    put(pad(depth) + ']'); return;
  }
  var entries = Object.keys(value);
  if (!entries.length) { put('{}'); return; }
  put('{\n');
  entries.forEach(function (k, i) {
    put(pad(depth + 1)); put(JSON.stringify(k), 'j-k'); put(': ');
    writeJson(target, value[k], depth + 1);
    put(i < entries.length - 1 ? ',\n' : '\n');
  });
  put(pad(depth) + '}');
}

/* --- Yardımcı bloklar --- */
function band(tone, title, text) {
  var b = el('div', 'band band-' + tone), body = el('div');
  if (title) body.appendChild(el('b', null, title));
  if (text) body.appendChild(el('p', null, text));
  return add(b, body);
}
function section(labelText, node) {
  var s = el('div', 'sec');
  if (labelText) s.appendChild(el('p', 'lbl', labelText));
  s.appendChild(node);
  return s;
}
function bullets(items, mono) {
  var ul = el('ul', 'ul' + (mono ? ' ul-mono' : ''));
  items.forEach(function (x) {
    ul.appendChild(el('li', null, isObj(x) || Array.isArray(x) ? JSON.stringify(x) : String(x)));
  });
  return ul;
}
/** Bir metin/sözlük karışık hata alanını güvenle metne çevirir. */
function errorText(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'string') return v;
  if (isObj(v)) {
    // exc.as_dict() → {error, code, ...}. Anlamlı alanı seç, JSON basma.
    return String(v.error || v.message || v.detail || v.reason ||
                  Object.keys(v).map(function (k) { return k + ': ' + v[k]; }).join(' · '));
  }
  return String(v);
}

/* ==========================================================================
   5. DURUM VE API
   ========================================================================== */
var state = {
  token: '', config: null, session: null, busy: false, view: 'ledger',
  tools: null, toolByName: {}, toolFilter: 'all', toolQuery: '',
  mcp: null, mcpProbe: null,
  logTimer: null, abort: null, turns: 0,
  stat: { denials: 0, reviews: 0, sapCalls: 0, tokens: 0, budget: 0, cached: 0, model: '' }
};
var K_TOKEN = 'certaops.session.token', K_LANG = 'certaops.lang', K_THEME = 'certaops.theme';

function ss(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }
function ssSet(k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} }
function ssDel(k) { try { sessionStorage.removeItem(k); } catch (e) {} }
function ls(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

function ApiError(status, body) {
  this.status = status; this.body = body || {};
  var d = this.body.detail || {};
  this.code = d.code || this.body.code || '';
  this.message = errorText(d.error) || errorText(this.body.error) || ('HTTP ' + status);
  this.detail = d;
}
ApiError.prototype = Object.create(Error.prototype);

function api(path, opts) {
  opts = opts || {};
  var headers = { Accept: 'application/json' };
  if (state.token) headers.Authorization = 'Bearer ' + state.token;
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';

  return fetch(path, {
    method: opts.method || 'GET', headers: headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    signal: opts.signal, credentials: 'same-origin', cache: 'no-store'
  }).catch(function (err) {
    if (err && err.name === 'AbortError') throw err;
    throw new ApiError(0, { error: t('gate.down') });
  }).then(function (res) {
    return res.text().then(function (text) {
      var payload = null;
      if (text) { try { payload = JSON.parse(text); } catch (e) { payload = { error: text }; } }
      if (!res.ok) throw new ApiError(res.status, payload);
      return payload;
    });
  });
}

/* ==========================================================================
   6. KAPI ŞERİDİ  —  execute_tool boru hattı

   Bir çağrının nerede durduğu bütün hikâyedir. Aşamalar registry.py'deki
   gerçek sırayı izler: policy → cache → handler → DLP → bütçe → audit.
   ========================================================================== */
var GATES = ['POL', 'CCH', 'EXE', 'DLP', 'BUD', 'AUD'];
var POLICY_CODES = {
  UNKNOWN_TOOL: 1, AUTH_REQUIRED: 1, MISSING_SCOPE: 1, ORG_SCOPE: 1, WINDOW_CLOSED: 1,
  APPROVAL_REQUIRED: 1, APPROVAL_INVALID: 1, APPROVAL_CONSUMED: 1, ALREADY_CONSUMED: 1,
  APPROVAL_SCOPE_EXCEEDED: 1, DRY_RUN_LOCKED: 1, TOOL_DISABLED: 1, POLICY_DENIED: 1
};

function deriveGates(payload, isError) {
  var p = payload || {};
  var meta = isObj(p._meta) ? p._meta : {};
  var code = p.denial_code || '';
  var S = { pass: 'pass', skip: 'skip', stop: 'stop', hit: 'hit', trim: 'trim' };

  // Denetim defteri her yolda yazar: reddedilen bir çağrı da audit'e düşer.
  if (POLICY_CODES[code]) {
    return { s: [S.stop, S.skip, S.skip, S.skip, S.skip, S.pass], note: t('g.stopPol'), tone: 'stop' };
  }
  if (code === 'DATA_POLICY_DENIED') {
    return { s: [S.pass, S.skip, S.pass, S.stop, S.skip, S.pass], note: t('g.stopDlp'), tone: 'stop' };
  }
  if (code === 'TOOL_EXECUTOR_SATURATED') {
    return { s: [S.pass, S.skip, S.stop, S.skip, S.skip, S.pass], note: t('g.notStarted'), tone: 'stop' };
  }
  if (code === 'TOOL_TIMEOUT' || code === 'CAPABILITY_NOT_SUPPORTED' ||
      p.sap_code || p.expected_schema || (isError && p.error)) {
    return { s: [S.pass, S.skip, S.stop, S.skip, S.skip, S.pass], note: t('g.stopExe'), tone: 'stop' };
  }
  if (meta.cached) {
    return { s: [S.pass, S.hit, S.skip, S.skip, S.pass, S.pass], note: t('g.hit'), tone: 'hit' };
  }
  if (meta.truncated) {
    return { s: [S.pass, S.skip, S.pass, S.pass, S.trim, S.pass], note: t('g.trim'), tone: 'trim' };
  }
  return { s: [S.pass, S.skip, S.pass, S.pass, S.pass, S.pass], note: t('g.pass'), tone: 'pass' };
}

function gateStrip(g) {
  var row = el('div', 'gate-row');
  var gates = el('div', 'gates');
  GATES.forEach(function (name, i) {
    var cell = el('div', 'g');
    cell.setAttribute('data-s', g.s[i]);
    add(cell, el('i', 'g-bar'), el('span', 'g-l', t('g.' + name)));
    gates.appendChild(cell);
  });
  add(row, gates, el('span', 'g-note ' + g.tone, g.note));
  return row;
}

/* ==========================================================================
   7. SENARYO SINIFLANDIRMASI
   ========================================================================== */
function classify(p, isError) {
  var code = (p && p.denial_code) || '';
  var meta = (p && isObj(p._meta)) ? p._meta : {};
  var flags = [];
  if (meta.cached) flags.push({ txt: t('post.cached').toUpperCase(), cls: 'v-info' });
  if (meta.truncated) flags.push({ txt: t('e.trim'), cls: 'v-review' });
  if (p && p.evidence && p.evidence.estimated) flags.push({ txt: t('e.est'), cls: 'v-review' });
  if (p && p.needs_review) flags.push({ txt: t('v.review'), cls: 'v-deny' });

  var mk = function (kind, node, verdict, vcls, title) {
    return { kind: kind, node: node, verdict: verdict, vcls: vcls, title: title, flags: flags };
  };
  if (!p) return mk('opaque', '●', t('v.ok'), 'v-mute', '');

  if (code === 'DATA_POLICY_DENIED') return mk('dataDeny', '✕', t('v.dataDeny'), 'v-deny', t('t.dataDeny'));
  if (code === 'TOOL_TIMEOUT') return mk('timeout', '◷', t('v.timeout'), 'v-review', t('t.timeout'));
  if (code === 'TOOL_EXECUTOR_SATURATED') return mk('saturated', '◷', t('v.saturated'), 'v-review', t('t.saturated'));
  if (code === 'CAPABILITY_NOT_SUPPORTED') return mk('unsupported', '⊘', t('v.unsupported'), 'v-review', t('t.unsupported'));
  if (POLICY_CODES[code]) return mk('deny', '✕', t('v.deny'), 'v-deny', t('t.deny'));
  if (code) return mk('deny', '✕', t('v.deny'), 'v-deny', t('t.deny'));

  if (p.expected_schema) return mk('param', '✕', t('v.param'), 'v-deny', t('t.param'));
  if (p.sap_code) return mk('sap', '✕', t('v.sap'), 'v-deny', t('t.sap'));
  if (p.error || isError) return mk('err', '✕', t('v.err'), 'v-deny', '');

  if (p.requires_human_approval === true) return mk('sign', '▲', t('v.sign'), 'v-review', t('t.sign'));
  if (p.write_status === 'simulated') return mk('sim', '◐', t('v.sim'), 'v-info', t('t.sim'));
  if (p.written_to_sap === true) {
    var rec = p.write_status === 'reconciled';
    return mk('written', '●', rec ? t('v.reconciled') : t('v.written'), 'v-ok', t('t.written'));
  }
  if (p.needs_review) return mk('review', '▲', t('v.review'), 'v-review', t('t.review'));

  var emptyKeys = ['chain', 'results', 'materials', 'invoices', 'shortages', 'orders',
                   'candidates', 'entries', 'items', 'steps', 'wbs_elements', 'timeline'];
  var isEmpty = emptyKeys.some(function (k) { return Array.isArray(p[k]) && p[k].length === 0; });
  if (isEmpty) return mk('empty', '○', t('v.empty'), 'v-mute', t('t.empty'));

  return mk('ok', '●', t('v.ok'), 'v-ok', '');
}

/* Gövdede özel olarak işlenen alanlar — genel ızgarada TEKRARLANMAZ. */
var HANDLED = {
  error: 1, denial_code: 1, remediation: 1, missing_scopes: 1, expected_schema: 1,
  sap_code: 1, sap_messages: 1, warnings: 1, notes: 1, evidence: 1, _meta: 1,
  interpretation: 1, next_steps: 1, findings: 1, blocking: 1, blocking_findings: 1,
  approval_instruction: 1, approval_task: 1, needs_review: 1, messages: 1,
  tool: 1, diff: 1, stages: 1, risk_tier: 1, timeout_s: 1, retryable: 1,
  data_gaps: 1, insights: 1, alerts: 1, assumptions: 1, caution: 1,
  recommendation: 1, conclusion: 1, hint: 1, note: 1, method_note: 1, basis: 1,
  detail_hint: 1, suggestion: 1, next_step: 1, approval_threshold: 1
};

/** Karar vericinin ilk bakışta görmesi gereken sayılar. */
function pickFigures(p, info) {
  var out = [];
  var cur = guessCurrency(p);
  var push = function (k, v, tone) {
    if (out.length >= 5 || isEmptyish(v)) return;
    out.push({ k: k, v: v, tone: tone || '' });
  };
  var money = function (k, tone) {
    if (typeof p[k] === 'number') push(k, textOf(k, p[k], { kind: 'money', currency: cur }), tone);
  };
  var plain = function (k, tone) {
    if (p[k] !== undefined && p[k] !== null && typeof p[k] !== 'object') {
      push(k, textOf(k, p[k]), tone === undefined ? toneFor(k, p[k]) : tone);
    }
  };

  money('total_value', info.kind === 'sign' ? 'review' : '');
  money('approval_threshold');
  money('total_open_value'); money('delayed_open_value');
  plain('item_count'); plain('order_count'); plain('invoice_count'); plain('wbs_count');
  plain('shortage_count'); plain('blocked_count'); plain('overdue_count');
  plain('net_position'); plain('has_shortage'); plain('chain_complete');
  plain('write_status', p.written_to_sap ? 'ok' : '');
  plain('verified'); plain('submittable'); plain('result_count'); plain('pending_count');

  if (isObj(p.portfolio_summary)) {
    Object.keys(p.portfolio_summary).slice(0, 3).forEach(function (k) {
      push(k, textOf(k, p.portfolio_summary[k], { currency: cur }), toneFor(k, p.portfolio_summary[k]));
    });
  }
  return out;
}

function figureRow(figs) {
  var box = el('div', 'figs');
  figs.forEach(function (f) {
    var dl = el('dl', 'fig');
    add(dl, el('dt', null, label(f.k)), el('dd', f.tone, f.v));
    box.appendChild(dl);
  });
  return box;
}

/* ==========================================================================
   8. YÜRÜTME İSTASYONU
   ========================================================================== */
function stationHead(call, info, spec) {
  var head = el('button', 'ex-head');
  head.type = 'button';

  if (spec && spec.risk_tier) {
    var n = el('span', 'notch');
    n.setAttribute('data-t', spec.risk_tier);
    n.setAttribute('aria-hidden', 'true');
    for (var i = 0; i < 5; i++) n.appendChild(el('i'));
    head.appendChild(n);
    var tr = el('span', 'tier', spec.risk_tier);
    tr.setAttribute('data-t', spec.risk_tier);
    head.appendChild(tr);
  }
  head.appendChild(el('span', 'ex-tool', call.name));
  head.appendChild(el('span', 'ex-gist', gistOf(call, info)));
  head.appendChild(el('span', 'verdict ' + info.vcls, info.verdict));
  info.flags.forEach(function (f) { head.appendChild(el('span', 'verdict ' + f.cls, f.txt)); });
  head.appendChild(el('span', 'ex-fold'));
  return head;
}

/** Başlık satırındaki tek cümlelik özet. */
function gistOf(call, info) {
  var p = call.result_json;
  if (!p) return String(call.result_preview || '').replace(/\s+/g, ' ').slice(0, 80);
  if (p.error) return errorText(p.error).replace(/\s+/g, ' ').slice(0, 90);
  if (p.interpretation) return String(p.interpretation).replace(/\s+/g, ' ').slice(0, 90);
  if (p.conclusion) return String(p.conclusion).replace(/\s+/g, ' ').slice(0, 90);

  var bits = [];
  var meta = isObj(p._meta) ? p._meta : {};
  if (typeof p.total_value === 'number') {
    bits.push(textOf('total_value', p.total_value, { kind: 'money', currency: guessCurrency(p) }));
  }
  ['item_count', 'order_count', 'result_count', 'invoice_count', 'shortage_count',
   'wbs_count'].forEach(function (k) {
    if (typeof p[k] === 'number' && bits.length < 3) {
      bits.push(label(k).toLowerCase() + ' ' + num(p[k]));
    }
  });
  if (typeof meta.returned_count === 'number' && bits.length < 3) {
    bits.push((meta.total_count && meta.total_count !== meta.returned_count
      ? meta.returned_count + '/' + meta.total_count : String(meta.returned_count))
      + ' ' + t('e.rows'));
  }
  if (bits.length) return bits.join(' · ');
  var keys = Object.keys(p);
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i], v = p[k];
    if (HANDLED[k] || isEmptyish(v) || typeof v === 'object') continue;
    // Diğer bütün dönüşler kırpıyor; bu da kırpmalı. Kırpmadığı sürece
    // tek bir SAP alanı başlık satırına megabaytlarca metin koyabiliyordu.
    return kirp(label(k) + ': ' + textOf(k, v, { currency: p.currency }),
                OZET_TAVANI)[0];
  }
  return info.title || '';
}

function stationBody(call, info) {
  var p = call.result_json;
  var box = el('div', 'ex-body');

  /* Yapısal sonuç yoksa metne düş — asla ham nesne basma. */
  if (!p) {
    box.appendChild(band('mute', '', t('t.noStruct')));
    if (call.result_preview) {
      box.appendChild(section(t('s.result'), el('pre', 'raw', String(call.result_preview))));
    }
    if (isObj(call.arguments) && Object.keys(call.arguments).length) {
      box.appendChild(section(t('s.input'), kvOf(call.arguments)));
    }
    return box;
  }

  var cur = p.currency || '';

  /* 1 · Kapı şeridi */
  box.appendChild(gateStrip(deriveGates(p, call.is_error)));

  /* 2 · Başlık bandı */
  if (info.title) {
    var tone = info.kind === 'written' ? 'ok'
      : info.kind === 'sim' ? 'info'
      : /timeout|saturated|unsupported|sign|review/.test(info.kind) ? 'review'
      : info.kind === 'empty' ? 'mute' : 'deny';
    box.appendChild(band(tone, info.title, errorText(p.error)));
  } else if (p.error) {
    box.appendChild(band('deny', '', errorText(p.error)));
  }
  if (info.kind === 'timeout' && p.needs_review) {
    box.appendChild(band('deny', '', t('t.timeoutWrite')));
  }

  /* 3 · Red ayrıntısı — buradaki alanlar aşağıda tekrarlanmaz */
  var errFields = {};
  ['denial_code', 'sap_code', 'risk_tier', 'timeout_s', 'retryable'].forEach(function (k) {
    if (!isEmptyish(p[k])) errFields[k] = p[k];
  });
  if (Object.keys(errFields).length) box.appendChild(kvOf(errFields));

  if (Array.isArray(p.missing_scopes) && p.missing_scopes.length) {
    box.appendChild(section(t('s.scopes'), flagsOf(p.missing_scopes, 'missing_scopes')));
  }
  if (p.remediation) box.appendChild(section(t('s.fix'), proseNode(p.remediation, t('s.fix'))));
  if (p.approval_instruction) {
    box.appendChild(section(label('approval_instruction'),
      proseNode(p.approval_instruction, label('approval_instruction'))));
  }
  if (isObj(p.approval_task)) box.appendChild(section(label('approval_task'), kvOf(p.approval_task)));
  if (Array.isArray(p.sap_messages) && p.sap_messages.length) {
    box.appendChild(section('SAP', bullets(p.sap_messages, true)));
  }
  if (Array.isArray(p.messages) && p.messages.length) box.appendChild(bullets(p.messages));

  /* 4 · Tool'un kendi doğal dil çıktısı */
  // Yorum ve sonuç tool'un ana cümlesidir; etiketsiz durur.
  ['interpretation', 'conclusion'].forEach(function (k) {
    if (typeof p[k] === 'string' && p[k]) box.appendChild(proseNode(p[k], label(k)));
  });
  // Kalan açıklamalar etiketli: neyin ne olduğu tahmin edilmemeli.
  ['recommendation', 'suggestion', 'caution', 'hint', 'note', 'basis',
   'method_note', 'next_step', 'detail_hint'].forEach(function (k) {
    var v = p[k];
    if (typeof v === 'string' && v) box.appendChild(section(label(k), proseNode(v, label(k))));
    else if (isObj(v)) box.appendChild(section(label(k), kvOf(v, { currency: cur })));
  });

  /* 5 · Ölçüler */
  var figs = pickFigures(p, info);
  if (figs.length) box.appendChild(figureRow(figs));

  /* 6 · Zincir aşamaları */
  if (Array.isArray(p.stages) && p.stages.length) {
    box.appendChild(section(t('s.stages'), tableOf(p.stages, { currency: cur, title: call.name })));
  }

  /* 7 · Bulgular */
  var findings = [];
  if (Array.isArray(p.findings)) findings = findings.concat(p.findings);
  if (Array.isArray(p.blocking)) {
    findings = findings.concat(p.blocking.map(function (m) { return { message: m, blocking: true }; }));
  }
  if (Array.isArray(p.blocking_findings)) {
    findings = findings.concat(p.blocking_findings.map(function (m) {
      return isObj(m) ? m : { message: m, blocking: true };
    }));
  }
  if (findings.length) box.appendChild(section(t('s.findings'), findingList(findings)));

  /* 8 · Diff */
  if (isObj(p.diff)) box.appendChild(section(t('s.diff'), diffOf(p.diff, cur)));
  else if (allObjects(p.diff)) box.appendChild(section(t('s.diff'), tableOf(p.diff, { currency: cur })));

  /* 9 · Kalan bütün alanlar — tipine göre */
  var scalars = {}, blocks = [];
  Object.keys(p).forEach(function (k) {
    if (HANDLED[k]) return;
    var v = p[k];
    if (isEmptyish(v) && v !== 0 && v !== false) return;
    var kind = kindOf(k, v);
    if (kind === 'emptylist') return;
    if (figs.some(function (f) { return f.k === k; })) return;

    if (kind === 'rows' || kind === 'map' || kind === 'caps' || kind === 'deep' ||
        kind === 'flat' || kind === 'mixedlist' ||
        (kind === 'flags' && v.length > 3) || kind === 'longtext') {
      blocks.push([k, v, kind]);
    } else {
      scalars[k] = v;
    }
  });
  if (Object.keys(scalars).length) box.appendChild(kvOf(scalars, { currency: cur }));
  blocks.forEach(function (b) {
    var node = b[2] === 'longtext'
      ? proseNode(b[1], label(b[0]))
      : valueNode(b[0], b[1], { currency: cur, title: call.name }, 0);
    box.appendChild(section(label(b[0]), node));
  });

  /* 10 · Uyarılar, notlar, sonraki adımlar */
  ['data_gaps', 'alerts', 'insights', 'assumptions'].forEach(function (k) {
    var v = p[k];
    if (Array.isArray(v) && v.length) box.appendChild(section(label(k), bullets(v)));
    else if (isObj(v)) box.appendChild(section(label(k), kvOf(v, { currency: cur })));
  });
  if (Array.isArray(p.warnings)) {
    p.warnings.forEach(function (w) { box.appendChild(band('review', '', String(w))); });
  }
  if (Array.isArray(p.next_steps) && p.next_steps.length) {
    box.appendChild(section(t('s.next'), bullets(p.next_steps)));
  }
  if (Array.isArray(p.notes) && p.notes.length) box.appendChild(section(t('s.notes'), bullets(p.notes)));
  if (p.expected_schema) box.appendChild(section(t('s.schema'), jsonOf(p.expected_schema)));

  /* 11 · Kanıt */
  var ev = evidenceOf(p.evidence, p._meta);
  if (ev) box.appendChild(ev);

  /* 12 · Girdi + tam kayıt */
  if (isObj(call.arguments) && Object.keys(call.arguments).length) {
    box.appendChild(section(t('s.input'), kvOf(call.arguments)));
  }
  var foot = el('div', 'ex-foot');
  var full = el('button', 'btn', t('ui.raw')); full.type = 'button';
  full.addEventListener('click', function () { openSheet(call.name, p, t('ui.raw')); });
  var cp = el('button', 'btn', t('ui.copy')); cp.type = 'button';
  cp.addEventListener('click', function () { copy(JSON.stringify(p, null, 2)); });
  add(foot, full, cp);
  box.appendChild(foot);

  return box;
}

function findingList(items) {
  var box = el('div', 'finds');
  var norm = items.map(function (f) {
    if (typeof f === 'string') return { message: f, blocking: false };
    return {
      message: String(f.message || f.text || f.detail || JSON.stringify(f)),
      blocking: f.blocking === true,
      severity: String(f.severity || f.level || ''),
      field: f.field ? String(f.field) : ''
    };
  });
  norm.sort(function (a, b) { return (b.blocking ? 1 : 0) - (a.blocking ? 1 : 0); });
  norm.forEach(function (f) {
    var warn = !f.blocking && /warn|uyar/i.test(f.severity);
    var row = el('div', 'find-row' + (f.blocking ? ' find-block' : warn ? ' find-warn' : ''));
    add(row, el('b', null, f.blocking ? '✕' : warn ? '!' : '·'),
             el('span', null, f.field ? f.field + ': ' + f.message : f.message));
    box.appendChild(row);
  });
  return box;
}

/**
 * Yazma farkı. Değerler KENDİ alan adlarıyla biçimlendirilir: satırı
 * `once`/`sonra` diye yeniden adlandırmak `total_value`ı bir tutar olmaktan
 * çıkarıp para birimini düşürüyordu.
 */
function diffOf(diff, cur) {
  var hasBA = ('before' in diff) || ('after' in diff);
  if (!hasBA) return kvOf(diff, { currency: cur });
  var before = isObj(diff.before) ? diff.before : null;
  var after = isObj(diff.after) ? diff.after : null;
  var keys = [];
  [before, after].forEach(function (o) {
    if (o) Object.keys(o).forEach(function (k) { if (keys.indexOf(k) === -1) keys.push(k); });
  });
  if (!keys.length) return kvOf(diff, { currency: cur });

  var box = el('div', 'tbl-box');
  var table = el('table', 'tbl');
  var thead = el('thead'), hr = el('tr');
  add(hr, el('th', null, label('field') === 'Field' ? 'Field' : 'Alan'),
          el('th', 'n', LANG === 'tr' ? 'Önce' : 'Before'),
          el('th', 'n', LANG === 'tr' ? 'Sonra' : 'After'));
  thead.appendChild(hr);

  var tbody = el('tbody');
  keys.forEach(function (k) {
    var b = before && before[k] !== undefined ? before[k] : null;
    var a = after && after[k] !== undefined ? after[k] : null;
    var tr = el('tr');
    var cell = function (v) {
      var kind = kindOf(k, v);
      var numeric = /^(money|percent|days|qty|count|ratio)$/.test(kind);
      var td = el('td', numeric ? 'n' : null);
      if (kind === 'null') { td.className = 'cell-null'; td.textContent = '—'; }
      else td.textContent = textOf(k, v, { kind: kind, currency: cur });
      return td;
    };
    add(tr, el('td', null, label(k)), cell(b), cell(a));
    tbody.appendChild(tr);
  });
  add(table, thead, tbody);
  box.appendChild(table);
  return box;
}

/** Kanıt şeridi: veri nereden, ne zaman, hangi kalitede geldi. */
function evidenceOf(evidence, meta) {
  var ev = isObj(evidence) ? evidence : {};
  var m = isObj(meta) ? meta : {};
  if (!Object.keys(ev).length && !Object.keys(m).length) return null;

  var estimated = ev.estimated === true;
  var strip = el('div', 'evid' + (estimated ? ' evid-est' : ''));
  var pair = function (k, v) {
    if (isEmptyish(v)) return;
    var s = el('span');
    add(s, el('b', null, k + ' '), document.createTextNode(String(v)));
    strip.appendChild(s);
  };

  if (ev.source_system || ev.source_api) {
    pair(t('e.source'), [ev.source_system, ev.source_api].filter(Boolean).join(' / '));
  }
  if (ev.read_at) pair(t('e.read'), fmtDate(ev.read_at));
  // `record_count` ile `_meta.returned_count` ayni seyi soyluyorsa iki kez yazma.
  var metaRows = typeof m.returned_count === 'number';
  if (ev.record_count && !metaRows) pair(t('e.rows'), num(ev.record_count));
  if (ev.etag) pair(t('e.etag'), String(ev.etag).slice(0, 18));
  if (m.detail) pair(t('e.detail'), m.detail);
  if (typeof m.returned_count === 'number') {
    pair(t('e.rows'), m.total_count ? m.returned_count + ' / ' + m.total_count : m.returned_count);
  }
  if (m.cached) pair(t('e.age'), fmtAge(m.age_seconds) + ' · ' + t('post.cached'));
  if (estimated) {
    var f = Array.isArray(ev.estimated_fields) ? ev.estimated_fields.join(', ') : '';
    pair(t('e.est'), t('e.estX') + (f ? ' (' + f + ')' : ''));
  }
  if (m.truncated) pair(t('e.trim'), t('e.trimX') + (m.evidence_id ? ' · ' + m.evidence_id : ''));
  if (Array.isArray(ev.notes)) ev.notes.forEach(function (n) { strip.appendChild(el('span', null, String(n))); });

  return strip.childNodes.length ? strip : null;
}

/* ==========================================================================
   9. MARKDOWN (küçük, güvenli alt küme — HTML üretmez)
   ========================================================================== */
function markdown(text) {
  var root = el('div', 'prose');
  var lines = String(text).replace(/\r\n/g, '\n').split('\n');
  var i = 0, para = [];
  var flush = function () {
    if (!para.length) return;
    var p = el('p'); inline(p, para.join('\n')); root.appendChild(p); para = [];
  };
  while (i < lines.length) {
    var line = lines[i];
    var fence = line.match(/^\s*```(\w*)\s*$/);
    if (fence) {
      flush(); var buf = []; i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      var pre = el('pre'); pre.appendChild(el('code', null, buf.join('\n')));
      root.appendChild(pre); continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      flush();
      var cells = function (r) { return r.trim().replace(/^\||\|$/g, '').split('|').map(function (c) { return c.trim(); }); };
      var table = el('table'), thead = el('thead'), hr = el('tr');
      cells(line).forEach(function (c) { var th = el('th'); inline(th, c); hr.appendChild(th); });
      thead.appendChild(hr);
      var tbody = el('tbody'); i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        var tr = el('tr');
        cells(lines[i]).forEach(function (c) { var td = el('td'); inline(td, c); tr.appendChild(td); });
        tbody.appendChild(tr); i++;
      }
      add(table, thead, tbody);
      var wrap = el('div', 'tbl-box'); wrap.appendChild(table); root.appendChild(wrap); continue;
    }
    var h = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (h) { flush(); var node = el(h[1].length <= 3 ? 'h3' : 'h4'); inline(node, h[2]); root.appendChild(node); i++; continue; }
    if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) { flush(); root.appendChild(el('hr')); i++; continue; }
    var b = line.match(/^\s*[-*•]\s+(.*)$/), n2 = line.match(/^\s*(\d+)[.)]\s+(.*)$/);
    if (b || n2) {
      flush();
      var ordered = !!n2, list = el(ordered ? 'ol' : 'ul');
      while (i < lines.length) {
        var bb = lines[i].match(/^\s*[-*•]\s+(.*)$/), nn = lines[i].match(/^\s*(\d+)[.)]\s+(.*)$/);
        if (ordered && !nn) break;
        if (!ordered && !bb) break;
        var li = el('li'); inline(li, ordered ? nn[2] : bb[1]); list.appendChild(li); i++;
      }
      root.appendChild(list); continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    para.push(line); i++;
  }
  flush();
  return root;
}
function inline(parent, text) {
  var re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(__[^_]+__)|(\*[^*\n]+\*)/g, last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    var tok = m[0];
    if (tok.charAt(0) === '`') parent.appendChild(el('code', null, tok.slice(1, -1)));
    else if (tok.slice(0, 2) === '**' || tok.slice(0, 2) === '__') parent.appendChild(el('strong', null, tok.slice(2, -2)));
    else parent.appendChild(el('em', null, tok.slice(1, -1)));
    last = m.index + tok.length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

/* ==========================================================================
   10. DEFTER
   ========================================================================== */
function railEl() { return $('#rail'); }

function pushEvent(node) {
  var intro = $('#intro');
  if (intro) intro.remove();
  railEl().appendChild(node);
  toEnd();
}
function toEnd() {
  var s = $('#scroll');
  requestAnimationFrame(function () { s.scrollTop = s.scrollHeight; });
}

function station(kind, glyph, broken) {
  var ev = el('article', 'ev ev-' + kind);
  if (broken) ev.setAttribute('data-break', '1');
  var rail = el('div', 'ev-rail');
  rail.appendChild(el('div', 'node', glyph));
  var main = el('div', 'ev-main');
  add(ev, rail, main);
  return { ev: ev, main: main };
}

function renderCommand(text) {
  var st = station('cmd', '▸', false);
  var body = el('div', 'cmd-text', text);
  var meta = el('div', 'cmd-meta');
  var who = (state.config && state.config.actor && state.config.actor.subject) || '';
  meta.textContent = fmtClock(new Date().toISOString()) + (who ? ' · ' + who : '');
  add(st.main, body, meta);
  pushEvent(st.ev);
}

function renderThinking() {
  var st = station('ok', '◌', false);
  var line = el('div', 'think');
  add(line, el('i'), el('i'), el('i'), el('span', null, t('ui.loading')));
  st.main.appendChild(line);
  pushEvent(st.ev);
  return st.ev;
}

function renderTurn(turn) {
  var group = frag();
  var calls = Array.isArray(turn.tool_calls) ? turn.tool_calls : [];
  var showGates = $('#show-gates').checked;

  if (turn.needs_review) {
    var w = station('review', '▲', false);
    w.main.appendChild(band('review', t('t.review'), t('t.reviewX')));
    group.appendChild(w.ev);
  }

  // Tool yurutmeleri artik CEVABIN ALTINDA ve VARSAYILAN KAPALI durur.
  // Onceki surumde her tool bloğu cevabin ustunde acik geliyordu; okumak
  // isteyen kullanici once bes blok teknik ciktiyi gecmek zorundaydi.
  // Sinyal kaybolmasin diye chip'ler sonuca gore renklenir: reddedilen bir
  // tool kirmizi chip olarak gorunur, detayi istege bagli acilir.
  var details = el('div', 'tool-details');
  var chips = [];

  calls.forEach(function (call) {
    var p = isObj(call.result_json) ? call.result_json : null;
    var info = classify(p, call.is_error);
    var spec = state.toolByName[call.name] || null;
    var isDeny = /^(deny|dataDeny)$/.test(info.kind);
    var kind = isDeny ? 'deny'
      : /timeout|saturated|unsupported|sign|review/.test(info.kind) ? 'review'
      : /err|param|sap/.test(info.kind) ? 'deny'
      : info.kind === 'empty' ? 'warn' : 'ok';

    var st = station(kind, info.node, isDeny);
    var head = stationHead(call, info, spec);
    var body = stationBody(call, info);
    if (!showGates) {
      var gr = body.querySelector('.gate-row');
      if (gr) gr.hidden = true;
    }

    st.ev.setAttribute('data-open', '0');
    body.hidden = true;
    head.setAttribute('aria-expanded', 'false');

    head.addEventListener('click', function () {
      var now = st.ev.getAttribute('data-open') === '1';
      st.ev.setAttribute('data-open', now ? '0' : '1');
      body.hidden = now;
      head.setAttribute('aria-expanded', now ? 'false' : 'true');
    });

    add(st.main, head, body);
    st.ev.hidden = true;
    details.appendChild(st.ev);
    chips.push({ name: call.name, kind: kind, ev: st.ev, head: head, body: body });
  });

  if (turn.direct_answer) {
    var d = station('ok', '\u25c7', false);
    d.main.appendChild(band('info', t('t.direct'),
      t('t.directX') + (turn.direct_answer_reason ? ' (' + turn.direct_answer_reason + ')' : '')));
    group.appendChild(d.ev);
  }

  var ans = station('ok', '\u25c6', false);
  if (turn.reply) ans.main.appendChild(markdown(turn.reply));
  if (Array.isArray(turn.artifacts) && turn.artifacts.length) {
    ans.main.appendChild(section(t('s.artifacts'), bullets(turn.artifacts, true)));
  }
  ans.main.appendChild(turnMeta(turn, chips));
  if (chips.length) ans.main.appendChild(details);
  group.appendChild(ans.ev);

  return group;
}

function turnMeta(turn, chips) {
  var m = el('div', 'turn-meta'), bits = [];
  if (turn.model) bits.push(turn.provider ? turn.provider + ' · ' + turn.model : turn.model);
  if (turn.input_tokens || turn.output_tokens) {
    bits.push(num(turn.input_tokens || 0) + ' in / ' + num(turn.output_tokens || 0) + ' out');
  }
  if (turn.iterations) bits.push(turn.iterations + ' iter');
  if (typeof turn.model_calls === 'number') bits.push(turn.model_calls + ' model');
  if (turn.tool_call_count) bits.push(turn.tool_call_count + ' tool');
  if (Array.isArray(turn.active_packs) && turn.active_packs.length) bits.push(turn.active_packs.join(', '));
  bits.forEach(function (b, i) {
    if (i) m.appendChild(el('span', 'sep', '·'));
    m.appendChild(el('span', null, b));
  });
  // Calisan tool'lar: ad + sonuc rengi. Tiklayinca detayi acar/kapatir.
  if (Array.isArray(chips) && chips.length) {
    if (bits.length) m.appendChild(el('span', 'sep', '\u00b7'));
    var wrap = el('span', 'tool-chips');
    chips.forEach(function (chip) {
      var b = el('button', 'tool-chip', chip.name);
      b.type = 'button';
      b.setAttribute('data-k', chip.kind);
      b.setAttribute('aria-expanded', 'false');
      b.setAttribute('title', t('ui.toolToggle') || chip.name);
      b.addEventListener('click', function () {
        var show = chip.ev.hidden;
        chip.ev.hidden = !show;
        b.setAttribute('aria-expanded', show ? 'true' : 'false');
        b.classList.toggle('on', show);
        // Chip ile acilan bir tool'un govdesi de acilir; iki tiklama gerekmez.
        if (show && chip.ev.getAttribute('data-open') !== '1') {
          chip.ev.setAttribute('data-open', '1');
          chip.body.hidden = false;
          chip.head.setAttribute('aria-expanded', 'true');
        }
      });
      wrap.appendChild(b);
    });
    m.appendChild(wrap);
  }

  if (turn.correlation_id) {
    if (bits.length) m.appendChild(el('span', 'sep', '·'));
    var btn = el('button', 'link-btn', String(turn.correlation_id).slice(0, 14));
    btn.type = 'button';
    btn.setAttribute('title', String(turn.correlation_id));
    btn.addEventListener('click', function () { copy(String(turn.correlation_id)); });
    m.appendChild(btn);
  }
  return m;
}

function renderFailure(err, cancelled) {
  var st = station('deny', '✕', true);
  if (cancelled) {
    st.main.appendChild(band('mute', '', t('ui.close')));
    return st.ev;
  }
  st.main.appendChild(band('deny', t('t.failed'), err.message || t('ui.failed')));
  if (err.detail && err.detail.reply) st.main.appendChild(markdown(err.detail.reply));
  var bits = [];
  if (err.status) bits.push('HTTP ' + err.status);
  if (err.code) bits.push(err.code);
  if (err.detail && err.detail.correlation_id) bits.push(String(err.detail.correlation_id).slice(0, 14));
  if (bits.length) {
    var m = el('div', 'turn-meta');
    bits.forEach(function (b, i) {
      if (i) m.appendChild(el('span', 'sep', '·'));
      m.appendChild(el('span', null, b));
    });
    st.main.appendChild(m);
  }
  if (err.status === 401) {
    ssDel(K_TOKEN); state.token = '';
    setTimeout(function () { showGate(t('gate.bad')); }, 800);
  }
  return st.ev;
}

/* --- Gönderme --- */
function send() {
  var input = $('#cmd'), msg = input.value.trim();
  if (!msg || state.busy) return;
  input.value = ''; grow(input);
  renderCommand(msg);

  state.busy = true;
  $('#run').disabled = true;
  var ph = renderThinking();
  state.abort = new AbortController();

  api('/chat', {
    method: 'POST', signal: state.abort.signal,
    body: { message: msg, session_id: state.session, include_tool_calls: true }
  }).then(function (turn) {
    state.session = turn.session_id || state.session;
    updateSession();
    accrue(turn);
    ph.replaceWith(renderTurn(turn));
    announce(turn);
    renderPosture();
  }).catch(function (err) {
    if (err && err.name === 'AbortError') ph.replaceWith(renderFailure(err, true));
    else ph.replaceWith(renderFailure(err, false));
  }).then(function () {
    state.busy = false; state.abort = null;
    $('#run').disabled = false;
    toEnd(); input.focus();
  });
}

function accrue(turn) {
  var s = state.stat;
  state.turns += 1;
  s.denials += turn.policy_denials || 0;
  if (turn.needs_review) s.reviews += 1;
  s.tokens += (turn.input_tokens || 0) + (turn.output_tokens || 0);
  if (turn.model) s.model = turn.model;
  (turn.tool_calls || []).forEach(function (c) {
    var p = c.result_json;
    if (p && isObj(p._meta) && p._meta.cached) s.cached += 1;
  });
}

function announce(turn) {
  var bits = [];
  if (turn.needs_review) bits.push(t('t.review'));
  if (turn.policy_denials) bits.push(turn.policy_denials + ' ' + t('t.denials'));
  bits.push(String(turn.reply || '').slice(0, 200));
  $('#live').textContent = bits.join('. ');
}

function updateSession() {
  var chip = $('#sess');
  if (!state.session) { chip.hidden = true; return; }
  chip.hidden = false;
  chip.textContent = 'sess ' + String(state.session).slice(0, 8);
  chip.setAttribute('title', String(state.session));
}

/* --- Duruş paneli --- */
function renderPosture() {
  var box = $('#posture-body');
  clear(box);
  var cfg = state.config || {}, s = state.stat;
  var row = function (k, v, cls) {
    var dl = el('dl', 'pst');
    add(dl, el('dt', null, k), el('dd', cls || '', v));
    box.appendChild(dl);
    return dl;
  };
  row(t('post.mode'), cfg.mode === 'live' ? t('mode.live') : t('mode.sim'),
      cfg.mode === 'live' ? 'ok' : 'sim');
  row(t('post.write'), cfg.dry_run ? t('post.locked') : t('post.open'),
      cfg.dry_run ? 'review' : 'ok');
  row(t('post.deny'), num(s.denials), s.denials ? 'deny' : 'mute');
  row(t('post.review'), num(s.reviews), s.reviews ? 'review' : 'mute');
  row(t('post.turns'), num(state.turns), 'mute');
  if (s.cached) row(t('post.cached'), num(s.cached), 'ok');
  if (s.tokens) {
    var dl = row(t('post.budget'), num(s.tokens), '');
    var meter = el('div', 'meter');
    var pct = Math.min(100, Math.round(s.tokens / 40000 * 100));
    var bar = el('i'); bar.style.width = pct + '%';
    if (pct > 90) bar.className = 'over';
    meter.appendChild(bar); dl.appendChild(meter);
  }
  if (s.model) row(t('post.model'), s.model, 'mute');
  if (state.tools) row(t('post.tools'), state.tools.visible_to_actor + ' / ' + state.tools.registered, 'mute');
}

/* ==========================================================================
   11. PANELLER
   ========================================================================== */
var RISK_TONE = { R0: 'tag-ok', R1: 'tag-info', R2: 'tag-review', R3: 'tag-deny', R4: 'tag-deny' };
var RISK_WORD = {
  tr: { R0: 'okuma', R1: 'hesap', R2: 'taslak', R3: 'yazma', R4: 'toplu yazma' },
  en: { R0: 'read', R1: 'compute', R2: 'draft', R3: 'write', R4: 'bulk write' }
};
function riskWord(tier) {
  var tbl = RISK_WORD[LANG] || RISK_WORD.tr;
  return tbl[tier] ? tier + ' ' + tbl[tier] : String(tier || '');
}
function notch(tier) {
  var n = el('span', 'notch');
  n.setAttribute('data-t', tier); n.setAttribute('aria-hidden', 'true');
  for (var i = 0; i < 5; i++) n.appendChild(el('i'));
  return n;
}

function loadView(view) {
  var body = $('#' + view + '-body');
  if (!body) return;
  clear(body);
  body.appendChild(el('p', 'empty', t('ui.loading')));

  var url = {
    tools: '/tools', health: '/health', telemetry: '/telemetry',
    audit: '/audit/recent?limit=' + ($('#audit-limit') ? $('#audit-limit').value : 60),
    logs: '/logs?limit=200&level=' + encodeURIComponent($('#log-level') ? $('#log-level').value : 'INFO'),
    mcp: '/ui/mcp',
    settings: '/ui/settings?lang=' + encodeURIComponent(LANG)
  }[view];

  api(url).then(function (data) {
    clear(body);
    ({ tools: viewTools, health: viewHealth, telemetry: viewTelemetry,
       audit: viewAudit, logs: viewLogs, mcp: viewMcp,
       settings: viewSettings })[view](data, body);
  }).catch(function (err) {
    clear(body);
    body.appendChild(band('deny', t('ui.failed'),
      err.status === 403 ? t('ui.forbidden') : (err.message || t('ui.failed'))));
  });
}

function viewTools(data, body) {
  state.tools = data;
  state.toolByName = {};
  (data.tools || []).forEach(function (s) { state.toolByName[s.tool] = s; });

  var domains = [];
  (data.tools || []).forEach(function (s) {
    if (s.domain && domains.indexOf(s.domain) === -1) domains.push(s.domain);
  });
  domains.sort();

  var chips = $('#tool-chips');
  clear(chips);
  var mk = function (val, text) {
    var c = el('button', 'chip' + (state.toolFilter === val ? ' on' : ''), text);
    c.type = 'button';
    c.addEventListener('click', function () { state.toolFilter = val; viewTools(state.tools, body); });
    return c;
  };
  chips.appendChild(mk('all', t('tools.all')));
  domains.forEach(function (d) { chips.appendChild(mk(d, d)); });

  var q = state.toolQuery.trim().toLowerCase();
  var list = (data.tools || []).filter(function (s) {
    if (state.toolFilter !== 'all' && s.domain !== state.toolFilter) return false;
    if (!q) return true;
    return (s.tool + ' ' + s.domain + ' ' + (s.required_scopes || []).join(' ')).toLowerCase().indexOf(q) !== -1;
  });

  clear(body);
  body.appendChild(el('p', 'pane-note',
    data.visible_to_actor + ' / ' + data.registered + ' ' + t('tools.visible')));
  if (!list.length) { body.appendChild(el('p', 'empty', t('tools.none'))); return; }

  list.forEach(function (s) {
    var card = el('div', 'card');
    var top = el('div', 'card-top');
    add(top, notch(s.risk_tier), el('span', 'card-name', s.tool),
        el('span', 'tag ' + (RISK_TONE[s.risk_tier] || 'tag'), riskWord(s.risk_tier)),
        el('span', 'tag', s.domain));
    if (s.approval_policy && s.approval_policy !== 'none') {
      top.appendChild(el('span', 'tag tag-review', t('tools.approval') + ' · ' + s.approval_policy));
    }
    if (s.idempotent) top.appendChild(el('span', 'tag tag-info', 'idempotent'));
    if (s.cache_policy && s.cache_policy.ttl_seconds) {
      top.appendChild(el('span', 'tag tag-info', t('tools.cache') + ' ' + s.cache_policy.ttl_seconds + 's'));
    }
    if (s.max_data_class) top.appendChild(el('span', 'tag', s.max_data_class));
    card.appendChild(top);

    var facts = {};
    if ((s.required_scopes || []).length) facts[t('tools.scopes')] = s.required_scopes.join(', ');
    if (s.impact_profile) {
      facts[t('tools.impact')] = [s.impact_profile.mutation, s.impact_profile.reversible]
        .filter(Boolean).join(' / ');
    }
    if (s.performance_budget) {
      var b = s.performance_budget;
      facts[t('tools.budget')] = 'p95 ' + b.p95_ms + 'ms · ' + b.max_sap_calls + ' SAP · ' + b.max_result_tokens + ' tok';
    }
    facts[t('tools.timeout')] = s.timeout_s + ' s';
    facts.version = s.version;
    card.appendChild(kvOf(facts));
    body.appendChild(card);
  });
}

function viewHealth(data, body) {
  var ready = data.production_ready === true;
  body.appendChild(band(ready ? 'ok' : 'review',
    t('health.ready') + ': ' + (ready ? t('ui.yes') : t('ui.no')), ''));

  if (Array.isArray(data.warnings) && data.warnings.length) {
    body.appendChild(section(t('health.blockers'), bullets(data.warnings)));
  }
  var chain = isObj(data.audit_head) ? data.audit_head : {};
  var valid = chain.valid === true;
  body.appendChild(band(valid ? 'ok' : 'deny',
    t('health.chain') + ': ' + (valid ? t('health.valid') : t('health.broken')),
    chain.checked !== undefined ? num(chain.checked) + ' ' + t('e.rows') : ''));

  var top = {};
  ['status', 'mode', 'app_env', 'auth_mode', 'session_backend', 'approval_gateway',
   'dry_run', 'registered_tools', 'domains', 'sap_attribution', 'architecture',
   'runtime_scope', 'runtime_count'].forEach(function (k) {
    if (data[k] !== undefined) top[k] = data[k];
  });
  var card = el('div', 'card');
  card.appendChild(el('p', 'lbl', 'runtime'));
  card.appendChild(kvOf(top));
  body.appendChild(card);

  Object.keys(data).forEach(function (k) {
    if (k === 'audit_head' || k === 'warnings' || top[k] !== undefined) return;
    var v = data[k];
    if (isEmptyish(v)) return;
    var kind = kindOf(k, v);
    if (kind === 'flat' || kind === 'deep' || kind === 'map' || kind === 'caps' ||
        kind === 'rows' || (kind === 'flags' && v.length)) {
      var c = el('div', 'card');
      c.appendChild(el('p', 'lbl', label(k)));
      c.appendChild(valueNode(k, v, {}, 0));
      body.appendChild(c);
    }
  });
}

function viewTelemetry(data, body) {
  var snap = isObj(data.snapshot) ? data.snapshot : {};
  var scalars = {}, blocks = [];
  Object.keys(snap).forEach(function (k) {
    var v = snap[k];
    if (isEmptyish(v)) return;
    var kind = kindOf(k, v);
    if (/^(flat|deep|map|caps|rows|mixedlist)$/.test(kind) || (kind === 'flags' && v.length > 3)) {
      blocks.push([k, v]);
    } else scalars[k] = v;
  });

  if (Object.keys(scalars).length) {
    var figs = Object.keys(scalars).slice(0, 6).map(function (k) {
      return { k: k, v: textOf(k, scalars[k]), tone: toneFor(k, scalars[k]) };
    });
    body.appendChild(figureRow(figs));
    var card = el('div', 'card');
    card.appendChild(kvOf(scalars));
    body.appendChild(card);
  }
  blocks.forEach(function (b) {
    var c = el('div', 'card');
    c.appendChild(el('p', 'lbl', label(b[0])));
    c.appendChild(valueNode(b[0], b[1], {}, 0));
    body.appendChild(c);
  });
  if (isObj(data.budgets)) {
    var bc = el('div', 'card');
    bc.appendChild(el('p', 'lbl', 'budgets'));
    bc.appendChild(kvOf(data.budgets));
    body.appendChild(bc);
  }
}

var OUT_TONE = { ok: 'tag-ok', denied: 'tag-deny', error: 'tag-deny', needs_review: 'tag-review' };

function viewAudit(data, body) {
  var chain = isObj(data.chain) ? data.chain : {};
  var valid = chain.valid === true;
  body.appendChild(band(valid ? 'ok' : 'deny',
    t('health.chain') + ': ' + (valid ? t('health.valid') : t('health.broken')),
    num(data.count || 0) + ' ' + t('audit.count') + ' · tenant ' + (data.tenant || '—')));

  var entries = Array.isArray(data.entries) ? data.entries : [];
  if (!entries.length) { body.appendChild(el('p', 'empty', t('ui.none'))); return; }

  var box = el('div', 'card');
  entries.forEach(function (e) {
    var row = el('div', 'audrow');
    var time = el('time', null, fmtDate(e.created_at || e.timestamp || e.at || ''));
    var name = el('span', 'ev-name', String(e.event || '—'));
    var right = el('span', 'r');
    if (e.tool) right.appendChild(el('span', 'tag', e.tool));
    if (e.risk_tier) {
      right.appendChild(notch(e.risk_tier));
      right.appendChild(el('span', 'tag ' + (RISK_TONE[e.risk_tier] || 'tag'), e.risk_tier));
    }
    if (e.outcome) right.appendChild(el('span', 'tag ' + (OUT_TONE[e.outcome] || 'tag'), e.outcome));
    add(row, time, name, right);
    row.addEventListener('click', function () { openSheet(String(e.event || 'audit'), e, t('ui.full')); });
    box.appendChild(row);
  });
  body.appendChild(box);
}

function viewLogs(data, body) {
  var bar = el('div', 'pane-bar');
  add(bar,
    el('span', 'tag ' + (data.masked ? 'tag-ok' : 'tag-deny'),
       data.masked ? t('logs.masked') : t('logs.unmasked')),
    el('span', 'pane-note',
       (data.entries || []).length + ' / ' + (data.capacity || '?') + ' · ' + (data.level || '')));
  body.appendChild(bar);

  var entries = Array.isArray(data.entries) ? data.entries : [];
  if (!entries.length) { body.appendChild(el('p', 'empty', t('ui.none'))); return; }

  var box = el('div', 'logbox');
  entries.forEach(function (e) {
    var lvl = String(e.level || 'INFO').toUpperCase();
    var row = el('div', 'logrow');
    add(row,
      el('time', null, fmtClock(e.created_at || e.timestamp || e.time || '')),
      el('b', 'lv-' + lvl, lvl),
      el('span', null, String(e.message || e.msg || '')));
    box.appendChild(row);
  });
  body.appendChild(box);
}

function mcpCard(title, values) {
  var card = el('div', 'card');
  card.appendChild(el('p', 'lbl', title));
  card.appendChild(kvOf(values || {}));
  return card;
}

function viewMcp(data, body) {
  state.mcp = data;
  var installed = data.installed === true;
  body.appendChild(band(installed ? 'ok' : 'deny',
    installed ? t('mcp.ready') : t('mcp.missing'),
    data.server_name + ' · ' + data.transport +
      (data.sdk_version ? ' · SDK ' + data.sdk_version : '')));
  body.appendChild(band('info', t('mcp.channel'), t('mcp.channelNote')));

  var security = data.security || {};
  body.appendChild(band(security.write_tools_exposed ? 'deny' : 'ok',
    security.write_tools_exposed ? t('mcp.write') : t('mcp.safe'),
    t('mcp.safeNote')));

  if (state.mcpProbe) {
    var p = state.mcpProbe;
    body.appendChild(band(p.ok ? 'ok' : 'deny',
      p.ok ? t('mcp.probeOk') : (p.code || t('ui.failed')),
      p.ok
        ? p.server_name + ' · ' + p.protocol_version + ' · ' + p.tool_count +
          ' tools · ' + p.duration_ms + ' ms · SAP calls ' + p.sap_calls
        : (p.message || t('ui.failed'))));
  }

  var grid = el('div', 'mcp-grid');
  grid.appendChild(mcpCard(t('mcp.identity'), data.identity));
  grid.appendChild(mcpCard(t('mcp.sap'), data.sap));
  grid.appendChild(mcpCard(t('mcp.security'), security));
  body.appendChild(grid);

  var tools = data.tools || {};
  var toolCard = el('div', 'card');
  toolCard.appendChild(el('p', 'lbl', t('mcp.tools') + ' · ' + (tools.count || 0)));
  var toolList = el('div', 'mcp-tools');
  (tools.names || []).forEach(function (name) {
    toolList.appendChild(el('span', 'tag tag-info', name));
  });
  if (!toolList.childNodes.length) toolList.appendChild(el('p', 'empty', t('ui.none')));
  toolCard.appendChild(toolList);
  body.appendChild(toolCard);

  var cfgCard = el('div', 'card');
  cfgCard.appendChild(el('p', 'lbl', t('mcp.config')));
  cfgCard.appendChild(el('p', 'card-desc', t('mcp.noSecrets')));
  var pre = el('pre', 'mcp-config');
  pre.appendChild(el('code', null, JSON.stringify(data.client_config || {}, null, 2)));
  cfgCard.appendChild(pre);
  body.appendChild(cfgCard);

  if (Array.isArray(data.notes) && data.notes.length) {
    body.appendChild(section(t('mcp.notes'), bullets(data.notes)));
  }
}

function testMcp() {
  var button = $('#mcp-test');
  if (!button || button.disabled) return;
  button.disabled = true;
  button.textContent = t('mcp.testing');
  api('/ui/mcp/test', { method: 'POST' }).then(function (probe) {
    state.mcpProbe = probe;
    if (state.mcp && state.view === 'mcp') {
      clear($('#mcp-body')); viewMcp(state.mcp, $('#mcp-body'));
    }
  }).catch(function (err) {
    state.mcpProbe = { ok: false, code: err.code, message: err.message };
    if (state.mcp && state.view === 'mcp') {
      clear($('#mcp-body')); viewMcp(state.mcp, $('#mcp-body'));
    }
  }).then(function () {
    button.disabled = false;
    button.textContent = t('mcp.test');
  });
}

/* ==========================================================================
   12. TAM KAYIT PANELİ
   ========================================================================== */
var sheetData = null;

/* --- Ayarlar -------------------------------------------------------------
   Bu ekran bir yonetim yuzeyi: her alan neyin degistirilebildigini VE
   degistirilemiyorsa nedenini soyler. Sessizce devre disi birakilmis bir
   kontrol, kullaniciyi kendi yetkisi hakkinda yaniltir.

   Renk yalniz anlam tasir: sonuc doguran ayarlar risk centigi alir, kilitli
   olanlar renk degil doku degistirir. */

function cfgControl(alan, onChange) {
  var kutu = el('div', 'cfg-ctl');
  var girdi;

  if (alan.kind === 'bool') {
    girdi = el('select');
    [['true', t('ui.yes')], ['false', t('ui.no')]].forEach(function (c) {
      var o = el('option', null, c[1]);
      o.value = c[0];
      if (String(alan.value).toLowerCase() === c[0]) o.selected = true;
      girdi.appendChild(o);
    });
  } else if (alan.kind === 'enum') {
    girdi = el('select');
    alan.choices.forEach(function (c) {
      var o = el('option', null, c);
      o.value = c;
      if (String(alan.value).toLowerCase() === String(c).toLowerCase()) o.selected = true;
      girdi.appendChild(o);
    });
  } else {
    girdi = el('input');
    girdi.type = 'number';
    girdi.value = alan.value === null || alan.value === undefined ? '' : String(alan.value);
    if (alan.minimum !== null && alan.minimum !== undefined) girdi.min = String(alan.minimum);
    if (alan.maximum !== null && alan.maximum !== undefined) girdi.max = String(alan.maximum);
  }

  girdi.disabled = !!alan.locked;
  girdi.id = 'cfg-' + alan.key;
  kutu.appendChild(girdi);

  if (!alan.locked) {
    var kaydet = el('button', 'btn', t('cfg.apply'));
    kaydet.type = 'button';
    kaydet.disabled = true;
    girdi.addEventListener('change', function () { kaydet.disabled = false; });
    girdi.addEventListener('input', function () { kaydet.disabled = false; });
    kaydet.addEventListener('click', function () {
      kaydet.disabled = true;
      onChange(alan.key, girdi.value);
    });
    kutu.appendChild(kaydet);
  }
  return kutu;
}

function cfgField(alan, onChange) {
  var satir = el('div', 'cfg-f');
  if (alan.consequential) satir.setAttribute('data-consequential', '1');
  if (alan.locked) satir.setAttribute('data-locked', '1');

  var sol = el('div', 'cfg-l');
  sol.appendChild(el('div', 'cfg-name', alan.label || alan.key));
  sol.appendChild(el('code', 'cfg-key', alan.key));
  if (alan.note) sol.appendChild(el('p', 'cfg-note', alan.note));

  var isaret = el('div', 'cfg-marks');
  if (alan.consequential) {
    var c = el('span', 'cfg-mark cfg-mark-conseq', t('cfg.consequential'));
    c.setAttribute('title', t('cfg.consequentialNote'));
    isaret.appendChild(c);
  }
  if (!alan.live) isaret.appendChild(el('span', 'cfg-mark', t('cfg.restart')));
  if (alan.overridden) {
    var o = el('span', 'cfg-mark', t('cfg.overridden'));
    if (alan.changed_by) o.setAttribute('title', alan.changed_by + ' · ' + fmtClock(alan.changed_at));
    isaret.appendChild(o);
  }
  if (alan.locked) {
    var neden = alan.locked_reason === 'env_pinned' ? t('cfg.envPinned') : t('cfg.noScope');
    var k = el('span', 'cfg-mark cfg-mark-lock', neden);
    k.setAttribute('title', alan.locked_reason === 'env_pinned'
      ? t('cfg.envPinnedNote') : t('cfg.noScopeNote'));
    isaret.appendChild(k);
  }
  if (isaret.childNodes.length) sol.appendChild(isaret);

  add(satir, sol, cfgControl(alan, onChange));
  return satir;
}

function viewSettings(data, body) {
  function yenile() { loadView('settings'); }

  function gonder(yol, govde, basarisiz) {
    api(yol, { method: 'POST', body: govde })
      .then(function (r) {
        toast(r.message || t('cfg.saved'));
        yenile();
      })
      .catch(function (err) {
        // ApiError FastAPI'nin `detail` sarmalayicisini zaten aciyor:
        // err.code ve err.message hazir, err.detail ham govde.
        var metin = err.message || t('ui.failed');
        var ek = err.detail && err.detail.detail;
        if (ek && ek.length) metin += ' — ' + [].concat(ek).join(' · ');
        (basarisiz || function () {})();
        body.insertBefore(band('deny', err.code || t('ui.failed'), metin), body.firstChild);
        toast(err.code || t('ui.failed'));
      });
  }

  function degistir(key, value) {
    gonder('/ui/settings', { key: key, value: value });
  }

  /* Uretim durumu once: bir ayari degistirmeden once nerede oldugunu bil. */
  var hazir = data.production_ready === true;
  body.appendChild(band(hazir ? 'ok' : 'review',
    t('cfg.posture') + ': ' + (data.app_env || ''),
    hazir ? t('cfg.compliant') : t('cfg.blockersPresent')));
  if (Array.isArray(data.production_blockers) && data.production_blockers.length) {
    body.appendChild(section(t('health.blockers'), bullets(data.production_blockers)));
  }

  if (!data.can_change) {
    body.appendChild(band('mute', t('cfg.readOnly'),
      t('cfg.readOnlyNote').replace('{scope}', data.required_scope || 'platform.config')));
  }

  /* Bekleyen degisiklikler: ikinci kimlik bekleyenler en ustte durur. */
  if (Array.isArray(data.pending) && data.pending.length) {
    var kutu = el('div', 'card');
    kutu.appendChild(el('p', 'lbl', t('cfg.pending')));
    data.pending.forEach(function (b) {
      var satir = el('div', 'cfg-pend');
      var metin = el('div', 'cfg-pend-t');
      metin.appendChild(el('code', 'cfg-key', b.key));
      metin.appendChild(el('span', null, ' ' + String(b.before) + ' → ' + String(b.value)));
      var kim = el('div', 'cfg-pend-m',
        t('cfg.requestedBy') + ' ' + (b.requested_by || '?') +
        (b.reason ? ' · ' + b.reason : ''));
      add(satir, metin, kim);

      var eylem = el('div', 'cfg-pend-a');
      var onay = el('button', 'btn', t('cfg.approve'));
      onay.type = 'button';
      onay.addEventListener('click', function () {
        onay.disabled = true;
        gonder('/ui/settings/approve', { change_id: b.change_id },
               function () { onay.disabled = false; });
      });
      var iptal = el('button', 'btn', t('cfg.cancel'));
      iptal.type = 'button';
      iptal.addEventListener('click', function () {
        iptal.disabled = true;
        gonder('/ui/settings/cancel', { change_id: b.change_id },
               function () { iptal.disabled = false; });
      });
      add(eylem, onay, iptal);
      satir.appendChild(eylem);
      kutu.appendChild(satir);
    });
    body.appendChild(kutu);
  }

  /* Bolumler */
  var bolumler = {};
  (data.settings || []).forEach(function (a) {
    (bolumler[a.section] = bolumler[a.section] || []).push(a);
  });
  ['sap', 'onay', 'veri', 'gozlem'].forEach(function (ad) {
    var alanlar = bolumler[ad];
    if (!alanlar || !alanlar.length) return;
    var kart = el('div', 'card');
    kart.appendChild(el('p', 'lbl', t('cfg.sec.' + ad)));
    alanlar.forEach(function (a) { kart.appendChild(cfgField(a, degistir)); });
    body.appendChild(kart);
  });

  /* Degistirilemeyenler acikca yazilir: bir yonetim ekraninin
     gostermedigi sey, gosterdigi kadar bilgi tasir. */
  var uyari = el('div', 'card');
  uyari.appendChild(el('p', 'lbl', t('cfg.never')));
  uyari.appendChild(bullets([t('cfg.never1'), t('cfg.never2'), t('cfg.never3'),
                             t('cfg.never4')]));
  body.appendChild(uyari);
}

function openSheet(title, data, kicker) {
  sheetData = data;
  $('#sheet-title').textContent = title;
  $('#sheet-kicker').textContent = kicker || '';
  var body = $('#sheet-body');
  clear(body);
  if (isObj(data)) {
    body.appendChild(objectOf(data, { currency: data.currency }, 0));
    body.appendChild(section(t('ui.raw'), jsonOf(data)));
  } else {
    body.appendChild(jsonOf(data));
  }
  $('#sheet').hidden = false;
  $('#sheet-x').focus();
}
function closeSheet() { $('#sheet').hidden = true; sheetData = null; }

/* ==========================================================================
   13. TEMA, DİL, GÖRÜNÜM
   ========================================================================== */
function applyTheme(v) {
  document.documentElement.setAttribute('data-theme', v);
  lsSet(K_THEME, v);
}
function cycleTheme() {
  var order = ['auto', 'light', 'dark'];
  var cur = document.documentElement.getAttribute('data-theme') || 'auto';
  applyTheme(order[(order.indexOf(cur) + 1) % order.length]);
}

function applyLang(lang) {
  LANG = I18N[lang] ? lang : 'tr';
  document.documentElement.setAttribute('lang', LANG);
  lsSet(K_LANG, LANG);
  $$('[data-i18n]').forEach(function (n) { n.textContent = t(n.dataset.i18n); });
  $$('[data-i18n-attr]').forEach(function (n) {
    var p = n.dataset.i18nAttr.split(':'); n.setAttribute(p[0], t(p[1]));
  });
  $$('[data-i18n-attr2]').forEach(function (n) {
    var p = n.dataset.i18nAttr2.split(':'); n.setAttribute(p[0], t(p[1]));
  });
  $$('.seg-b').forEach(function (b) { b.classList.toggle('on', b.dataset.lang === LANG); });
  renderSeeds();
  applyMode();
  renderPosture();
  if (state.view !== 'ledger') loadView(state.view);
}

function applyMode() {
  var cfg = state.config || {};
  var pill = $('#mode'), live = cfg.mode === 'live';
  pill.className = 'mode ' + (cfg.mode ? (live ? 'mode-live' : 'mode-sim') : 'mode-unknown');
  $('#mode-text').textContent = cfg.mode ? (live ? t('mode.live') : t('mode.sim')) : t('mode.wait');
  $('#dry').hidden = !cfg.dry_run;
  $('#dry').textContent = t('mode.dry');
  $('#app').setAttribute('data-mode', cfg.mode || '');
}

function switchView(view) {
  state.view = view;
  $$('.nav-b').forEach(function (b) { b.classList.toggle('on', b.dataset.view === view); });
  // Gorunum listesi DOM'dan turetilir. Sabit bir dizi, yeni bir sekme
  // eklendiginde sessizce unutulur: sekme aktif gorunur, icerik uretilir,
  // ama bolum hicbir zaman gorunur olmaz.
  $$('.view').forEach(function (bolum) {
    bolum.hidden = bolum.id !== ('v-' + view);
  });
  if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
  if (view === 'ledger') { $('#cmd').focus(); return; }
  loadView(view);
  if (view === 'logs' && $('#log-follow').checked) {
    state.logTimer = setInterval(function () { loadView('logs'); }, 5000);
  }
}

function renderSeeds() {
  var box = $('#seeds');
  if (!box) return;
  clear(box);
  [1, 2, 3, 4].forEach(function (n) {
    var b = el('button', 'seed');
    b.type = 'button';
    add(b, el('b', null, '0' + n), el('span', null, t('seed.' + n)));
    b.addEventListener('click', function () {
      $('#cmd').value = t('seed.' + n); grow($('#cmd')); send();
    });
    box.appendChild(b);
  });
}

function grow(ta) {
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 168) + 'px';
}

/* ==========================================================================
   14. GİRİŞ VE OLAYLAR
   ========================================================================== */
function showGate(msg) {
  $('#gate').hidden = false;
  $('#app').hidden = true;
  var box = $('#gate-err');
  if (msg) { box.textContent = msg; box.hidden = false; } else { box.hidden = true; }
  var input = $('#token'); input.value = '';
  setTimeout(function () { input.focus(); }, 40);
}

function enterApp() {
  $('#gate').hidden = true;
  $('#app').hidden = false;
  var actor = (state.config && state.config.actor) || {};
  $('#who').textContent = (actor.subject || '') + (actor.tenant ? ' · ' + actor.tenant : '');
  $('#who').setAttribute('title', actor.subject || '');

  var canAudit = !!(state.config && state.config.can_read_audit);
  $$('[data-scope="audit"]').forEach(function (b) { b.hidden = !canAudit; });
  if (!canAudit && (state.view === 'audit' || state.view === 'logs')) switchView('ledger');

  applyMode();
  renderPosture();
  $('#cmd').focus();

  // Tool sözleşmeleri: risk basamağı sohbet yanıtında gelmiyor, katalogdan
  // alınıyor — her istasyon kendi çentiğini böyle kazanıyor.
  api('/tools').then(function (data) {
    state.tools = data;
    state.toolByName = {};
    (data.tools || []).forEach(function (s) { state.toolByName[s.tool] = s; });
    renderPosture();
  }).catch(function () {});
}

function boot() {
  applyTheme(ls(K_THEME) || 'auto');
  applyLang(ls(K_LANG) || 'tr');
  wire();

  var saved = ss(K_TOKEN);
  if (saved) state.token = saved;
  api('/ui/config').then(function (cfg) {
    state.config = cfg; enterApp();
  }).catch(function () {
    if (saved) { ssDel(K_TOKEN); state.token = ''; }
    showGate();
  });
}

function wire() {
  $('#gate-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var tok = $('#token').value.trim();
    if (!tok) return;
    state.token = tok;
    api('/ui/config').then(function (cfg) {
      state.config = cfg; ssSet(K_TOKEN, tok); $('#gate-err').hidden = true; enterApp();
    }).catch(function (err) {
      state.token = '';
      var box = $('#gate-err');
      box.textContent = err.status === 403 ? t('gate.scope')
        : err.status === 0 ? t('gate.down') : t('gate.bad');
      box.hidden = false;
    });
  });
  $('#token-eye').addEventListener('click', function () {
    var i = $('#token'); i.type = i.type === 'password' ? 'text' : 'password';
  });
  $('#out').addEventListener('click', function () {
    ssDel(K_TOKEN); location.reload();
  });

  $$('.nav-b').forEach(function (b) {
    b.addEventListener('click', function () { switchView(b.dataset.view); });
  });
  $('#theme').addEventListener('click', cycleTheme);
  $$('.seg-b').forEach(function (b) {
    b.addEventListener('click', function () { applyLang(b.dataset.lang); });
  });

  var cmd = $('#cmd');
  cmd.addEventListener('input', function () { grow(cmd); });
  cmd.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  $('#entry').addEventListener('submit', function (e) {
    e.preventDefault();
    if (state.busy && state.abort) { state.abort.abort(); return; }
    send();
  });

  var sc = $('#scroll');
  sc.addEventListener('scroll', function () {
    $('#to-end').hidden = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 200;
  });
  $('#to-end').addEventListener('click', toEnd);

  $('#show-gates').addEventListener('change', function (e) {
    $$('.gate-row').forEach(function (g) { g.hidden = !e.target.checked; });
  });

  $$('[data-load]').forEach(function (b) {
    b.addEventListener('click', function () { loadView(b.dataset.load); });
  });
  $('#tool-q').addEventListener('input', function (e) {
    state.toolQuery = e.target.value;
    if (state.tools) viewTools(state.tools, $('#tools-body'));
  });
  $('#audit-limit').addEventListener('change', function () { loadView('audit'); });
  $('#log-level').addEventListener('change', function () { loadView('logs'); });
  $('#log-follow').addEventListener('change', function (e) {
    if (state.logTimer) { clearInterval(state.logTimer); state.logTimer = null; }
    if (e.target.checked) state.logTimer = setInterval(function () { loadView('logs'); }, 5000);
  });
  $('#mcp-test').addEventListener('click', testMcp);
  $('#mcp-copy').addEventListener('click', function () {
    if (state.mcp) copy(JSON.stringify(state.mcp.client_config || {}, null, 2));
  });

  $('#sheet-x').addEventListener('click', closeSheet);
  $('#sheet').addEventListener('click', function (e) { if (e.target.id === 'sheet') closeSheet(); });
  $('#sheet-copy').addEventListener('click', function () {
    if (sheetData) copy(JSON.stringify(sheetData, null, 2));
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && !$('#sheet').hidden) closeSheet();
  });
}

document.addEventListener('DOMContentLoaded', boot);
