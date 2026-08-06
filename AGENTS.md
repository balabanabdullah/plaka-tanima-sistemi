# Geliştirici ve AI Ajan Yönergeleri (AGENTS.md)

Bu dosya, Plaka Tanıma ve Otomatik Bariyer Kontrol Sistemi projesinde kod geliştiren kişiler ve yapay zeka ajanları için uyulması gereken temel kuralları tanımlar.

## 📌 Temel Kurallar ve Prensipler

1. **Başlangıç Seviyesi Python Kodlama:**
   - Kodlar, Python'a yeni başlayan bir öğrencinin rahatlıkla anlayabileceği sadelikte, temiz ve açıklayıcı yorum satırlarıyla yazılmalıdır.
   - Karmaşık, anlaşılması zor metaprogramlama veya aşırı soyutlanmış tasarım kalıplarından kaçınılmalıdır.

2. **Sorumlulukların Ayrılması (Separation of Concerns):**
   - **OpenCV & YOLO & OCR:** Güvenlik bilgisayarında yerel çalışır, görüntü işleme ve plaka okumayı üstlenir.
   - **FastAPI:** Backend servisleri, web paneli ve veritabanı işlemlerini yönetir.
   - **ESP32:** Yalnızca donanım kontrolünden (servo, röle, sensör) sorumludur, ağır veri işleme yapmaz.

3. **Aşamalı Geliştirme Yaklaşımı:**
   - Aşama 1: Proje skeleton'ı (Yalnızca dosya/klasör yapısı - Kodsuz).
   - Aşama 2: Modül bazlı Python geliştirmeleri (OpenCV, YOLO, OCR, FastAPI, SQLite).
   - Aşama 3: ESP32 donanım haberleşmesi.
   - Aşama 4: Docker & Docker Compose containerization.
   - Aşama 5: Kubernetes & Google Cloud entegrasyonu.

4. **Güvenlik ve Konfigürasyon:**
   - Şifreler, port bilgileri ve sistem yolları kod içinde sabit (hardcoded) yazılmamalı, `.env` dosyası üzerinden okunmalıdır.
