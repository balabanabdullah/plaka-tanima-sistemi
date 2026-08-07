import re
import cv2
import time
import numpy
from pathlib import Path
from collections import deque, Counter

# EasyOCR'u import ediyoruz; kurulu değilse hata mesajı verecek
try:
    import easyocr
except ImportError:
    easyocr = None

from ultralytics import YOLO

# Veritabanı ve servis katmanı
# Bu modüller src/ altında bulunur; python src/ocr_reader.py ile çalıştırılır
from database import init_db, get_session
from plate_service import (
    normalize_plate,
    get_or_create_vehicle,
    get_vehicle_by_plate,
    evaluate_access,
    create_access_log,
    should_log,
)
from models import AccessDirection
from sqlalchemy.exc import SQLAlchemyError

# Proje kök dizini ve varsayılan model yolu
# Path(__file__) -> bu dosyanın konumu (src/ocr_reader.py)
# .parent -> src/ klasörü
# .parent -> proje kökü
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_PATH = PROJECT_ROOT / "models" / "license_plate_detector.pt"

# OCR güven eşiği: bu değerin altındaki okumalar geçersiz sayılır
OCR_CONFIDENCE_THRESHOLD = 0.45

# OCR kaç karede bir çalışacak (CPU yükünü azaltmak için)
OCR_FRAME_INTERVAL = 8

# Veritabanı durum yenileme sıklığı (saniye)
DB_STATUS_REFRESH_SECONDS = 1.0

# Yeni plaka adayı doğrulama tekrar sayısı
NEW_PLATE_CONFIRMATIONS = 3

# OCR için izin verilen karakter listesi (yalnızca Latin harf ve rakam)
OCR_ALLOWLIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# Minimum plaka kutusu boyutları (piksel)
MIN_PLATE_WIDTH = 120
MIN_PLATE_HEIGHT = 35

# Minimum keskinlik eşiği (Laplacian varyansı)
MIN_SHARPNESS = 60.0

# Hata ayıklama / Debug modu
DEBUG_OCR = False

# Kararlılık geçmişi: son kaç OCR sonucu saklanacak
OCR_HISTORY_SIZE = 10

# Kararlı kabul için aynı metnin kaç kez tekrar etmesi gerektiği
OCR_MIN_REPETITIONS = 3

# Geçerli plaka uzunluk aralığı (karakter sayısı)
MIN_PLATE_LENGTH = 5
MAX_PLATE_LENGTH = 12

# Kaç başarısız OCR denemesinden sonra geçmiş temizleneceği
OCR_STALE_LIMIT = 15

# İki satırlı plaka desteği: aynı satırda kabul için dikey merkez toleransı (piksel)
OCR_LINE_Y_TOLERANCE = 20


def load_plate_model(model_path: Path | str = DEFAULT_MODEL_PATH) -> YOLO | None:
    """
    Plaka tespit YOLO modelini yükler.
    Model dosyası bulunamazsa Türkçe hata mesajı verir ve None döner.

    Parametreler:
        model_path (Path | str): YOLO model dosyasının yolu

    Döndürür:
        YOLO | None: Yüklenmiş model nesnesi veya None
    """
    path = Path(model_path).resolve()

    # Model dosyası var mı kontrol et
    if not path.exists():
        print("Hata: Plaka tespit model dosyası bulunamadı!")
        print(f"Beklenen dosya yolu: {path}")
        print("Lütfen 'license_plate_detector.pt' dosyasını 'models/' klasörüne ekleyin.")
        return None

    try:
        print(f"YOLO plaka tespit modeli yükleniyor: {path.name} ...")
        model = YOLO(str(path))
        print("YOLO modeli başarıyla yüklendi.")
        return model
    except Exception as e:
        print(f"Hata: YOLO modeli yüklenirken sorun oluştu: {e}")
        return None


def load_ocr_reader() -> "easyocr.Reader | None":
    """
    EasyOCR Reader nesnesini oluşturur (yalnızca Latin alfabe desteği ile).
    İlk çalıştırmada gerekli OCR model dosyaları otomatik indirilebilir.

    Döndürür:
        easyocr.Reader | None: Yüklenmiş Reader nesnesi veya None
    """
    # easyocr kurulu değilse Türkçe hata ver
    if easyocr is None:
        print("Hata: 'easyocr' kütüphanesi kurulu değil!")
        print("Lütfen şu komutu çalıştırın: pip install easyocr")
        return None

    try:
        print("EasyOCR Reader yükleniyor (ilk çalıştırmada model indirilebilir, lütfen bekleyin) ...")
        reader = easyocr.Reader(["en"], gpu=False)
        print("EasyOCR Reader başarıyla yüklendi.")
        return reader
    except Exception as e:
        print(f"Hata: EasyOCR Reader oluşturulurken sorun oluştu: {e}")
        return None


def preprocess_plate(plate_crop: numpy.ndarray) -> numpy.ndarray:
    """
    Kırpılan plaka görüntüsüne temel ön işleme uygular.
    Gri tonlama, yeniden boyutlandırma ve CLAHE kontrast iyileştirme uygulanır.

    Parametreler:
        plate_crop (numpy.ndarray): Kırpılmış plaka görüntüsü (BGR)

    Döndürür:
        numpy.ndarray: Ön işlenmiş gri tonlamalı görüntü
    """
    # BGR görüntüyü gri tona çevir
    gray = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2GRAY)

    # Plaka yüksekliği çok küçükse en-boy oranını koruyarak büyüt
    # Minimum 100 piksel yüksekliği iki satırlı plakalar için daha iyi OCR doğruluğu sağlar
    # INTER_CUBIC: büyütmede daha keskin kenarlar üretir (INTER_LINEAR'a göre daha iyi)
    min_height = 100
    h, w = gray.shape
    if h < min_height:
        scale = min_height / h
        new_w = int(w * scale)
        gray = cv2.resize(gray, (new_w, min_height), interpolation=cv2.INTER_CUBIC)

    # CLAHE ile kontrast iyileştirme uygula
    # Düşük ışık veya soluk plakalarda karakterlerin öne çıkmasını sağlar
    # clipLimit: kontrast sınırı, tileGridSize: bölgesel analiz için ızgara boyutu
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray = clahe.apply(gray)

    return gray


def read_plate_text(reader: "easyocr.Reader", plate_crop: numpy.ndarray) -> tuple[str, float]:
    """
    Ön işlenmiş plaka görüntüsünden OCR ile metin okur.
    Tek satırlı ve iki satırlı plakaları destekler.
    Parçalar önce satırlara ayrılır, satırlar yukarıdan aşağıya,
    her satır içindeki parçalar soldan sağa sıralanarak birleştirilir.

    Parametreler:
        reader (easyocr.Reader): Yüklenmiş EasyOCR Reader nesnesi
        plate_crop (numpy.ndarray): Ön işlenmiş plaka görüntüsü

    Döndürür:
        tuple[str, float]: (okunan metin, ortalama güven değeri)
    """
    if plate_crop is None or plate_crop.size == 0:
        return "OKUNAMADI", 0.0

    try:
        # EasyOCR ile metin tespit et; yalnızca izin verilen karakterleri kullan
        results = reader.readtext(plate_crop, allowlist=OCR_ALLOWLIST)
    except Exception:
        return "OKUNAMADI", 0.0

    # Hiç sonuç yoksa
    if not results:
        return "OKUNAMADI", 0.0

    # Güven eşiğini geçen parçaları filtrele ve merkez koordinatlarını hesapla
    # Her parça: (center_x, center_y, normalize_edilmis_metin, guven)
    gecerli_parcalar = []
    for bbox, text, confidence in results:
        if confidence < OCR_CONFIDENCE_THRESHOLD:
            continue
        temiz = normalize_plate_text(text)
        if not temiz:
            continue

        # bbox: [[sol_üst_x, sol_üst_y], [sağ_üst_x, sağ_üst_y],
        #        [sağ_alt_x, sağ_alt_y], [sol_alt_x, sol_alt_y]]
        # Kutunun yatay ve dikey merkezini hesapla
        tum_x = [nokta[0] for nokta in bbox]
        tum_y = [nokta[1] for nokta in bbox]
        center_x = sum(tum_x) / len(tum_x)
        center_y = sum(tum_y) / len(tum_y)

        gecerli_parcalar.append((center_x, center_y, temiz, confidence))

    # Hiç geçerli parça bulunamadıysa
    if not gecerli_parcalar:
        return "OKUNAMADI", 0.0

    # ---------------------------------------------------------------
    # İKİ SATIRLI PLAKA GRUPLAMA MANTIĞI
    # ---------------------------------------------------------------
    # Parçaları dikey merkeze (center_y) göre satırlara ayır.
    # Bir parçanın center_y değeri mevcut satırın ortalama center_y
    # değerine OCR_LINE_Y_TOLERANCE piksel içindeyse aynı satıra eklenir;
    # aksi hâlde yeni bir satır başlatılır.
    #
    # Örnek: PN 628BE şeklinde iki satırlı plaka
    #   PN  -> center_y ≈ 15  → Satır 1
    #   628 -> center_y ≈ 45  → 15'ten 30 piksel uzak → Satır 2
    #   BE  -> center_y ≈ 47  → 45'e 2 piksel yakın  → Satır 2'ye eklenir
    #   Birleşim: PN + 628BE = PN628BE
    # ---------------------------------------------------------------

    # Parçaları önce dikey merkeze göre sırala (yukarıdan aşağıya)
    gecerli_parcalar.sort(key=lambda p: p[1])

    # Her satır: [(center_x, center_y, metin, guven), ...] listesi
    satirlar: list[list[tuple]] = []

    for parca in gecerli_parcalar:
        center_x, center_y, metin, guven = parca
        yerlestirildi = False

        for satir in satirlar:
            # Mevcut satırın ortalama dikey merkezini hesapla
            satir_center_y = sum(s[1] for s in satir) / len(satir)

            # Tolerans içindeyse bu satıra ekle
            if abs(center_y - satir_center_y) <= OCR_LINE_Y_TOLERANCE:
                satir.append(parca)
                yerlestirildi = True
                break

        # Hiçbir satıra uymadıysa yeni satır oluştur
        if not yerlestirildi:
            satirlar.append([parca])

    # Satırları yukarıdan aşağıya sırala (her satırın ortalama center_y değerine göre)
    satirlar.sort(key=lambda satir: sum(s[1] for s in satir) / len(satir))

    # Her satır içindeki parçaları soldan sağa sırala ve metni birleştir
    metin_parcalari = []
    toplam_guven = 0.0
    parca_sayisi = 0

    for satir in satirlar:
        # Satır içi soldan sağa sıralama (center_x'e göre)
        satir.sort(key=lambda p: p[0])
        for _, _, metin, guven in satir:
            metin_parcalari.append(metin)
            toplam_guven += guven
            parca_sayisi += 1

    # Tüm satırlardaki metinleri boşluksuz birleştir
    birlesik_metin = "".join(metin_parcalari)
    ortalama_guven = toplam_guven / parca_sayisi

    # Plaka uzunluk kontrolü: çok kısa veya çok uzun metinler geçersiz
    if len(birlesik_metin) < MIN_PLATE_LENGTH or len(birlesik_metin) > MAX_PLATE_LENGTH:
        return "OKUNAMADI", 0.0

    return birlesik_metin, round(ortalama_guven, 2)


def normalize_plate_text(text: str) -> str:
    """
    OCR çıktısını temizler: büyük harfe çevirir, boşlukları ve
    Latin harf/rakam dışındaki karakterleri kaldırır.

    Parametreler:
        text (str): Ham OCR metni

    Döndürür:
        str: Temizlenmiş plaka metni
    """
    # Büyük harfe çevir
    text = text.upper()

    # Yalnızca Latin harf ve rakamları tut
    text = re.sub(r"[^A-Z0-9]", "", text)

    return text


def process_plate_access(
    plate_text: str,
    ocr_confidence: float,
    direction: AccessDirection = AccessDirection.unknown,
    source_camera: str = "cam_0",
) -> "tuple[str, str, str] | None":
    """
    Kararlı OCR metnini veritabanına kaydeder ve erişim kararı üretir.

    Adımlar:
    1. normalize_plate() ile plakayı temizler.
    2. get_or_create_vehicle() ile araç kaydını bulur veya oluşturur.
    3. evaluate_access() ile durum → karar üretir.
    4. should_log() ile cooldown kontrolü yapar.
    5. Uygunsa create_access_log() ile erişim kaydı oluşturur.

    Parametreler:
        plate_text:     Kararlı OCR metni.
        ocr_confidence: OCR güven değeri (0.0–1.0).
        direction:      Giriş / çıkış / bilinmiyor.
        source_camera:  Kamera kimliği.

    Döndürür:
        tuple[str, str, str]: (normalized_plate, status, decision)
        None: Hata durumunda.
    """
    # 1. Plakayı normalize et; boş sonuç ValueError üretir
    try:
        normalized = normalize_plate(plate_text)
    except ValueError as e:
        print(f"Uyarı: Plaka normalize edilemedi: {e}")
        return None

    # 2. Veritabanı işlemleri
    try:
        with get_session() as session:
            # Araç kaydını bul veya oluştur (UNIQUE korumalı)
            vehicle, _ = get_or_create_vehicle(session, plate_text, normalized)

            # Durum bilgisine göre erişim kararı üret
            decision = evaluate_access(vehicle)

            # Cooldown kontrolü: aynı plaka + yön için yakın zamanda log varsa atla
            if should_log(session, normalized, direction):
                create_access_log(
                    session=session,
                    vehicle=vehicle,
                    plate_text=plate_text,
                    normalized_plate=normalized,
                    direction=direction,
                    decision=decision,
                    ocr_confidence=ocr_confidence,
                    source_camera=source_camera,
                )

            return normalized, vehicle.status.value, decision.value

    except SQLAlchemyError as e:
        print(f"Hata: Veritabanı işlemi sırasında sorun oluştu: {e}")
        return None
    except Exception as e:
        print(f"Hata: Beklenmeyen bir sorun oluştu: {e}")
        return None


def refresh_plate_access_status(plate_text: str) -> "tuple[str, str] | None":
    """
    Görüntüdeki kararlı plakanın veritabanındaki güncel yetki durumunu (status)
    ve geçiş kararını (decision) salt okunur olarak sorgular.
    Herhangi bir access_log kaydı OLUŞTURMAZ, last_seen_at GÜNCELLEMEZ.

    Parametreler:
        plate_text (str): Kararlı plaka metni

    Döndürür:
        tuple[str, str] | None: (status_str, decision_str) veya None
    """
    try:
        normalized = normalize_plate(plate_text)
    except ValueError:
        return None

    try:
        with get_session() as session:
            vehicle = get_vehicle_by_plate(session, normalized)
            if vehicle is None:
                return None
            decision = evaluate_access(vehicle)
            return vehicle.status.value, decision.value
    except Exception as e:
        print(f"Uyarı: Status refresh sırasında sorun oluştu: {e}")
        return None


def run_plate_ocr(camera_source: int = 0) -> None:
    """
    Kameradan canlı görüntü alır, YOLO ile plaka tespiti yapar,
    her 5 karede bir EasyOCR ile plaka metnini okur ve ekranda gösterir.
    Kararlı plakalar veritabanına kaydedilir ve erişim kararı üretilir.

    Parametreler:
        camera_source (int): Kamera kaynağı indeksi (Varsayılan: 0)
    """
    # 1. Veritabanını başlat (kamera açılmadan önce zorunlu)
    try:
        init_db()
    except Exception as e:
        print(f"Hata: Veritabanı başlatılamadı: {e}")
        print("Uygulama sonlandırılıyor.")
        return

    # 2. YOLO modeli ve EasyOCR Reader'ı kamera açılmadan önce bir kez yükle
    yolo_model = load_plate_model(DEFAULT_MODEL_PATH)
    if yolo_model is None:
        print("YOLO modeli yüklenemedi. Program sonlandırılıyor.")
        return

    ocr_reader = load_ocr_reader()
    if ocr_reader is None:
        print("EasyOCR Reader oluşturulamadı. Program sonlandırılıyor.")
        return

    # 2. Kamerayı başlat
    cap = cv2.VideoCapture(camera_source)

    # OCR sayacı ve durum değişkenleri
    kare_sayaci = 0
    son_metin = "OKUNUYOR..."     # Ekranda gösterilen metin
    son_guven = 0.0               # Ekranda gösterilen güven değeri
    son_yazdirilan_metin = ""     # Terminale en son yazdırılan metin
    basarisiz_sayisi = 0          # Arka arkaya başarısız OCR denemesi sayısı

    # En yüksek güvenle okunan sonucu saklar
    # Düşük güvenli yeni okumalar bu sonucu değiştiremez
    en_iyi_metin = "OKUNAMADI"
    en_iyi_guven = 0.0

    # Veritabanı entegrasyonu için durum değişkenleri
    son_veritabani_metin = ""    # DB'ye en son gönderilen kararlı plaka metni
    son_db_durum = ""            # Ekranda gösterilecek araç durumu (PENDING, APPROVED, vb.)
    son_db_karar = ""            # Ekranda gösterilecek geçiş kararı (ALLOW, WAIT, DENY)
    son_refresh_zamani = 0.0     # Son DB status refresh yapılma zamanı (time.monotonic)

    # Yeni plaka adayı takibi (eski plakadan hızlı geçiş için)
    aday_metin = ""
    aday_tekrar = 0

    # OCR geçmişi: son OCR_HISTORY_SIZE adet (metin, güven) çiftini saklar
    # deque, sınıra ulaşınca en eski kaydı otomatik siler
    ocr_gecmisi: deque[tuple[str, float]] = deque(maxlen=OCR_HISTORY_SIZE)

    try:
        if not cap.isOpened():
            print(f"Hata: {camera_source} indeksli kamera açılamadı! Bağlantıyı kontrol edin.")
            return

        print("Plaka OCR başlatıldı. Çıkmak için pencere üzerindeyken 'q' tuşuna basın.")

        while True:
            ret, frame = cap.read()

            if not ret:
                print("Hata: Kameradan görüntü alınamadı!")
                break

            h_frame, w_frame = frame.shape[:2]
            kare_sayaci += 1

            # Periyodik DB Status Refresh: Kararlı plaka ekranda dururken web panelden yapılan
            # approve/reject/delete gibi değişiklikleri anlık olarak kontrol et (1 saniyede bir)
            simdi_mono = time.monotonic()
            if son_veritabani_metin and (simdi_mono - son_refresh_zamani >= DB_STATUS_REFRESH_SECONDS):
                guncel_sonuc = refresh_plate_access_status(son_veritabani_metin)
                if guncel_sonuc is not None:
                    yeni_durum, yeni_karar = guncel_sonuc
                    yeni_durum_up = yeni_durum.upper()
                    yeni_karar_up = yeni_karar.upper()

                    # Durum veya karar değiştiyse ekrana yansıt
                    if yeni_durum_up != son_db_durum or yeni_karar_up != son_db_karar:
                        print(f"[STATUS GÜNCELLENDİ] Plaka: {son_veritabani_metin} | Durum: {yeni_durum_up} | Karar: {yeni_karar_up}")
                        son_db_durum = yeni_durum_up
                        son_db_karar = yeni_karar_up
                else:
                    # Web panelden araç silinmiş: DB durumu ve kaydını temizle
                    # Araç tekrar okunduğunda yeni araç gibi (pending) işlenecektir
                    if DEBUG_OCR:
                        print(f"[STATUS GÜNCELLENDİ] Araç silindi: {son_veritabani_metin}")
                    son_db_durum = ""
                    son_db_karar = ""
                    son_veritabani_metin = ""

                son_refresh_zamani = simdi_mono

            # 3. YOLO ile plaka tespiti yap
            results = yolo_model(frame, conf=0.40, verbose=False, device="cpu")
            kutular = results[0].boxes

            # 4. Her tespit kutusu için işlem yap
            for kutu in kutular:
                # Koordinatları al ve görüntü sınırları içine sıkıştır
                x1, y1, x2, y2 = map(int, kutu.xyxy[0])
                x1 = max(0, min(x1, w_frame - 1))
                y1 = max(0, min(y1, h_frame - 1))
                x2 = max(0, min(x2, w_frame - 1))
                y2 = max(0, min(y2, h_frame - 1))

                # Geçersiz veya boş alan kontrolü
                if x2 <= x1 or y2 <= y1:
                    continue

                # 5. Belirlenen kare aralığında (OCR_FRAME_INTERVAL) OCR çalıştır
                if kare_sayaci % OCR_FRAME_INTERVAL == 0:
                    crop_w = x2 - x1
                    crop_h = y2 - y1

                    # Minimum boyut kontrolü: çok küçük plaka kutularında OCR çalıştırma
                    if crop_w < MIN_PLATE_WIDTH or crop_h < MIN_PLATE_HEIGHT:
                        if DEBUG_OCR:
                            print(f"[DEBUG OCR] Boyut yetersiz atlandı: {crop_w}x{crop_h} (Min: {MIN_PLATE_WIDTH}x{MIN_PLATE_HEIGHT})")
                        continue

                    plate_crop = frame[y1:y2, x1:x2]

                    # Keskinlik (sharpness) kontrolü: Laplacian varyansı
                    gray_crop = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2GRAY)
                    sharpness = float(cv2.Laplacian(gray_crop, cv2.CV_64F).var())

                    if sharpness < MIN_SHARPNESS:
                        if DEBUG_OCR:
                            print(f"[DEBUG OCR] Bulanık plaka atlandı: Sharpness={sharpness:.1f} (Min: {MIN_SHARPNESS})")
                        continue

                    islenmis = preprocess_plate(plate_crop)
                    okunan, guven = read_plate_text(ocr_reader, islenmis)

                    if DEBUG_OCR:
                        print(f"[DEBUG OCR] Boyut: {crop_w}x{crop_h} | Keskinlik: {sharpness:.1f} | OCR: '{okunan}' | Güven: {guven}")

                    if okunan != "OKUNAMADI":
                        # Yeni plaka adayı takibi:
                        # Mevcut kararlı bir veritabanı plakası varken farklı ve geçerli bir plaka okunursa:
                        if son_veritabani_metin and okunan != son_veritabani_metin:
                            if okunan == aday_metin:
                                aday_tekrar += 1
                            else:
                                aday_metin = okunan
                                aday_tekrar = 1

                            # Farklı aday NEW_PLATE_CONFIRMATIONS kadar doğrulandıysa yeni araca geç!
                            if aday_tekrar >= NEW_PLATE_CONFIRMATIONS:
                                if DEBUG_OCR:
                                    print(f"[YENİ PLAKA ALGILANDI] Eski: {son_veritabani_metin} -> Yeni Aday: {aday_metin}")

                                # Tüm eski state'i temizle, yeni plakaya temiz başlangıç sağla
                                ocr_gecmisi.clear()
                                basarisiz_sayisi = 0
                                en_iyi_metin = "OKUNAMADI"
                                en_iyi_guven = 0.0
                                son_metin = "OKUNUYOR..."
                                son_guven = 0.0
                                son_yazdirilan_metin = ""
                                son_veritabani_metin = ""
                                son_db_durum = ""
                                son_db_karar = ""
                                son_refresh_zamani = 0.0
                                aday_metin = ""
                                aday_tekrar = 0
                        else:
                            # Okunan plaka mevcut kararlı plaka ile aynı ise aday takibini sıfırla
                            aday_metin = ""
                            aday_tekrar = 0

                        # Başarılı okuma: geçmişe ekle, başarısız sayacını sıfırla
                        ocr_gecmisi.append((okunan, guven))
                        basarisiz_sayisi = 0

                        # En iyi sonucu güncelle:
                        # Yalnızca yeni güven mevcut en iyi güvenden 0.02 daha yüksekse kabul et
                        if guven >= en_iyi_guven + 0.02:
                            en_iyi_metin = okunan
                            en_iyi_guven = guven

                    else:
                        # Başarısız okuma: sayacı artır
                        basarisiz_sayisi += 1

                    # Arka arkaya çok fazla başarısız okuma varsa geçmişi VE
                    # en iyi sonucu temizle (plaka görüş alanından çıkmış olabilir)
                    if basarisiz_sayisi >= OCR_STALE_LIMIT:
                        ocr_gecmisi.clear()
                        basarisiz_sayisi = 0
                        en_iyi_metin = "OKUNAMADI"
                        en_iyi_guven = 0.0
                        son_metin = "OKUNUYOR..."
                        son_guven = 0.0
                        # Plaka kayboldu: tüm izleme değişkenlerini sıfırla
                        # Araç yeniden görüldüğünde sıfırdan işlenebilsin
                        son_yazdirilan_metin = ""
                        son_veritabani_metin = ""
                        son_db_durum = ""
                        son_db_karar = ""
                        son_refresh_zamani = 0.0
                        aday_metin = ""
                        aday_tekrar = 0

                    # 6. Geçmişteki metinleri say ve kararlı sonucu belirle
                    if len(ocr_gecmisi) > 0:
                        # Her metnin kaç kez geçtiğini say
                        metin_sayaci = Counter(metin for metin, _ in ocr_gecmisi)

                        # En sık görülen metni ve kaç kez göründüğünü al
                        en_sik_metin, tekrar_sayisi = metin_sayaci.most_common(1)[0]

                        # Yeterli tekrar varsa kararlı kabul et
                        if tekrar_sayisi >= OCR_MIN_REPETITIONS:
                            # Kararlı metin ile en iyi sonucu karşılaştır:
                            # Ekranda her zaman daha yüksek güvenli olanı göster
                            if en_iyi_metin != "OKUNAMADI":
                                gosterilecek_metin = en_iyi_metin
                                gosterilecek_guven = en_iyi_guven
                            else:
                                # Henüz en iyi yoksa kararlı sonucu kullan
                                eslesen_guvenler = [
                                    g for m, g in ocr_gecmisi if m == en_sik_metin
                                ]
                                gosterilecek_metin = en_sik_metin
                                gosterilecek_guven = round(
                                    sum(eslesen_guvenler) / len(eslesen_guvenler), 2
                                )

                            # Ekran durumunu güncelle
                            son_metin = gosterilecek_metin
                            son_guven = gosterilecek_guven

                            # Yeni kararlı plaka oluştuğunda terminale yaz
                            if gosterilecek_metin != son_yazdirilan_metin:
                                print(f"Plaka okundu: {gosterilecek_metin}  (Güven: {gosterilecek_guven})")
                                son_yazdirilan_metin = gosterilecek_metin

                            # Veritabanı işlemi: yalnızca yeni kararlı plaka oluştuğunda
                            # (aynı plaka kamerada durduğu sürece bir kez işlenir)
                            if gosterilecek_metin != son_veritabani_metin:
                                db_sonuc = process_plate_access(
                                    plate_text=gosterilecek_metin,
                                    ocr_confidence=gosterilecek_guven,
                                    source_camera="cam_0",
                                )
                                if db_sonuc is not None:
                                    norm, durum, karar = db_sonuc
                                    son_veritabani_metin = gosterilecek_metin
                                    son_db_durum = durum.upper()
                                    son_db_karar = karar.upper()
                                    son_refresh_zamani = time.monotonic()
                                    # Yapılandırılmış terminal çıktısı
                                    print("-" * 40)
                                    print(f"Plaka       : {norm}")
                                    print(f"Durum       : {durum}")
                                    print(f"Karar       : {karar}")
                                    print(f"OCR Güveni  : {gosterilecek_guven}")
                                    print("-" * 40)

                # 6. Plaka kutusunu çiz
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

                # 7. OCR sonucunu, DB durumunu ve geçiş kararını kutunun üzerine yaz
                # DB işlemi başarılıysa 2 satır olarak gösterilir:
                #   Satır 1: "34FRK052 | APPROVED"
                #   Satır 2: "ALLOW"
                if son_db_durum:
                    karar_etiket = "WAIT" if "WAIT" in son_db_karar else son_db_karar
                    etiket1 = f"{son_metin} | {son_db_durum}"
                    etiket2 = f"{karar_etiket}"

                    # Satır 1: Plaka | DURUM (Yeşil)
                    cv2.putText(
                        frame, etiket1,
                        (x1, max(y1 - 24, 0)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (0, 255, 0), 2
                    )
                    # Satır 2: KARAR (Sarı)
                    cv2.putText(
                        frame, etiket2,
                        (x1, max(y1 - 6, 0)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (0, 255, 255), 2
                    )
                else:
                    etiket = son_metin
                    cv2.putText(
                        frame, etiket,
                        (x1, max(y1 - 8, 0)),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6, (0, 255, 0), 2
                    )

            # 8. Görüntüyü ekranda göster
            cv2.imshow("Plaka Tanima Sistemi - YOLO ve OCR", frame)

            # q tuşuna basıldığında çık
            if cv2.waitKey(1) & 0xFF == ord("q"):
                print("Kullanıcı tarafından çıkış komutu ('q') alındı.")
                break

    finally:
        # 9. Kamera ve pencereleri serbest bırak
        if cap is not None and cap.isOpened():
            cap.release()

        cv2.destroyAllWindows()
        print("Kamera ve pencereler güvenli şekilde kapatıldı.")


def main() -> None:
    """
    Plaka OCR modülünü başlatan ana fonksiyon.
    """
    run_plate_ocr(camera_source=0)


if __name__ == "__main__":
    main()
