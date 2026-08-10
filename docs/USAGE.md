# Güvenlik Görevlisi Kullanım Kılavuzu

## Sistemi Açma

1. Kullanılacak USB kamera veya DroidCam bağlantısını hazırlayın.
2. Docker Desktop'ın çalıştığını kontrol edin.
3. Kamera indeksini belirleyin. Varsayılan kamera genellikle `0`, DroidCam çoğu sistemde `1` veya başka bir indeks olabilir.
4. Gerekli runtime environment variable'larının tanımlı olduğunu doğrulayın.
5. Proje klasöründe çalıştırın:

```bat
start_system.bat
```

Script web servisini, Sync Manager'ı ve OCR penceresini başlatır. OCR varsayılan olarak automatic direction ve barrier dry-run modundadır.

Web paneli varsayılan olarak:

```text
http://localhost:8000
```

Cloud Run paneli için deployment tarafından verilen HTTPS URL kullanılır.

## Login

Panel açıldığında yönetici giriş ekranı gösterilir. Deployment yöneticisinin sağladığı `WEB_ADMIN_USERNAME` ve `WEB_ADMIN_PASSWORD` ile giriş yapın.

- Hatalı kullanıcı adı/parola genel bir hata mesajı verir.
- Session yaklaşık 8 saat geçerlidir.
- İşiniz bittiğinde üst menüdeki **Çıkış** düğmesini kullanın.
- Parolayı log, ekran görüntüsü veya Git dosyasında paylaşmayın.

Auth config eksikse panel HTTP 503 ile fail-closed olur. Bu durumda deployment/runtime secret yapılandırmasını sistem yöneticisi kontrol etmelidir.

## Dashboard

Dashboard şunları gösterir:

- İçerideki araç sayısı
- Onay bekleyen araç sayısı
- Approved, rejected ve inactive sayaçları
- İlk beş pending araç
- Son on erişim kaydı

Presence bilgisi yalnız başarılı `ALLOW` giriş/çıkış kayıtlarından türetilir.

## Pending Araç İşlemleri

İlk kez kararlı şekilde okunan plaka `pending` olarak kaydedilir. Bariyer otomatik açılmaz.

1. **Pending** sayfasına gidin.
2. Plaka ve görülme zamanını kontrol edin.
3. Yetkili araç için **Onayla** seçin.
4. Yetkisiz araç için isteğe bağlı gerekçe girip **Reddet** seçin.

Durumlar:

- `pending`: onay bekliyor
- `approved`: bir sonraki geçişte `ALLOW`
- `rejected`: geçiş `DENY`
- `inactive`: geçiş `DENY`

Araç kameranın önündeyken uzaktan onaylansa bile aynı fiziksel olay içinde bariyer sonradan açılmaz. Araç ayrılıp yeni fiziksel olayla döndüğünde güncel authorization kararı uygulanır.

## Araçlar Sayfası

**Araçlar** sayfasında tüm kayıtlar veya status filtresi görüntülenebilir. Tablo şunları içerir:

- Plaka ve yetki durumu
- İçeride/dışarıda presence durumu
- Son giriş/çıkış hareketi
- İlk ve son görülme
- Onay zamanı ve onaylayan

**Sil** işlemi Vehicle kaydını kaldırır; geçmiş AccessLog kayıtları korunur ve araç bağlantısı boşaltılır. Silme işlemini yalnız gerçekten gerekli olduğunda kullanın.

Approve, reject, delete ve logout formları CSRF korumalıdır. Session süresi dolarsa tekrar login olmanız gerekir.

## Giriş/Çıkış Kayıtları

**Erişim Kayıtları** sayfası son 100 geçiş denemesini gösterir:

- Normalize plaka
- `entry`, `exit` veya `unknown` yönü
- `allow`, `wait_for_approval`, `deny` kararı
- OCR confidence
- Kamera adı
- Ret gerekçesi
- Tarih/saat

Automatic direction, aracın en son başarılı `ALLOW` kaydına göre ENTRY/EXIT arasında ilerler. Pending veya deny kayıtları presence durumunu değiştirmez.

## Telefon Kamerası / DroidCam

DroidCam telefon kamerasını Windows'ta sanal kamera olarak gösterir. Örneğin index `1` ise:

```bat
set CAMERA_SOURCE=1
start_system.bat
```

Laptop kamerası index `0` ise:

```bat
set CAMERA_SOURCE=0
start_system.bat
```

İndeks cihazdan cihaza değişir. Yanlış indeks seçilirse kamera açılamaz veya farklı kamera açılır.

Backend/property testi:

```bat
.venv\Scripts\python.exe tests\manual_camera_settings_test.py
```

YOLO crop ve OCR preprocessing diagnostik testi:

```bat
.venv\Scripts\python.exe tests\manual_plate_crop_debug.py --camera 1
```

Diagnostik araçlar normal production çalışmasının parçası değildir.

## Bariyer Modu

`start_system.bat` varsayılan olarak `--barrier-dry-run` ile başlar. Bu modda onaylı olayda terminale `OPEN` yazılır fakat seri port kullanılmaz.

Fiziksel ESP32 testi için OCR CLI doğrudan uygun seri portla başlatılabilir:

```bat
.venv\Scripts\python.exe src\ocr_reader.py --camera 0 --direction auto --camera-name test_giris --esp32-port COM5 --esp32-baud 115200
```

`COM5` örnektir; gerçek portu Windows Device Manager'dan doğrulayın. Gerçek röle/bariyer saha entegrasyonu **Pending hardware integration** durumundadır. Yetkili teknik onay olmadan dry-run seçeneğini kaldırmayın.

## İnternet Kesilirse

İnternet kesintisi sırasında:

- Kamera ve OCR yerelde çalışmaya devam eder.
- Vehicle ve AccessLog SQLite'a kaydedilir.
- Yerel approved/pending/rejected/inactive kararı çalışır.
- ESP32 bağlı ve fiziksel mod etkinse yerel karara göre bariyer çalışabilir.
- Cloud panel yeni kayıtları geçici olarak göremez.
- Sync Manager bağlantı döndüğünde otomatik tekrar dener.

Cloud'da verilen yeni bir approval kararı, bağlantı geri gelip approval sync tamamlanana kadar yerelde görünmez. Bu sırada son bilinen yerel authorization durumu kullanılır.

## Sistemi Kapatma

Proje klasöründe:

```bat
stop_system.bat
```

Script Docker web servisini durdurur ve OCR ile Sync Manager pencerelerini kapatır.

## Günlük Operasyon Kontrolü

- Kamera görüntüsü açık ve plaka bölgesi net mi?
- Doğru kamera indeksi kullanılıyor mu?
- OCR terminalinde beklenmeyen sürekli hata var mı?
- Sync Manager penceresi çalışıyor mu?
- Web panel `/health` ve login erişilebilir mi?
- Pending araçlar düzenli değerlendiriliyor mu?
- Sistem dry-run mı, fiziksel bariyer modu mu?
- Fiziksel moddaysa ESP32 seri bağlantısı ve güvenlik sensörleri doğrulandı mı?
