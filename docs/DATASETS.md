# Plaka Tespit Veri Seti Dokümantasyonu

## Veri Setinin Amacı

Bu veri seti, otomatik bariyer kontrol sisteminde yer alan YOLO modelinin araç plakalarını ülke, renk, boyut, açı ve ortam şartlarından bağımsız olarak yüksek doğrulukla tespit edebilmesi amacıyla oluşturulmaktadır.

## Hedef Sınıf: `license_plate`

Ülke tespiti ve plaka formatı doğrulaması sonraki OCR aşamasına bırakılmıştır. YOLO tespit modelinde yalnızca tek sınıf kullanılmaktadır:

- **Sınıf Adı:** `license_plate`
- **Sınıf ID:** `0`

Tüm plakalar (Türk, yabancı, kare, standart vb.) aynı `license_plate` etiketi altında toplanacaktır.

## Desteklenmesi Hedeflenen Ülke ve Görüntü Çeşitliliği

Modelin saha şartlarında yüksek başarıyla çalışabilmesi için veri setinde aşağıdaki çeşitlilik hedeflenmektedir:

- Türk ve yabancı plakalar
- Latin, Arap ve Kiril karakterleri
- Tek ve çift satırlı plakalar
- Farklı arka plan renkleri (beyaz, sarı, siyah, yeşil vb.)
- Otomobil, motosiklet, kamyon ve otobüs plakaları
- Ön ve arka plakalar
- Gündüz, gece ve farklı hava şartları
- Eğik, uzak, bulanık ve kısmen kapalı plakalar

## Veri Seti Lisanslarının Nasıl Kaydedileceği

Projeye eklenen tüm açık kaynak veya üçüncü taraf veri setlerinin lisans hakları (CC BY 4.0, MIT, ODbL vb.), telif şartlarına tam uyum sağlamak adına bu doküman altındaki lisans tablosunda kayıt altına alınacaktır.

## İleride Eklenecek Veri Setleri İçin Kaynak, Sürüm ve Lisans Tablosu

| Veri Seti Adı | Kaynak / URL | Sürüm | Lisans | Açıklama |
| :--- | :--- | :--- | :--- | :--- |
| *Örnek Dataset 1* | *Roboflow / Kaggle* | *v1.0* | *CC BY 4.0* | *Genel araç ve plaka görselleri* |
| *Örnek Dataset 2* | *Open Images V7* | *v7* | *CC BY 2.0* | *Çeşitli ülke ve gece/gündüz görselleri* |
