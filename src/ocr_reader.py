import re
import cv2
import time
import argparse
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
from barrier_controller import BarrierController

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

# Veritabanı öncesi final doğrulama sabitleri
FINAL_HISTORY_SIZE = 8
FINAL_MIN_MATCHES = 4
FINAL_MIN_AVG_CONFIDENCE = 0.70

# OCR için izin verilen karakter listesi (yalnızca Latin harf ve rakam)
OCR_ALLOWLIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

# Minimum plaka kutusu boyutları (piksel)
MIN_PLATE_WIDTH = 120
MIN_PLATE_HEIGHT = 35

# Minimum keskinlik eşiği (Laplacian varyansı)
MIN_SHARPNESS = 60.0

# Plaka gerçek kaybolma zaman aşımı (saniye)
PLATE_ABSENCE_RESET_SECONDS = 3.0

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
    direction: AccessDirection,
    source_camera: str,
) -> "tuple[str, str, str, bool] | None":
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
        tuple[str, str, str, bool]: (normalized_plate, status, decision, log_created)
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

            log_created = False
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
                log_created = True
                if DEBUG_OCR:
                    print(
                        f"[DEBUG LOG] AccessLog oluşturuldu:\n"
                        f"  Plaka: {normalized}\n"
                        f"  Karar: {decision.value}\n"
                        f"  Yön: {direction.value}\n"
                        f"  Kamera: {source_camera}"
                    )

            return normalized, vehicle.status.value, decision.value, log_created

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


def get_final_plate_candidate(
    history: deque[tuple[str, float]] | list[tuple[str, float]],
) -> "tuple[str, float, int] | None":
    """
    Veritabanına gönderilecek nihai plaka adayını doğrular.

    Adımlar:
    1. Geçmişteki exact metin tekrar sayıları hesaplanır (Counter).
    2. En sık geçen 1. ve 2. adaylar karşılaştırılır; eşitlik varsa None döner.
    3. En sık adayın tekrar sayısı >= FINAL_MIN_MATCHES olmalı.
    4. Adayın ortalama güven değeri >= FINAL_MIN_AVG_CONFIDENCE (0.70) olmalı.

    Parametreler:
        history: (metin, guven) çiftlerini içeren liste veya deque.

    Döndürür:
        tuple[str, float, int] | None: (final_metin, ortalama_guven, tekrar_sayisi) veya None.
    """
    if not history or len(history) < FINAL_MIN_MATCHES:
        return None

    # Exact metin tekrar sayılarını hesapla
    metin_sayilari = Counter(metin for metin, _ in history)
    most_common = metin_sayilari.most_common(2)

    if not most_common:
        return None

    en_sik_metin, tekrar_sayisi = most_common[0]

    # 1. Eşitlik kontrolü: En sık geçen ilk 2 adayın tekrar sayıları eşitse belirsizlik var
    if len(most_common) > 1 and most_common[0][1] == most_common[1][1]:
        return None

    # 2. Minimum tekrar kontrolü
    if tekrar_sayisi < FINAL_MIN_MATCHES:
        return None

    # 3. Ortalama güven hesaplama ve kontrolü
    eslesen_guvenler = [guven for metin, guven in history if metin == en_sik_metin]
    if not eslesen_guvenler:
        return None

    avg_confidence = sum(eslesen_guvenler) / len(eslesen_guvenler)
    if avg_confidence < FINAL_MIN_AVG_CONFIDENCE:
        return None

    return en_sik_metin, round(avg_confidence, 2), tekrar_sayisi


def run_plate_ocr(
    camera_source: int = 0,
    direction: AccessDirection = AccessDirection.unknown,
    source_camera: str = "cam_0",
    esp32_port: str | None = None,
    esp32_baud: int = 115200,
    barrier_dry_run: bool = False,
) -> None:
    """
    Kameradan canlı görüntü alır, YOLO ile plaka tespiti yapar,
    EasyOCR ile plaka metnini okur ve ekranda gösterir.
    Kararlı plakalar veritabanına kaydedilir ve erişim kararı üretilir.

    Parametreler:
        camera_source (int): Kamera kaynağı indeksi (Varsayılan: 0)
        direction (AccessDirection): Kameranın hareket yönü (entry / exit / unknown)
        source_camera (str): Kameranın adı (Varsayılan: "cam_0")
        esp32_port (str | None): ESP32 seri port adresi
        esp32_baud (int): ESP32 seri port hızı (Varsayılan: 115200)
        barrier_dry_run (bool): True ise seri porta bağlanmadan dry-run bariyer testi yapılır.
    """
    # 1. Veritabanını başlat (kamera açılmadan önce zorunlu)
    try:
        init_db()
    except Exception as e:
        print(f"Hata: Veritabanı başlatılamadı: {e}")
        print("Uygulama sonlandırılıyor.")
        return

    # 2. ESP32 Bariyer Kontrolcüsünü oluştur (opsiyonel)
    barrier: BarrierController | None = None
    if barrier_dry_run or esp32_port:
        barrier = BarrierController(
            port=esp32_port,
            baudrate=esp32_baud,
            dry_run=barrier_dry_run,
        )
        barrier.connect()

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

    # DB öncesi nihai doğrulama geçmişi (yalnızca filtrelenmiş geçerli okumalar)
    final_ocr_history: deque[tuple[str, float]] = deque(maxlen=FINAL_HISTORY_SIZE)

    # YOLO plaka tespiti zaman takibi (gerçek görünmeme zaman aşımı için)
    last_plate_detection_time = time.monotonic()

    try:
        if not cap.isOpened():
            print(f"Hata: {camera_source} indeksli kamera açılamadı! Bağlantıyı kontrol edin.")
            return

        print("-" * 40)
        print(f"Kamera       : {camera_source}")
        print(f"Kamera Adı   : {source_camera}")
        print(f"Yön          : {direction.value}")
        print("-" * 40)
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

                # Geçerli bir plaka tespiti yapıldı: son görülme zamanını güncelle
                last_plate_detection_time = time.monotonic()

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
                                final_ocr_history.clear()
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

                        # Başarılı okuma: geçmişlere ekle, başarısız sayacını sıfırla
                        ocr_gecmisi.append((okunan, guven))
                        final_ocr_history.append((okunan, guven))
                        basarisiz_sayisi = 0

                        # En iyi sonucu güncelle:
                        # Yalnızca yeni güven mevcut en iyi güvenden 0.02 daha yüksekse kabul et
                        if guven >= en_iyi_guven + 0.02:
                            en_iyi_metin = okunan
                            en_iyi_guven = guven

                    else:
                        # Başarısız okuma: sayacı artır
                        basarisiz_sayisi += 1

                    # OCR_STALE_LIMIT dolduğunda YALNIZCA OCR state'i temizle
                    # son_veritabani_metin / aktif geçiş olayı SİLİNMEZ!
                    if basarisiz_sayisi >= OCR_STALE_LIMIT:
                        if DEBUG_OCR:
                            print("[OCR RESET] OCR geçmişi temizlendi ancak aktif geçiş korunuyor.")

                        ocr_gecmisi.clear()
                        final_ocr_history.clear()
                        basarisiz_sayisi = 0
                        en_iyi_metin = "OKUNAMADI"
                        en_iyi_guven = 0.0

                    # 6. DB öncesi nihai doğrulama katmanı (Final Plate Validation)
                    if len(final_ocr_history) > 0:
                        final_aday = get_final_plate_candidate(final_ocr_history)

                        if DEBUG_OCR and final_aday:
                            print(f"[DEBUG FINAL HISTORY] Aday: {final_aday[0]} | Ort. Güven: {final_aday[1]} | Tekrar: {final_aday[2]}/{len(final_ocr_history)}")

                        if final_aday is not None:
                            final_metin, final_guven, tekrar_sayisi = final_aday
                            gosterilecek_metin = final_metin
                            gosterilecek_guven = final_guven

                            # Ekran durumunu güncelle
                            son_metin = gosterilecek_metin
                            son_guven = gosterilecek_guven

                            # Veritabanı işlemi: yalnızca nihai olarak doğrulanmış YENİ plaka oluştuğunda
                            if final_metin != son_veritabani_metin:
                                if DEBUG_OCR:
                                    print(f"[DB EVENT]\nPlaka: {final_metin}\nDirection: {direction.value}\nSource Camera: {source_camera}")
                                db_sonuc = process_plate_access(
                                    plate_text=final_metin,
                                    ocr_confidence=final_guven,
                                    direction=direction,
                                    source_camera=source_camera,
                                )
                                if db_sonuc is not None:
                                    norm, durum, karar, log_created = db_sonuc
                                    son_veritabani_metin = final_metin
                                    son_db_durum = durum.upper()
                                    son_db_karar = karar.upper()
                                    son_refresh_zamani = time.monotonic()
                                    # Yapılandırılmış terminal çıktısı
                                    print("-" * 40)
                                    print(f"Final Plaka  : {norm}")
                                    print(f"Tekrar       : {tekrar_sayisi}/{len(final_ocr_history)}")
                                    print(f"Ort. Güven   : {final_guven}")
                                    print(f"Durum        : {durum}")
                                    print(f"Karar        : {karar}")
                                    print(f"Yön          : {direction.value}")
                                    print(f"Kamera       : {source_camera}")
                                    print("-" * 40)

                                    # Yalnızca bariyer aktifse, YENİ bir erişim kaydı yazıldığında (log_created == True)
                                    # VE karar "allow" ise ESP32 bariyer komutunu tetikle
                                    if barrier is not None and karar == "allow" and log_created:
                                        barrier.send_open()

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

            # 8. Plaka Gerçek Kaybolma Kontrolü (Event Reset)
            # YOLO plaka tespiti 3.0 saniye boyunca gerçekleşmediyse araç sahneden çıkmıştır
            if (time.monotonic() - last_plate_detection_time >= PLATE_ABSENCE_RESET_SECONDS) and son_veritabani_metin:
                if DEBUG_OCR:
                    print("[EVENT RESET] Plaka 3.0 saniyedir görünmüyor. Aktif geçiş kapatıldı.")

                son_veritabani_metin = ""
                son_db_durum = ""
                son_db_karar = ""
                son_refresh_zamani = 0.0
                aday_metin = ""
                aday_tekrar = 0

                ocr_gecmisi.clear()
                final_ocr_history.clear()
                en_iyi_metin = "OKUNAMADI"
                en_iyi_guven = 0.0
                son_metin = "OKUNUYOR..."
                son_guven = 0.0
                basarisiz_sayisi = 0

            # 9. Görüntüyü ekranda göster
            cv2.imshow("Plaka Tanima Sistemi - YOLO ve OCR", frame)

            # q tuşuna basıldığında çık
            if cv2.waitKey(1) & 0xFF == ord("q"):
                print("Kullanıcı tarafından çıkış komutu ('q') alındı.")
                break

    finally:
        # 10. ESP32 bariyer bağlantısı, kamera ve pencereleri serbest bırak
        if barrier is not None:
            barrier.disconnect()

        if cap is not None and cap.isOpened():
            cap.release()

        cv2.destroyAllWindows()
        print("Kamera ve pencereler güvenli şekilde kapatıldı.")


def main() -> None:
    """
    Plaka OCR modülünü CLI argümanlarıyla başlatan ana fonksiyon.
    """
    parser = argparse.ArgumentParser(
        description="Plaka Tanıma Sistemi - Canlı Kamera ve OCR Modülü"
    )
    parser.add_argument(
        "--camera",
        type=int,
        default=0,
        help="Kamera cihaz indeksi (Varsayılan: 0)",
    )
    parser.add_argument(
        "--camera-name",
        type=str,
        default=None,
        help="İnsan tarafından okunabilir kamera adı (Varsayılan: cam_<camera>)",
    )
    parser.add_argument(
        "--direction",
        type=str,
        choices=["entry", "exit", "unknown"],
        default="unknown",
        help="Kamera yönü: entry (giriş), exit (çıkış), unknown (bilinmiyor) (Varsayılan: unknown)",
    )
    parser.add_argument(
        "--esp32-port",
        type=str,
        default=None,
        help="ESP32 USB Seri Port adresi (örn: COM5, /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--esp32-baud",
        type=int,
        default=115200,
        help="ESP32 Seri haberleşme hızı (Varsayılan: 115200)",
    )
    parser.add_argument(
        "--barrier-dry-run",
        action="store_true",
        help="ESP32 seri portuna bağlanmadan dry-run bariyer testi yap",
    )

    args = parser.parse_args()

    # Kamera adı verilmediyse cam_<camera_index> üret
    cam_name = args.camera_name if args.camera_name else f"cam_{args.camera}"

    # Yön stringini AccessDirection enum'a dönüştür
    try:
        dir_enum = AccessDirection(args.direction)
    except ValueError:
        dir_enum = AccessDirection.unknown

    run_plate_ocr(
        camera_source=args.camera,
        direction=dir_enum,
        source_camera=cam_name,
        esp32_port=args.esp32_port,
        esp32_baud=args.esp32_baud,
        barrier_dry_run=args.barrier_dry_run,
    )


if __name__ == "__main__":
    main()
