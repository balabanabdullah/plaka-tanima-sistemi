from pathlib import Path
import cv2
from ultralytics import YOLO

# Proje kök dizinini ve varsayılan model yolunu belirliyoruz
# Path(__file__).resolve().parent.parent -> src klasörünün bir üstü (proje kökü)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODEL_PATH = PROJECT_ROOT / "models" / "license_plate_detector.pt"


def load_plate_model(model_path: Path | str = DEFAULT_MODEL_PATH) -> YOLO | None:
    """
    Belirtilen yoldan plaka tespit YOLO modelini yükler.
    Model dosyası bulunamazsa otomatik indirme yapmaz, Türkçe uyarı verip None döner.

    Parametreler:
        model_path (Path | str): Model dosyasının yolu

    Döndürür:
        YOLO | None: Yüklenmiş YOLO model nesnesi veya dosya yoksa None
    """
    path = Path(model_path).resolve()

    # Model dosyasının var olup olmadığını kontrol et
    if not path.exists():
        print("Hata: Plaka tespit model dosyası bulunamadı!")
        print(f"Beklenen dosya yolu: {path}")
        print("Lütfen 'license_plate_detector.pt' dosyasını 'models/' klasörü altına ekleyin.")
        return None

    try:
        print(f"Plaka tespit modeli yükleniyor: {path.name} ...")
        model = YOLO(str(path))
        print("Plaka tespit modeli başarıyla yüklendi.")
        return model
    except Exception as e:
        print(f"Hata: Model yüklenirken bir sorun oluştu: {e}")
        return None


def detect_plates(camera_source: int = 0) -> None:
    """
    Kameradan canlı görüntü alarak araç plakalarını tespit eder ve ekranda gösterir.

    Parametreler:
        camera_source (int): Açılacak kamera kaynağı (Varsayılan: 0)
    """
    # 1. Modeli kamera açılmadan ÖNCE bir kez yüklüyoruz
    model = load_plate_model(DEFAULT_MODEL_PATH)

    # Model dosyası yoksa veya yüklenemediyse kamera açılmadan temiz şekilde sonlandır
    if model is None:
        print("Model yüklenemediği için kamera başlatılmadı. Program sonlandırılıyor.")
        return

    # 2. Kamera kaynağını başlatıyoruz
    cap = cv2.VideoCapture(camera_source)

    try:
        # Kamera açılabildi mi kontrol ediyoruz
        if not cap.isOpened():
            print(f"Hata: {camera_source} indeksli kamera kaynağı açılamadı! Lütfen kamera bağlantısını kontrol edin.")
            return

        print("Plaka tespiti başlatıldı. Çıkmak için pencere üzerindeyken 'q' tuşuna basın.")

        # Canlı görüntü işleme döngüsü
        while True:
            # Kameradan bir kare oku
            ret, frame = cap.read()

            # Görüntü okunamadıysa döngüden çık
            if not ret:
                print("Hata: Kameradan görüntü karesi alınamadı!")
                break

            # 3. YOLO modeli ile plaka tespiti yap (conf=0.40, verbose=False, device="cpu")
            results = model(frame, conf=0.40, verbose=False, device="cpu")

            # 4. Tespit edilen plaka kutularını görüntü üzerine çiz
            annotated_frame = results[0].plot()

            # 5. Görüntüyü ekranda göster
            cv2.imshow("Plaka Tanima Sistemi - Plaka Tespiti", annotated_frame)

            # Kullanıcı 'q' tuşuna basarsa döngüden çık
            if cv2.waitKey(1) & 0xFF == ord('q'):
                print("Kullanıcı tarafından çıkış komutu ('q') alındı.")
                break

    finally:
        # 6. Kamera kaynağını ve OpenCV pencerelerini serbest bırak
        if cap is not None and cap.isOpened():
            cap.release()

        cv2.destroyAllWindows()
        print("Kamera kaynağı ve pencereler güvenli bir şekilde kapatıldı.")


def main() -> None:
    """
    Plaka tespiti modülünü başlatan ana fonksiyon.
    """
    detect_plates(camera_source=0)


if __name__ == "__main__":
    main()
