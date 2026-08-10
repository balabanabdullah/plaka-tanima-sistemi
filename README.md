# Plaka Tanıma ve Bariyer Kontrol Sistemi

## Proje Özeti

Bu proje, kamera görüntüsünden araç plakasını yerel olarak algılayan ve yetkilendirme durumuna göre geçiş kararı üreten çalışan bir Edge AI MVP'sidir.

- Plaka bölgesi Ultralytics YOLO ile tespit edilir.
- Plaka metni EasyOCR ile okunur ve çok kareli uzlaşma (multi-frame consensus) ile kararlı hale getirilir.
- Araçlar ve geçiş kayıtları yerel SQLite veritabanına yazılır.
- Sistem internet bağlantısından bağımsız, offline-first çalışır.
- İlk kez görülen araçlar `pending` durumunda güvenlik görevlisinin onayına sunulur.
- Onaylı araçlar için otomatik giriş/çıkış yönü ve ESP32 bariyer komutu desteklenir.
- FastAPI/Jinja2 web paneli Cloud Run üzerinde, PostgreSQL tabanlı Cloud SQL ile çalışabilir.
- Yerel bilgisayar ile bulut arasında Bearer token korumalı HTTPS/443 senkronizasyonu bulunur.

## Sistem Mimarisi

```text
Telefon / DroidCam / USB Kamera
              |
              v
       YOLO + EasyOCR
              |
              v
        Local SQLite
              |
              +----> BarrierController ----> ESP32
              |
              +----> HTTPS Sync API ----> Cloud Run ----> Cloud SQL
                                           |
                                           v
                                       Web Panel
                                           |
                     approval sync         |
        Local SQLite <----------------------+
```

OpenCV altyapısı farklı video kaynaklarını destekleyebilse de mevcut `ocr_reader.py` CLI arayüzü `--camera` için sayısal cihaz indeksi kabul eder. USB kamera ve Windows'ta sanal kamera olarak görünen DroidCam bu yolla kullanılabilir. Doğrudan RTSP/IP URL girişi mevcut CLI'da etkin değildir.

Daha teknik açıklama için [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) belgesine bakın.

## Temel Özellikler

- YOLO tabanlı license plate detection
- EasyOCR ve adaptif OCR preprocessing
- Confidence, tekrar sayısı ve frame'ler arası tutarlılığa dayalı multi-frame consensus
- Latin `A-Z` ve `0-9` kapsamındaki yabancı/alışılmadık plaka desteği
- `pending`, `approved`, `rejected`, `inactive` araç durumları
- Son başarılı `ALLOW` kaydından türetilen automatic entry/exit
- Aynı araç görünür kaldığında tekrar log ve bariyer açılmasını önleyen physical-event deduplication
- Yerel SQLite ile offline-first çalışma
- Commit sonrası hızlı Local → Cloud wakeup sinyali
- Periyodik Local → Cloud retry senkronizasyonu
- Cloud → Local authorization sync
- FastAPI dashboard, araç yönetimi ve erişim kayıtları
- Tek yönetici login, imzalı session cookie ve CSRF koruması
- Bearer token korumalı machine-to-machine sync API
- Google Secret Manager ile runtime secret sağlama modeli
- ESP32 seri port entegrasyonu ve dry-run modu

## Kullanılan Teknolojiler

- Python 3
- OpenCV
- Ultralytics YOLO
- EasyOCR
- SQLAlchemy
- SQLite
- PostgreSQL / Cloud SQL
- FastAPI
- Jinja2
- Starlette SessionMiddleware / itsdangerous
- Docker ve Docker Compose
- Google Cloud Run
- Google Artifact Registry
- Google Secret Manager
- ESP32 / Arduino

## Proje Yapısı

```text
src/                         Ana Python modülleri
  ocr_reader.py              Kamera, YOLO, EasyOCR ve fiziksel olay akışı
  plate_service.py           Plaka, karar, log, yön ve presence servisleri
  database.py                SQLAlchemy engine/session ve SQLite başlangıcı
  models.py                  Vehicle ve AccessLog veri modelleri
  barrier_controller.py      ESP32 USB Serial OPEN protokolü
  cloud_sync.py              Local -> Cloud HTTPS push
  approval_sync.py           Cloud -> Local yetkilendirme güncellemesi
  sync_manager.py            Çift yönlü sync worker yöneticisi
  sync_signal.py             Commit sonrası immediate-sync wakeup sinyali
  web_app.py                 FastAPI paneli, login ve sync API
templates/                   Jinja2 panel ve login şablonları
static/                      Web paneli CSS dosyaları
tests/                       Otomatik testler ve manuel diagnostik araçlar
esp32/                       ESP32 Arduino örnek kodu
data/                        Yerel SQLite ve runtime sync sinyali (Git dışı)
captures/                    Manuel OCR debug görüntüleri (Git dışı)
models/                      YOLO ağırlığı için yerel klasör
datasets/                    Veri seti çalışma alanı
docs/                        Teknik, deployment ve kullanım belgeleri
Dockerfile                   Web/Cloud Run container image tanımı
docker-compose.yml           Yerel web container servisi
start_system.bat             Web, Sync Manager ve OCR başlangıcı
stop_system.bat              Yerel servisleri durdurma
```

`docker/` ve `kubernetes/` klasörleri şu anda yalnızca yer tutucu içerir; aktif deployment manifestleri repository'de bulunmaz.

## Kurulum

Windows PowerShell veya Command Prompt üzerinde:

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-web.txt
```

`requirements.txt` yerel OCR/AI, veritabanı, FastAPI ve seri port bağımlılıklarını içerir. `requirements-web.txt` daha küçük Cloud Run web image'ı için FastAPI, PostgreSQL ve session bağımlılıklarını içerir. Tam yerel geliştirme ortamında iki dosyanın da kurulması önerilir.

Yerel birleşik başlangıç için Docker Desktop da kurulu ve çalışır durumda olmalıdır.

> `.env.example` bir referans şablonudur; uygulamada `python-dotenv` tabanlı otomatik `.env` yükleme yoktur. Değişkenleri çalıştıran shell, Docker/Cloud Run runtime yapılandırması veya Secret Manager entegrasyonu sağlamalıdır.

## Model

OCR uygulaması şu dosyayı bekler:

```text
models/license_plate_detector.pt
```

`.pt` dosyaları `.gitignore` ile Git dışında tutulur; bu nedenle model ağırlığı repository clone işleminden sonra ayrıca sağlanmalıdır. Model bir secret değildir, ancak boyutu ve dağıtım/lisans yönetimi nedeniyle source control'e eklenmemiştir. Repository sahte veya doğrulanmamış bir download URL sağlamaz.

## Environment Variables

`.env.example` içindeki değişkenler:

| Değişken | Secret? | Kullanım |
|---|---:|---|
| `APP_ENV` | Hayır | `production` olduğunda web session cookie'sine `Secure` bayrağı eklenir. Varsayılan geliştirme davranışıdır. |
| `APP_HOST` | Hayır | Deployment/çalıştırma referansı. Mevcut Docker CMD doğrudan `0.0.0.0` kullanır. |
| `APP_PORT` | Hayır | Deployment/çalıştırma referansı. Mevcut Docker image ve compose portu `8000` kullanır. |
| `WEB_ADMIN_USERNAME` | Genellikle hayır | Tek web paneli yöneticisinin kullanıcı adı. |
| `WEB_ADMIN_PASSWORD` | **Evet** | Web paneli yöneticisinin parolası. |
| `WEB_SESSION_SECRET` | **Evet** | Session cookie imza anahtarı. Uzun ve rastgele olmalıdır. |
| `CAMERA_SOURCE` | Hayır | `start_system.bat` tarafından `--camera` cihaz indeksine aktarılır; boşsa `0`. |
| `DATABASE_URL` | **Evet olabilir** | Boşsa `data/plate_system.db` SQLite; Cloud Run'da PostgreSQL/Cloud SQL SQLAlchemy URL'si. |
| `CLOUD_SYNC_API_URL` | Hayır | Cloud Run servisinin temel HTTPS URL'si. |
| `SYNC_API_TOKEN` | **Evet** | `/api/sync/push` ve `/api/sync/approvals` Bearer token'ı. |
| `CLOUD_DATABASE_URL` | **Evet olabilir** | Legacy/doğrudan DB bağlantısı için ayrılmış örnek değer; normal sync yolu bunu kullanmaz. |
| `ESP32_IP` | Hayır | Örnek/gelecek ağ donanımı ayarı; mevcut `BarrierController` USB Serial CLI parametresi kullanır. |
| `ESP32_PORT` | Hayır | Örnek/gelecek ağ donanımı ayarı; mevcut seri port baud/COM ayarıyla aynı şey değildir. |

Gerçek parola, DB credential, API token ve session secret değerlerini README'ye, `.env.example` içine veya Git'e yazmayın. Production Cloud Run servisinde `WEB_ADMIN_PASSWORD`, `WEB_SESSION_SECRET`, `SYNC_API_TOKEN` ve credential içeren `DATABASE_URL` değerlerini Google Secret Manager üzerinden verin.

Web auth ayarlarından biri eksikse panel fail-closed davranır ve HTTP 503 döndürür. `/health` ve doğru Bearer token ile çağrılan sync API çalışmaya devam eder.

## Telefon Kamerası / DroidCam

DroidCam, telefon kamerasını Windows üzerinde sanal kamera cihazı olarak gösterebilir. Kamera indeksi bilgisayardaki cihaz sırasına göre değişir.

Örneğin DroidCam index `1` ise:

```bat
set CAMERA_SOURCE=1
start_system.bat
```

Varsayılan kamera için:

```bat
set CAMERA_SOURCE=0
start_system.bat
```

Doğru indeksi bulmak için [Diagnostic Tools](#diagnostic-tools) bölümündeki kamera testini kullanabilirsiniz.

## Sistemi Başlatma

Gerekli environment variable'ları çalıştıran ortamda tanımladıktan sonra:

```bat
start_system.bat
```

Script üç bileşen başlatır:

1. `docker compose up -d` ile web servisi
2. Ayrı pencerede `src/sync_manager.py`
3. Ayrı pencerede `src/ocr_reader.py --direction auto --barrier-dry-run`

Web arayüzü varsayılan olarak `http://localhost:8000` adresindedir. `start_system.bat` OCR ve Sync Manager için mevcut shell environment'ını kullanır. Web container'ın auth/database değişkenleri ise Docker runtime/deployment yapılandırmasıyla container'a verilmelidir; compose dosyası şu anda secret değerlerini source control içinde tanımlamaz.

Normal HTTPS sync mimarisinde Cloud SQL Auth Proxy gerekli değildir. Yerel bilgisayar Cloud Run'a standart HTTPS/443 üzerinden bağlanır.

Geliştirme amacıyla web paneli doğrudan da çalıştırılabilir:

```bat
.venv\Scripts\python.exe -m uvicorn web_app:app --app-dir src --host 127.0.0.1 --port 8000
```

## Sistemi Durdurma

```bat
stop_system.bat
```

Bu script Docker web servisini durdurur ve `Plaka - OCR` ile `Plaka - Sync Manager` başlıklı pencereleri kapatır.

## Testler

Tüm otomatik testler gerçek Google Cloud veya fiziksel ESP32 gerektirmeden çalışır:

```bat
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Sanal ortam aktifse istenen kısa komut da kullanılabilir:

```bat
python -m unittest discover -s tests -v
```

Test paketi OCR consensus, event dedup, automatic direction, bariyer seri protokolü, SQLite/cloud sync, immediate wakeup, approval sync, web login/session/CSRF ve Bearer API güvenliğini kapsar.

## Diagnostic Tools

Bu scriptler production runtime'ın parçası değildir:

```bat
# Windows MSMF ve DSHOW backend kamera property karşılaştırması
.venv\Scripts\python.exe tests\manual_camera_settings_test.py

# Kamera -> YOLO crop -> preprocessing -> EasyOCR diagnostik akışı
.venv\Scripts\python.exe tests\manual_plate_crop_debug.py --camera 0
.venv\Scripts\python.exe tests\manual_plate_crop_debug.py --camera 1
```

İkinci araçta `s` crop/preprocessing görüntülerini `captures/ocr_debug/` altına kaydeder ve OCR confidence değerlerini yazdırır; `q` çıkış yapar.

## Offline-first Davranış

İnternet veya Cloud Run geçici olarak erişilemez olduğunda:

- Kamera, YOLO ve EasyOCR yerelde çalışmaya devam eder.
- Vehicle ve AccessLog kayıtları SQLite'a yazılır.
- Yetkilendirme ve access decision yerel son duruma göre üretilir.
- ESP32 bağlıysa bariyer yerel karara göre çalışabilir.
- HTTP hatası OCR/bariyer hot path'ini durdurmaz.
- Sync Manager periyodik olarak yeniden dener.

Yeni kayıt commit edildikten sonra dosya tabanlı bir wakeup sinyali Local → Cloud worker'ını erken uyandırır. Bu hızlı yol başarısız olsa bile 60 saniyelik periyodik sync retry mekanizması yedek olarak devam eder.

## Cloud Mimarisi

```text
Local Security PC
    |
    | HTTPS/443 + Bearer SYNC_API_TOKEN
    v
Cloud Run (FastAPI Web Panel + Sync API)
    |
    | SQLAlchemy / PostgreSQL
    v
Cloud SQL
```

Cloud panelindeki onay bilgileri `/api/sync/approvals` üzerinden yerel bilgisayar tarafından çekilir. Yalnızca yetkilendirme alanları (`status`, `approved_at`, `approved_by`, `notes`) yerelde güncellenir; yerel operasyonel alanlar ve AccessLog kayıtları korunur.

Yerel bilgisayardan Cloud SQL'a doğrudan bağlantı normal production sync yolu değildir. `cloud-sql-proxy.exe` yerel klasörde bulunabilse bile çalışan HTTPS sync mimarisi için gerekli değildir.

Deployment ayrıntıları: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Güvenlik

- Public Cloud Run URL arkasındaki insan paneli uygulama seviyesinde login gerektirir.
- Session cookie 8 saatliktir, `HttpOnly` ve `SameSite=Lax` kullanır; production'da `Secure` olur.
- Login, logout, approve, reject ve delete POST işlemleri CSRF korumalıdır.
- Machine sync endpointleri web session yerine ayrı `SYNC_API_TOKEN` Bearer doğrulaması kullanır.
- Password/token karşılaştırmaları constant-time `secrets.compare_digest()` ile yapılır.
- Auth config eksikse panel sessizce anonim moda geçmez; fail-closed HTTP 503 verir.
- FastAPI `/docs`, `/redoc` ve `/openapi.json` yüzeyleri kapalıdır.
- Gerçek secret'lar Google Secret Manager/runtime environment ile verilmelidir.
- `.env`, credential dosyaları, veritabanları, capture görüntüleri ve model ağırlıkları Git'e commit edilmemelidir.

## Bariyer / ESP32

Mevcut yazılımda:

- `BarrierController` USB Serial üzerinden ESP32'ye bağlanır.
- Python tarafı `OPEN\n` gönderir ve `OK` yanıtını bekler.
- `--barrier-dry-run` gerçek seri port açmadan akışı doğrular.
- `esp32/barrier_led_test.ino`, `OPEN` komutunda GPIO 18 LED'ini üç saniye yakar ve `OK` döndürür.
- Aynı fiziksel olayda bariyer en fazla bir kez tetiklenir.

`start_system.bat` güvenli varsayılan olarak `--barrier-dry-run` kullanır. Gerçek röle/servo/bariyer saha entegrasyonu **Pending hardware integration** durumundadır; saha donanımı, elektriksel izolasyon, güvenlik sensörleri ve acil durdurma doğrulanmadan production fiziksel moda geçilmemelidir.

## Bilinen Sınırlamalar

- OCR hiçbir kamera sisteminde yüzde 100 doğru değildir.
- Uzak, küçük, bulanık, parlak, gölgeli veya yüksek açılı plakalar hatalı okunabilir.
- Mevcut OCR allowlist Latin `A-Z` ve rakamlarla sınırlıdır; Latin dışı alfabeler kapsam dışıdır.
- Kamera konumu, odak, enstantane/exposure, ışık ve crop çözünürlüğü başarıyı doğrudan etkiler.
- Mevcut CLI sayısal kamera indeksi kullanır; RTSP/IP URL doğrudan parametre desteği yoktur.
- Local → Cloud sync şu anda tüm yerel araç/log kümesini taşır; çok büyük veri hacminde batching veya incremental sync gerekebilir.
- Cloud → Local approval isteği yerel araç UUID listesini taşır; büyük filolarda batching gerekebilir.
- Tek yönetici modeli küçük/MVP kurulum içindir; çok kullanıcılı rol tabanlı erişim yoktur.
- Gerçek bariyer/röle saha testi ve fail-safe donanım entegrasyonu tamamlanmamış olabilir.
- Repository aktif Kubernetes manifesti içermez.

## Production Checklist

- [ ] `models/license_plate_detector.pt` mevcut
- [ ] `DATABASE_URL` doğru ortam için ayarlı
- [ ] `CLOUD_SYNC_API_URL` doğru Cloud Run URL'sini gösteriyor
- [ ] `SYNC_API_TOKEN` Secret Manager/runtime üzerinden ayarlı
- [ ] `WEB_ADMIN_USERNAME`, `WEB_ADMIN_PASSWORD`, `WEB_SESSION_SECRET` ayarlı
- [ ] Cloud Run `APP_ENV=production` kullanıyor
- [ ] Docker/Cloud Run health endpoint'i `200 OK`
- [ ] Web login, logout ve CSRF korumalı işlemler çalışıyor
- [ ] Local → Cloud ve Cloud → Local sync doğrulandı
- [ ] Kamera/DroidCam cihaz indeksi doğru
- [ ] Dry-run veya fiziksel bariyer modu bilinçli seçildi
- [ ] ESP32 seri portu ve baud ayarı fiziksel mod için doğru
- [ ] `python -m unittest discover -s tests -v` sonucu başarılı

Operatör adımları için [docs/USAGE.md](docs/USAGE.md) belgesine bakın.
