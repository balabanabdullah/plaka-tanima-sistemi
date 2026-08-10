# Sistem Mimarisi

## Genel Bakış

Sistem iki ana çalışma alanından oluşur:

1. Güvenlik bilgisayarındaki yerel Edge AI ve SQLite katmanı
2. Cloud Run, FastAPI web paneli ve Cloud SQL PostgreSQL katmanı

Yerel çalışma, bulut bağlantısından bağımsızdır. Bulut merkezi görüntüleme ve yetkilendirme sağlar; gerçek zamanlı OCR veya bariyer kararının zorunlu bir parçası değildir.

```text
Kamera -> YOLO -> OCR -> Consensus -> Access Decision -> SQLite
                                               |            |
                                               v            v
                                             ESP32      HTTPS Sync
                                                               |
                                                               v
                                                     Cloud Run / Cloud SQL
```

## Local AI Pipeline

`src/ocr_reader.py` ana canlı akıştır:

1. OpenCV `VideoCapture` ile sayısal kamera cihazı açılır.
2. `models/license_plate_detector.pt` Ultralytics YOLO modeli frame üzerinde çalışır.
3. Tespit edilen plaka crop'u boyut ve keskinlik kontrollerinden geçer.
4. Crop en-boy oranı korunarak 3x büyütülür.
5. Upscale OCR sonucu yeterince güvenliyse kullanılır; değilse grayscale/CLAHE alternatifi değerlendirilir.
6. EasyOCR parçaları geometrik konumlarına göre sıralanır ve Latin alfanümerik metne normalize edilir.
7. Son frame geçmişindeki exact tekrar, ortalama confidence ve baskınlık final adayı belirler.
8. Kararlı aday yalnızca fiziksel olay state machine izin verirse `process_plate_access()` akışına girer.

YOLO ve EasyOCR CPU üzerinde yerel çalışır. Otsu threshold ana runtime OCR yolunda değildir; manuel diagnostik araçta karşılaştırma için bulunur.

## Vehicle ve AccessLog

`src/models.py` iki temel SQLAlchemy modeli tanımlar.

### Vehicle

- Normalize edilmiş plaka başına tek ana kayıt
- `normalized_plate` unique
- Yetki durumu: `pending`, `approved`, `rejected`, `inactive`
- İlk/son görülme ve onay bilgileri
- Yerel primary key'den bağımsız `sync_id` UUID

### AccessLog

- Her kabul edilen fiziksel geçiş denemesinin kaydı
- Plaka metni, yön, karar, OCR confidence, kamera ve zaman
- Kararlar: `allow`, `wait_for_approval`, `deny`, `manual_override`
- Yönler: `entry`, `exit`, `unknown`
- Cloud idempotency için ayrı `sync_id` UUID

Yeni araç varsayılan olarak `pending` oluşturulur. `approved` araç `allow`, `pending` araç `wait_for_approval`, `rejected` ve `inactive` araç `deny` sonucu üretir.

## sync_id ve Idempotency

Yerel SQLite ve Cloud SQL farklı integer primary key değerlerine sahip olabilir. Bu yüzden senkronizasyon kimliği olarak UUID biçimli `sync_id` kullanılır.

- Eski yerel kayıtlarda UUID eksikse `cloud_sync.ensure_local_sync_ids()` tamamlar.
- Cloud `/api/sync/push`, Vehicle kaydını önce `sync_id`, gerekirse `normalized_plate` ile bulur.
- AccessLog aynı `sync_id` ile daha önce aktarılmışsa tekrar oluşturulmaz.
- Tekrarlanan HTTPS push güvenli/idempotent kalır.

Bu idempotency, periyodik full-data retry yaklaşımının mükerrer satır üretmeden çalışmasını sağlar.

## Entry/Exit State Derivation

Automatic direction bellekte tutulan geçici bir sayaçtan değil, veritabanındaki son başarılı `ALLOW` AccessLog kaydından türetilir:

```text
ALLOW kaydı yok / son yön EXIT -> sonraki yön ENTRY
son başarılı yön ENTRY         -> sonraki yön EXIT
son yön UNKNOWN                -> güvenli varsayılan ENTRY
```

`WAIT_FOR_APPROVAL` ve `DENY` kayıtları presence durumunu değiştirmez. Böylece uygulama yeniden başlatılsa bile son başarılı hareketten `inside`/`outside` durumu yeniden hesaplanabilir.

## Physical-event Deduplication

Canlı OCR aynı araç kamerada dururken çok sayıda farklı frame üretir. `ocr_reader.py` bunu tek fiziksel olay olarak yönetir:

- OCR history ve final consensus kararlı plaka bekler.
- Muhafazakâr `is_probable_same_plate()` aynı olay içindeki küçük OCR varyasyonlarını bastırır.
- Alakasız tek frame outlier yeni araç sayılmaz.
- Farklı plaka adayının birkaç frame tutarlı olması gerekir.
- Aynı olayda `process_plate_access()` en fazla bir kez çalışır.
- Aynı olayda bariyer en fazla bir kez açılır.
- YOLO plaka tespiti yaklaşık 3 saniye yok olduğunda olay resetlenir.
- DB seviyesindeki kısa cooldown ek tekrar log koruması sağlar.

Fuzzy eşleştirme Vehicle satırlarını birleştirmez; yalnızca canlı olay bağlamında gürültü bastırma amacı taşır.

## Local → Cloud Sync

### Immediate wakeup

Vehicle veya AccessLog başarılı SQLite commit'inden sonra `src/sync_signal.py`, `data/.cloud_sync_wakeup` dosyasına yeni bir sürüm yazar. Bu işlem network çağrısı değildir ve hata verirse OCR akışını durdurmaz.

`SyncManager` içindeki tek LOCAL → CLOUD worker sinyali izler:

- Varsayılan polling: 250 ms
- Kısa coalescing/debounce: 300 ms
- Birden fazla hızlı sinyal tek push'ta birleşir
- Paralel HTTP request oluşturulmaz
- Sync sırasında gelen daha yeni sinyal sonraki tur için korunur

### Periodic retry

Immediate sinyal olmasa da worker varsayılan 60 saniyelik periyotta çalışır. İnternet/HTTP hatasında worker yaşamaya devam eder ve sonraki sinyal veya interval'da tekrar dener.

`cloud_sync.py` SQLite'taki Vehicle ve AccessLog kayıtlarını JSON payload'a çevirerek Cloud Run `/api/sync/push` endpoint'ine Bearer token ile gönderir. Mevcut uygulama incremental cursor yerine tüm yerel kümeyi gönderir; cloud idempotency tekrarları güvenli kılar.

## Cloud → Local Approval Sync

İkinci worker varsayılan 30 saniyede bir yerel Vehicle `sync_id` listesini `/api/sync/approvals` endpoint'ine gönderir.

Cloud response içinden yalnızca şu alanlar yerelde uygulanır:

- `status`
- `approved_at`
- `approved_by`
- `notes`

Yerel `plate_text`, görülme zamanları, AccessLog geçmişi, yön ve karar kayıtları değiştirilmez. HTTP hatasında transaction geri alınır ve yerel durum korunur.

## Web Authentication ve Machine Authentication

İki ayrı güvenlik sınırı vardır.

### İnsan/web paneli

- `WEB_ADMIN_USERNAME` ve `WEB_ADMIN_PASSWORD` ile tek yönetici login
- Starlette imzalı session cookie
- 8 saat max-age
- `HttpOnly`, `SameSite=Lax`, production'da `Secure`
- Login/logout/approve/reject/delete için session tabanlı CSRF
- Eksik config durumunda protected panel HTTP 503 fail-closed

Korunan yollar: `/`, `/pending`, `/vehicles`, `/access-logs` ve Vehicle yönetim POST'ları.

### Machine-to-machine sync

- `/api/sync/push`
- `/api/sync/approvals`

Bu endpointler web session istemez. `Authorization: Bearer <SYNC_API_TOKEN>` başlığı kullanır ve constant-time token karşılaştırması yapar. `/health`, login endpointleri ve static dosyalar da public kalır.

## Offline Failure Modes

| Hata | Yerel davranış | Kurtarma |
|---|---|---|
| İnternet yok | OCR, SQLite, karar ve bağlı ESP32 çalışır | Periyodik HTTPS retry |
| Cloud Run erişilemiyor | Yerel kayıtlar korunur | Sonraki sinyal/interval |
| Sync Manager kapalı | OCR sinyal yazmayı dener, çökmez | Manager açıldığında full sync |
| Approval sync hatası | Son yerel authorization durumu korunur | Sonraki 30 saniyelik tur |
| Web auth config eksik | Panel 503 fail-closed | Secret/env yapılandırılır |
| Sync token eksik | Sync API 503; local worker güvenle atlar/hata raporlar | Token runtime'a verilir |
| ESP32 yok | OCR ve DB çalışır, fiziksel OPEN başarısız/dry-run | Seri port/donanım düzeltilir |
| Kamera açılamıyor | OCR süreci güvenli şekilde sonlanır | Kamera indeksi/driver düzeltilir |

Yerel SQLite sistemin operasyonel source-of-truth katmanıdır. Bulut kesintisi access decision hot path'ine taşınmaz.
