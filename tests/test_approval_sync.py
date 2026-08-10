"""
tests/test_approval_sync.py — Buluttan Yerele HTTPS Yetki Senkronizasyonu Birim Testleri

Bu test paketi, approval_sync modülünün HTTPS API yetki aktarımlarını doğrular.
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

from models import Base, Vehicle, VehicleStatus, utc_now
from approval_sync import (
    run_approval_sync,
    run_watch_mode,
    fetch_approvals_from_cloud_api,
)


def create_test_vehicle(sync_id=None, plate="34APP01", status=VehicleStatus.pending, **kwargs):
    now = utc_now()
    return Vehicle(
        sync_id=sync_id or str(uuid.uuid4()),
        plate_text=plate,
        normalized_plate=plate,
        status=status,
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
        **kwargs,
    )


class TestApprovalSync(unittest.TestCase):
    """
    Buluttan yerele yetki senkronizasyonu birim testleri.
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

    @patch("approval_sync.fetch_approvals_from_cloud_api")
    def test_1_approved_cloud_status_updates_local_pending_vehicle(self, mock_fetch):
        """Bulutta onaylanan (approved) araç yereldeki pending aracı güncellemeli."""
        v_sync_id = str(uuid.uuid4())
        with self.LocalSession() as local_session:
            v_local = create_test_vehicle(
                sync_id=v_sync_id,
                plate="34APP01",
                status=VehicleStatus.pending,
            )
            local_session.add(v_local)
            local_session.commit()

        mock_fetch.return_value = (
            True,
            [
                {
                    "sync_id": v_sync_id,
                    "status": "approved",
                    "approved_at": "2026-08-10T09:00:00",
                    "approved_by": "admin",
                    "notes": "Approved",
                }
            ],
        )

        success, stats = run_approval_sync(
            local_url=self.local_url,
            sync_api_url="https://cloud.run.app",
            sync_token="token123",
        )
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["updated"], 1)

        with self.LocalSession() as local_session:
            v_after = local_session.query(Vehicle).filter_by(sync_id=v_sync_id).first()
            self.assertEqual(v_after.status, VehicleStatus.approved)

    @patch("approval_sync.fetch_approvals_from_cloud_api")
    def test_2_rejected_cloud_status_updates_local_pending_vehicle(self, mock_fetch):
        """Bulutta reddedilen (rejected) araç yereldeki pending aracı güncellemeli."""
        v_sync_id = str(uuid.uuid4())
        with self.LocalSession() as local_session:
            v_local = create_test_vehicle(
                sync_id=v_sync_id,
                plate="34REJ01",
                status=VehicleStatus.pending,
            )
            local_session.add(v_local)
            local_session.commit()

        mock_fetch.return_value = (
            True,
            [
                {
                    "sync_id": v_sync_id,
                    "status": "rejected",
                    "approved_at": None,
                    "approved_by": "admin",
                    "notes": "Rejected",
                }
            ],
        )

        success, stats = run_approval_sync(
            local_url=self.local_url,
            sync_api_url="https://cloud.run.app",
            sync_token="token123",
        )
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["updated"], 1)

        with self.LocalSession() as local_session:
            v_after = local_session.query(Vehicle).filter_by(sync_id=v_sync_id).first()
            self.assertEqual(v_after.status, VehicleStatus.rejected)

    @patch("approval_sync.fetch_approvals_from_cloud_api")
    def test_3_local_operational_fields_preserved(self, mock_fetch):
        """Yerel ilk görülme ve plaka metinleri yetki senkronizasyonunda KESİNLİKLE KORUNMALIDIR."""
        v_sync_id = str(uuid.uuid4())
        with self.LocalSession() as local_session:
            v_local = create_test_vehicle(
                sync_id=v_sync_id,
                plate="34PRESERVE",
                status=VehicleStatus.pending,
            )
            local_session.add(v_local)
            local_session.commit()

        mock_fetch.return_value = (
            True,
            [
                {
                    "sync_id": v_sync_id,
                    "status": "approved",
                    "approved_at": "2026-08-10T09:00:00",
                    "approved_by": "admin",
                    "notes": "Ok",
                }
            ],
        )

        run_approval_sync(
            local_url=self.local_url,
            sync_api_url="https://cloud.run.app",
            sync_token="token123",
        )

        with self.LocalSession() as local_session:
            v_after = local_session.query(Vehicle).filter_by(sync_id=v_sync_id).first()
            self.assertEqual(v_after.plate_text, "34PRESERVE")
            self.assertEqual(v_after.normalized_plate, "34PRESERVE")

    def test_4_interval_validation(self):
        """Interval 0 veya negatif girildiğinde ValueError fırlatılmalıdır."""
        with self.assertRaises(ValueError):
            run_watch_mode(interval=0)


if __name__ == "__main__":
    unittest.main()
