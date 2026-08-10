"""Kamera, YOLO crop ve OCR preprocessing zinciri icin manuel diagnostik arac."""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import cv2
import numpy


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ocr_reader import (  # noqa: E402
    DEFAULT_MODEL_PATH,
    create_ocr_preprocessing_variants,
    load_ocr_reader,
    load_plate_model,
    read_plate_text,
)


CAPTURE_DIR = PROJECT_ROOT / "captures" / "ocr_debug"
YOLO_CONFIDENCE = 0.40


def create_debug_variants(plate_crop: numpy.ndarray) -> list[tuple[str, numpy.ndarray]]:
    """Ana OCR preprocessing yapisini kullanarak acik isimli varyantlar uretir."""
    variants: list[tuple[str, numpy.ndarray]] = [("original", plate_crop.copy())]

    project_variants = create_ocr_preprocessing_variants(plate_crop)
    if not project_variants:
        return variants

    upscaled = project_variants[0]
    variants.append(("upscaled", upscaled))

    if len(project_variants) > 1:
        clahe = project_variants[1]
        variants.append(("grayscale_clahe", clahe))

        _, thresholded = cv2.threshold(
            clahe, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
        )
        variants.append(("otsu_threshold", thresholded))

    return variants


def save_and_read_variants(
    reader,
    plate_crop: numpy.ndarray,
) -> None:
    """Varyantlari kaydeder ve her biri icin EasyOCR sonucunu raporlar."""
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    print("\n" + "=" * 65)
    print(f"OCR debug capture: {timestamp}")
    print("=" * 65)

    for variant_name, image in create_debug_variants(plate_crop):
        height, width = image.shape[:2]
        filename = f"{timestamp}_{variant_name}_{width}x{height}.png"
        output_path = CAPTURE_DIR / filename
        saved = cv2.imwrite(str(output_path), image)

        text, confidence = read_plate_text(reader, image)
        print(f"{variant_name} ({width}x{height}):")
        print(f"  {text}  conf={confidence:.2f}")
        print(f"  kayit: {output_path if saved else 'KAYDEDILEMEDI'}")


def select_best_plate_crop(frame: numpy.ndarray, boxes) -> tuple[numpy.ndarray | None, tuple | None]:
    """Tespitler arasindan YOLO confidence degeri en yuksek crop'u secer."""
    frame_height, frame_width = frame.shape[:2]
    best_crop = None
    best_info = None
    best_confidence = -1.0

    for box in boxes:
        x1, y1, x2, y2 = map(int, box.xyxy[0])
        x1 = max(0, min(x1, frame_width - 1))
        y1 = max(0, min(y1, frame_height - 1))
        x2 = max(0, min(x2, frame_width))
        y2 = max(0, min(y2, frame_height))

        if x2 <= x1 or y2 <= y1:
            continue

        confidence = float(box.conf[0]) if box.conf is not None else 0.0
        if confidence > best_confidence:
            best_confidence = confidence
            best_crop = frame[y1:y2, x1:x2].copy()
            best_info = (x1, y1, x2, y2, confidence)

    return best_crop, best_info


def run_debug(camera_index: int) -> None:
    """Canli kamera uzerinde YOLO crop diagnostigini calistirir."""
    model = load_plate_model(DEFAULT_MODEL_PATH)
    if model is None:
        return

    reader = load_ocr_reader()
    if reader is None:
        return

    camera = cv2.VideoCapture(camera_index)
    latest_crop = None

    try:
        if not camera.isOpened():
            print(f"Hata: Kamera {camera_index} acilamadi.")
            return

        try:
            backend_name = camera.getBackendName()
        except (cv2.error, AttributeError):
            backend_name = "Bilinmiyor"

        print(f"Kamera: {camera_index} | Backend: {backend_name}")
        print("s: crop ve OCR varyantlarini kaydet/raporla")
        print("q: cikis")

        while True:
            success, frame = camera.read()
            if not success:
                print("Hata: Kameradan goruntu alinamadi.")
                break

            results = model(frame, conf=YOLO_CONFIDENCE, verbose=False, device="cpu")
            crop, detection = select_best_plate_crop(frame, results[0].boxes)

            if crop is not None and detection is not None:
                latest_crop = crop
                x1, y1, x2, y2, confidence = detection
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(
                    frame,
                    f"plate conf={confidence:.2f}",
                    (x1, max(y1 - 8, 20)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (0, 255, 0),
                    2,
                )
                cv2.imshow("Detected Plate Crop", latest_crop)

            cv2.imshow("Manual Plate Crop Debug", frame)
            key = cv2.waitKey(1) & 0xFF

            if key == ord("q"):
                break
            if key == ord("s"):
                if latest_crop is None:
                    print("Kaydedilecek plaka crop'u henuz tespit edilmedi.")
                else:
                    save_and_read_variants(reader, latest_crop)
    finally:
        camera.release()
        cv2.destroyAllWindows()
        print("Kamera ve debug pencereleri kapatildi.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Kamera -> YOLO crop -> preprocessing -> EasyOCR diagnostik araci"
    )
    parser.add_argument(
        "--camera",
        type=int,
        default=0,
        help="Kamera cihaz indeksi (varsayilan: 0)",
    )
    args = parser.parse_args()
    run_debug(args.camera)


if __name__ == "__main__":
    main()
