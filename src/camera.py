import cv2


def open_camera(camera_source: int = 0) -> None:
    """
    Belirtilen kamera kaynağından canlı görüntü akışını açar ve ekranda gösterir.

    Parametreler:
        camera_source (int): Açılacak kameranın indeksi (Varsayılan: 0)
    """
    # OpenCV VideoCapture nesnesi ile kamerayı başlatıyoruz
    cap = cv2.VideoCapture(camera_source)

    try:
        # Kameranın başarıyla açılıp açılmadığını kontrol ediyoruz
        if not cap.isOpened():
            print(f"Hata: {camera_source} indeksli kamera kaynağı açılamadı! Lütfen kamera bağlantısını kontrol edin.")
            return

        print("Kamera akışı başlatıldı. Çıkmak için pencere üzerindeyken 'q' tuşuna basın.")

        # Kamera açık olduğu sürece görüntü karelerini (frame) okumaya devam ediyoruz
        while True:
            # Kameradan bir kare oku
            # ret: Okuma başarılı mı (True/False)
            # frame: Okunan görüntü karesi
            ret, frame = cap.read()

            # Görüntü okunamadıysa döngüyü sonlandır
            if not ret:
                print("Hata: Kameradan görüntü karesi alınamadı!")
                break

            # Canlı görüntüyü ekranda göster (Yatay çevirme yapılmıyor)
            cv2.imshow("Plaka Tanima Sistemi - Kamera", frame)

            # Kullanıcı 'q' tuşuna basarsa döngüden çık
            if cv2.waitKey(1) & 0xFF == ord('q'):
                print("Kullanıcı tarafından çıkış komutu ('q') alındı.")
                break

    finally:
        # Program kapandığında veya bir hata oluştuğunda kamera kaynağını serbest bırak
        if cap is not None and cap.isOpened():
            cap.release()

        # Tüm OpenCV pencerelerini kapat
        cv2.destroyAllWindows()
        print("Kamera kaynağı ve pencere alanı güvenli bir şekilde kapatıldı.")


def main() -> None:
    """
    Kamera modülünü başlatan ana çalıştırma fonksiyonu.
    """
    open_camera(camera_source=0)


if __name__ == "__main__":
    main()
