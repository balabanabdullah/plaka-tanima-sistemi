"""Windows kamera backend'lerini salt okunur olarak karsilastiran manuel test."""

import math
import cv2


CAMERA_INDEX = 0


def get_backend_name(camera: cv2.VideoCapture) -> str:
    """Aktif VideoCapture backend adini guvenli sekilde dondurur."""
    try:
        return camera.getBackendName()
    except (cv2.error, AttributeError):
        return "Bilinmiyor"


CAMERA_PROPERTIES = [
    ("CAP_PROP_BRIGHTNESS", cv2.CAP_PROP_BRIGHTNESS),
    ("CAP_PROP_CONTRAST", cv2.CAP_PROP_CONTRAST),
    ("CAP_PROP_EXPOSURE", cv2.CAP_PROP_EXPOSURE),
    ("CAP_PROP_AUTO_EXPOSURE", cv2.CAP_PROP_AUTO_EXPOSURE),
    ("CAP_PROP_GAIN", cv2.CAP_PROP_GAIN),
    ("CAP_PROP_AUTOFOCUS", cv2.CAP_PROP_AUTOFOCUS),
    ("CAP_PROP_FOCUS", cv2.CAP_PROP_FOCUS),
]


def is_valid_property_value(value: float) -> bool:
    """OpenCV'nin yaygin desteklenmiyor degeri olan -1'i gecersiz sayar."""
    return math.isfinite(value) and value != -1.0


def print_camera_properties(camera: cv2.VideoCapture) -> int:
    """Ozellikleri get() ile yazdirir ve gecerli deger sayisini dondurur."""
    valid_count = 0

    print(f"VideoCapture backend: {get_backend_name(camera)}")
    print("Mevcut kamera ozellikleri (salt okunur):")

    for property_name, property_id in CAMERA_PROPERTIES:
        value = camera.get(property_id)
        valid = is_valid_property_value(value)
        if valid:
            valid_count += 1
        status = "gecerli" if valid else "desteklenmiyor olabilir"
        print(f"  {property_name}: {value} ({status})")

    return valid_count


def test_backend(backend_label: str, backend_id: int) -> int:
    """Kamera 0'i verilen backend ile acar, raporlar ve sonra release eder."""
    print("\n" + "=" * 60)
    print(f"{backend_label} testi")
    print("=" * 60)

    camera = cv2.VideoCapture(CAMERA_INDEX, backend_id)

    try:
        if not camera.isOpened():
            print(f"Kamera {CAMERA_INDEX}, {backend_label} ile acilamadi.")
            return 0

        valid_count = print_camera_properties(camera)
        print("Canli goruntuyu kapatip sonraki teste gecmek icin q tusuna basin.")

        while True:
            success, frame = camera.read()
            if not success:
                print("Hata: Kameradan goruntu alinamadi.")
                break

            window_name = f"Manual Camera Test - {backend_label}"
            cv2.imshow(window_name, frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

        return valid_count
    finally:
        camera.release()
        cv2.destroyAllWindows()
        print(f"{backend_label} kamera baglantisi release edildi.")


def main() -> None:
    """MSMF ve DSHOW backend'lerini sirayla test eder ve karsilastirir."""
    msmf_count = test_backend("MSMF", cv2.CAP_MSMF)
    dshow_count = test_backend("DSHOW", cv2.CAP_DSHOW)

    print("\n" + "=" * 60)
    print("Kisa sonuc")
    print("=" * 60)
    print(f"MSMF gecerli property sayisi : {msmf_count}/{len(CAMERA_PROPERTIES)}")
    print(f"DSHOW gecerli property sayisi: {dshow_count}/{len(CAMERA_PROPERTIES)}")

    if msmf_count > dshow_count:
        print("Daha fazla gecerli deger veren backend: MSMF")
    elif dshow_count > msmf_count:
        print("Daha fazla gecerli deger veren backend: DSHOW")
    else:
        print("Iki backend ayni sayida gecerli property degeri verdi.")


if __name__ == "__main__":
    main()
