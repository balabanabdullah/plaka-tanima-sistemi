import cv2
from ultralytics import YOLO


def load_model(model_name: str = "yolov8n.pt") -> YOLO:
    """
    YOLO modelini yükler. Model yerelde yoksa otomatik olarak indirilir.

    Parametreler:
        model_name (str): Yüklenecek YOLO model adı (Varsayılan: "yolov8n.pt")

    Döndürür:
        YOLO: Yüklenmiş Ultralytics YOLO model nesnesi
    """
    print(f"YOLO modeli yükleniyor: {model_name} ...")
    model = YOLO(model_name)
    print("Model başarıyla yüklendi.")
    return model


def detect_vehicles(camera_source: int = 0) -> None:
    """
    Kameradan canlı görüntü alarak yalnızca araç sınıflarını (otomobil, motosiklet, otobüs, kamyon)
    CPU üzerinde tespit eder ve ekranda gösterir.

    Parametreler:
        camera_source (int): Açılacak kamera indeksi (Varsayılan: 0)
    """
    # 1. Modeli döngüden ÖNCE bir kez yüklüyoruz (her karede tekrar yüklenmez)
    model = load_model("yolov8n.pt")

    # 2. Sadece araç sınıflarını filtrelemek için COCO veri seti ID'leri
    # 2: car (otomobil), 3: motorcycle (motosiklet), 5: bus (otobüs), 7: truck (kamyon)
    vehicle_classes = [2, 3, 5, 7]

    # 3. Kamera kaynağını başlatıyoruz
    cap = cv2.VideoCapture(camera_source)

    try:
        # Kamera açılabildi mi kontrol ediyoruz
        if not cap.isOpened():
            print(f"Hata: {camera_source} indeksli kamera kaynağı açılamadı! Lütfen kamera bağlantısını kontrol edin.")
            return

        print("Araç tespiti başlatıldı. Çıkmak için pencere üzerindeyken 'q' tuşuna basın.")

        # Canlı görüntü işleme döngüsü
        while True:
            # Kameradan bir kare oku
            ret, frame = cap.read()

            # Görüntü okunamadıysa döngüden çık
            if not ret:
                print("Hata: Kameradan görüntü karesi alınamadı!")
                break

            # 4. YOLO Modeli ile araç tespiti yap (CPU üzerinde)
            # classes=[2, 3, 5, 7]: Yalnızca araç sınıflarını tespit et
            # conf=0.50: Güven eşiği %50 ve üzeri olanları al
            # verbose=False: Konsola her kare için log yazdırarak CPU'yu yorma
            results = model(frame, classes=vehicle_classes, conf=0.50, verbose=False)

            # 5. Tespit edilen araç kutularını ve etiketlerini görüntü üzerine çiz
            annotated_frame = results[0].plot()

            # 6. İşlenmiş görüntüyü ekranda göster
            cv2.imshow("Plaka Tanima Sistemi - Arac Tespiti", annotated_frame)

            # Kullanıcı 'q' tuşuna basarsa döngüden çık
            if cv2.waitKey(1) & 0xFF == ord('q'):
                print("Kullanıcı tarafından çıkış komutu ('q') alındı.")
                break

    finally:
        # 7. Kamera kaynağını ve pencereleri her durumda temizle
        if cap is not None and cap.isOpened():
            cap.release()

        cv2.destroyAllWindows()
        print("Kamera kaynağı ve pencereler güvenli bir şekilde kapatıldı.")


def main() -> None:
    """
    Araç tespiti modülünü başlatan ana fonksiyon.
    """
    detect_vehicles(camera_source=0)


if __name__ == "__main__":
    main()
