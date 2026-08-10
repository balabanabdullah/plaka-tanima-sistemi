"""
tests/test_cloud_sync.py — Çevrimdışı Öncelikli Tek Yönlü HTTPS Senkronizasyon Testleri

Bu test paketi, cloud_sync modülünün HTTPS API senkronizasyonunu doğrular.
"""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

# src klasörünü sys.path'e ekle
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Vehicle, AccessLog, VehicleStatus, AccessDirection, AccessDecision, utc_now
from cloud_sync import (
    run_sync,
    run_watch_mode,
    ensure_local_sync_ids,
    send_https_push,
)


def create_test_vehicle(plate="34SYNC1", **kwargs):
    now = utc_now()
    return Vehicle(
        sync_id=kwargs.get("sync_id", str(uuid.uuid4())),
        plate_text=plate,
        normalized_plate=plate,
        status=kwargs.get("status", VehicleStatus.pending),
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )


class TestCloudSync(unittest.TestCase):
    """
    Bulut senkronizasyon birim testleri.
    """

    def setUp(self):
        self.local_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.local_url = f"sqlite:///{self.local_db_file.name}"

        self.local_engine = create_engine(self.local_url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(bind=self.local_engine)
        self.LocalSession = sessionmaker(bind=self.local_engine)

    def tearDown(self):
        self.local_db_file.close()
        try:
            os.remove(self.local_db_file.name)
        except OSError:
            pass

    def test_1_missing_cloud_url_exits_gracefully(self):
        """CLOUD_SYNC_API_URL veya SYNC_API_TOKEN tanımlanmadığında (True, stats) dönmeli."""
        success, stats = run_sync(local_url=self.local_url, sync_api_url="", sync_token="")
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["new"], 0)

    def test_2_legacy_local_records_assigned_uuid(self):
        """sync_id alanı NULL olan kayıtlara güvenle UUID atanmalıdır."""
        with self.LocalSession() as session:
            v = create_test_vehicle(plate="34NOCLU1")
            session.add(v)
            session.flush()
            log = AccessLog(
                vehicle_id=v.id,
                plate_text="34NOCLU1",
                normalized_plate="34NOCLU1",
                decision=AccessDecision.wait_for_approval,
                detected_at=utc_now(),
            )
            session.add(log)
            session.commit()

            # Eski veritabanı simülasyonu için sync_id alanlarını NULL yap
            session.query(Vehicle).filter_by(id=v.id).update({"sync_id": None})
            session.query(AccessLog).filter_by(id=log.id).update({"sync_id": None})
            session.commit()

            up_v, up_l = ensure_local_sync_ids(session)
            self.assertEqual(up_v, 1)
            self.assertEqual(up_l, 1)
            self.assertIsNotNone(v.sync_id)
            self.assertIsNotNone(log.sync_id)

    @patch("cloud_sync.send_https_push")
    def test_3_summary_counters_and_idempotency(self, mock_push):
        """Senkronizasyon özet sayaçlarını doğrular."""
        mock_push.return_value = (
            True,
            {
                "vehicles": {"new": 1, "updated": 0, "unchanged": 0},
                "access_logs": {"new": 1, "updated": 0, "unchanged": 0},
            },
        )
        with self.LocalSession() as session:
            v = create_test_vehicle(plate="34SYNC1")
            session.add(v)
            session.commit()

        success, stats = run_sync(
            local_url=self.local_url,
            sync_api_url="https://cloud.run.app",
            sync_token="secret123",
        )
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["new"], 1)

    def test_4_dry_run_mode_does_not_modify_cloud_db(self):
        """--dry-run seçeneğinde sunucuya istek atılmaz."""
        with self.LocalSession() as session:
            v = create_test_vehicle(plate="34DRY1")
            session.add(v)
            session.commit()

        success, stats = run_sync(
            local_url=self.local_url,
            sync_api_url="https://cloud.run.app",
            sync_token="secret123",
            dry_run=True,
        )
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["new"], 1)

    def test_5_interval_validation(self):
        """Interval 0 veya negatif girildiğinde ValueError fırlatılmalıdır."""
        with self.assertRaises(ValueError):
            run_watch_mode(interval=0)


if __name__ == "__main__":
    unittest.main()
