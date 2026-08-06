# Plaka Tanıma ve Otomatik Bariyer Kontrol Sistemi

Bu proje, Python tabanlı görüntü işleme, yapay zeka (YOLO & OCR) ve mikrodenetleyici (ESP32) entegrasyonu ile otomatik plaka tanıma ve bariyer kontrol mekanizması sunmaktadır.

## 🚀 Proje Mimarisi ve Çalışma Mantığı

1. **Görüntü Yakalama (OpenCV):** USB veya IP kameralardan canlı görüntü akışı alınır.
2. **Plaka Tespiti (YOLO):** Yerel güvenlik bilgisayarında çalışan YOLO modeli, görüntüdeki araç plaka bölgesini tespit eder.
3. **Plaka Okuma (OCR):** Tespit edilen plaka alanındaki harf ve rakamlar yerel OCR modeli ile metne dönüştürülür.
4. **Karar Mekanizması & Otomasyon:**
   - **Kayıtlı/Yetkili Plaka:** Sistem otomatik olarak ESP32 mikrodenetleyicisine sinyal gönderir ve bariyeri açar.
   - **İlk Kez Görülen Plaka:** Güvenlik görevlisinin web paneli üzerinden onayına sunulur.
5. **Yönetim Paneli & Backend (FastAPI):** Güvenlik görevlilerinin canlı giriş/çıkış kayıtlarını takip edebileceği, onay verebileceği web arayüzü ve REST API.
6. **Veri Tabanı:** İlk aşamada hafif ve yerel **SQLite**, ilerleyen aşamada ölçeklenebilir **PostgreSQL**.
7. **Donanım Kontrolü (ESP-WROOM-32):** Servo motor, röle ve fiziki sensörlerin kontrolünden tek başına sorumludur.
8. **Dağıtım ve Bulut (Gelecek Aşama):** Docker, Docker Compose, Kubernetes (GKE) ve Google Cloud yedekleme/merkezi kayıt entegrasyonu.

## 📂 Klasör Yapısı

```
plaka-tanima-sistemi/
├── src/            # Python kaynak kodları (Core, API, Image Processing vb.)
├── tests/          # Birim (unit) ve entegrasyon testleri
├── models/         # YOLO ve OCR model dosyaları / ağırlıkları
├── data/           # SQLite veri tabanı dosyası ve yerel veri saklama alanı
├── captures/       # Kamera tarafından anlık çekilen plaka/araç fotoğrafları
├── templates/      # FastAPI Jinja2 HTML şablonları (Güvenlik paneli)
├── static/         # Web arayüzü CSS, JavaScript ve görsel varlıkları
├── esp32/          # ESP-WROOM-32 mikrodenetleyici C++/Arduino kodları
├── docker/         # Dockerfile ve container yapılandırma dosyaları (İleriki aşama)
├── kubernetes/     # Kubernetes (K8s) manifest dosyaları (İleriki aşama)
├── docs/           # Proje dokümantasyonu ve mimari şemalar
├── .env.example    # Çevre değişkenleri örnek yapılandırma dosyası
├── .gitignore      # Git tarafından takip edilmeyecek dosya kuralları
├── AGENTS.md       # Yapay zeka ajanları ve geliştirici yönergeleri
├── README.md       # Proje genel tanıtım ve mimari belgesi
└── requirements.txt# Python kütüphane bağımlılıkları listesi
```
