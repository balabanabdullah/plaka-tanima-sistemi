"""OCR preprocessing, consensus ve fiziksel olay kararlılığı regresyonları."""

import sys
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

import numpy

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from ocr_reader import (
    create_ocr_preprocessing_variants,
    get_final_plate_candidate,
    read_plate_with_variants,
)
from plate_service import is_probable_same_plate, normalize_plate


class TestOCRStability(unittest.TestCase):
    def test_consensus_prefers_plate_over_country_band_noise(self):
        history = deque([
            ("34AVEC01", 0.91),
            ("34AVEC01", 0.88),
            ("TR34AVEC01", 0.93),
            ("34AVEC01", 0.90),
        ])
        self.assertEqual(get_final_plate_candidate(history)[0], "34AVEC01")

    def test_consensus_ignores_single_unrelated_outlier(self):
        history = deque([
            ("34AVEC01", 0.91),
            ("34AVEC01", 0.89),
            ("18LD410", 0.97),
            ("34AVEC01", 0.90),
        ])
        self.assertEqual(get_final_plate_candidate(history)[0], "34AVEC01")

    def test_similar_real_plates_are_not_fuzzy_merged(self):
        self.assertFalse(is_probable_same_plate("34ABC123", "34ABC128"))

    def test_foreign_latin_alphanumeric_plates_are_preserved(self):
        for plate in ("AB12XYZ", "7ABC1234", "XYZ987"):
            self.assertEqual(normalize_plate(plate), plate)
            history = [(plate, 0.90)] * 4
            self.assertEqual(get_final_plate_candidate(history)[0], plate)

    def test_preprocessing_keeps_aspect_ratio_and_limits_variants(self):
        crop = numpy.zeros((40, 120, 3), dtype=numpy.uint8)
        variants = create_ocr_preprocessing_variants(crop)
        self.assertEqual(len(variants), 2)
        for variant in variants:
            height, width = variant.shape[:2]
            self.assertAlmostEqual(width / height, 3.0, places=2)

    @patch("ocr_reader.read_plate_text")
    def test_variant_reading_returns_one_frame_candidate(self, mock_read):
        mock_read.side_effect = [
            ("34AVEC01", 0.84),
            ("34AVEC01", 0.92),
        ]
        crop = numpy.zeros((50, 150, 3), dtype=numpy.uint8)
        self.assertEqual(read_plate_with_variants(object(), crop), ("34AVEC01", 0.92))
        self.assertEqual(mock_read.call_count, 2)

    @patch("ocr_reader.read_plate_text")
    def test_real_crop_high_confidence_upscale_skips_extra_ocr(self, mock_read):
        """174x44 debug orneginde guclu upscale sonucu tek cagriyla secilmelidir."""
        mock_read.return_value = ("52BE868", 0.93)
        crop = numpy.zeros((44, 174, 3), dtype=numpy.uint8)

        result = read_plate_with_variants(object(), crop)

        self.assertEqual(result, ("52BE868", 0.93))
        self.assertEqual(mock_read.call_count, 1)

    @patch("ocr_reader.read_plate_text")
    def test_real_small_crop_keeps_valid_upscale_when_clahe_fails(self, mock_read):
        """111x26 crop'ta CLAHE basarisiz olsa da upscale sonucu korunmalidir."""
        mock_read.side_effect = [
            ("34HKT024", 0.70),
            ("OKUNAMADI", 0.0),
        ]
        crop = numpy.zeros((26, 111, 3), dtype=numpy.uint8)

        result = read_plate_with_variants(object(), crop)

        self.assertEqual(result, ("34HKT024", 0.70))
        self.assertEqual(mock_read.call_count, 2)

        variants = create_ocr_preprocessing_variants(crop)
        self.assertEqual(variants[0].shape[:2], (78, 333))


if __name__ == "__main__":
    unittest.main()
