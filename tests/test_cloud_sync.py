"""
tests/test_cloud_sync.py — Çevrimdışı Öncelikli Tek Yönlü Senkronizasyon Testleri

Bu test paketi, cloud_sync modülünün:
1. CLOUD_DATABASE_URL boş olduğunda zararsız ve başarılı şekilde sonlandığını,
2. Yerel SQLite veritabanındaki Vehicle ve AccessLog verilerini hedef veritabanına aktardığını,
3. sync_id (UUID) bulunmayan eski yerel kayıtlara güvenle UUID atadığını,
4. Senkronizasyonun idempotent (tekrarlanabilir) olduğunu ve mükerrer kayıt oluşturmadığını,
5. --dry-run seçeneğinin veri yazmadan simülasyon yaptığını doğrular.

Not: Gerçek Google Cloud kaynaklarına erişim yapılmaz; iki adet geçici SQLite veritabanı kullanılır.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

# src klasörünü sys.path'e ekle
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, Vehicle, AccessLog, VehicleStatus, AccessDirection, AccessDecision, utc_now
from cloud_sync import run_sync, ensure_local_sync_ids, sync_vehicles, sync_access_logs


class TestCloudSync(unittest.TestCase):
    """
    Bulut senkronizasyon birim testleri.
    """

    def setUp(self):
        # Geçici yerel ve hedef bulut SQLite veritabanı dosyaları oluştur
        self.local_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.cloud_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)

        self.local_url = f"sqlite:///{self.local_db_file.name}"
        self.cloud_url = f"sqlite:///{self.cloud_db_file.name}"

        # Şemaları oluştur
        self.local_engine = create_engine(self.local_url, connect_args={"check_same_thread": False})
        self.cloud_engine = create_engine(self.cloud_url, connect_args={"check_same_thread": False})

        Base.metadata.create_all(bind=self.local_engine)
        Base.metadata.create_all(bind=self.cloud_engine)

        self.LocalSession = sessionmaker(bind=self.local_engine)
        self.CloudSession = sessionmaker(bind=self.cloud_engine)

        # Orijinal CLOUD_DATABASE_URL değerini sakla
        self.original_env = os.environ.get("CLOUD_DATABASE_URL")

    def tearDown(self):
        # Orijinal CLOUD_DATABASE_URL geri yükle
        if self.original_env is None:
            os.environ.pop("CLOUD_DATABASE_URL", None)
        else:
            os.environ["CLOUD_DATABASE_URL"] = self.original_env

        # Geçici dosyaları kapat ve sil
        self.local_db_file.close()
        self.cloud_db_file.close()

        try:
            os.remove(self.local_db_file.name)
        except OSError:
            pass

        try:
            os.remove(self.cloud_db_file.name)
        except OSError:
            pass

    def test_1_missing_cloud_url_exits_gracefully(self):
        """
        CLOUD_DATABASE_URL boş veya eksikse run_sync True dönmeli ve işlem yapmamalı.
        """
        os.environ.pop("CLOUD_DATABASE_URL", None)
        result = run_sync(local_url=self.local_url, cloud_url="", dry_run=False)
        self.assertTrue(result)

    def test_2_legacy_local_records_assigned_uuid(self):
        """
        sync_id alanı NULL/None olan eski yerel kayıtlara (legacy DB) güvenle UUID atanmalı.
        """
        simdi = utc_now()
        with self.LocalSession() as session:
            v_legacy = Vehicle(
                plate_text="34OLD01",
                normalized_plate="34OLD01",
                status=VehicleStatus.approved,
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            session.add(v_legacy)
            session.commit()

            # Veritabanında sync_id alanını NULL yap (eski veritabanı simülasyonu)
            session.execute(Vehicle.__table__.update().values(sync_id=None))
            session.commit()

            session.expire_all()
            v_reloaded = session.query(Vehicle).filter_by(normalized_plate="34OLD01").one()
            self.assertIsNone(v_reloaded.sync_id)

            # ensure_local_sync_ids çalıştır
            up_v, up_l = ensure_local_sync_ids(session)
            self.assertEqual(up_v, 1)

            # Atama sonrası sync_id dolu olmalı
            session.refresh(v_reloaded)
            self.assertIsNotNone(v_reloaded.sync_id)
            self.assertEqual(len(v_reloaded.sync_id), 36)

    def test_3_one_way_sync_and_idempotency(self):
        """
        Yerel veriler hedef bulut veritabanına aktarılmalı ve tekrar çalıştırıldığında mükerrer kayıt oluşmamalı.
        """
        simdi = utc_now()
        with self.LocalSession() as session:
            v1 = Vehicle(
                sync_id="test-uuid-vehicle-001",
                plate_text="34SYNC01",
                normalized_plate="34SYNC01",
                status=VehicleStatus.approved,
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            session.add(v1)
            session.commit()

            l1 = AccessLog(
                sync_id="test-uuid-log-001",
                vehicle_id=v1.id,
                plate_text="34SYNC01",
                normalized_plate="34SYNC01",
                direction=AccessDirection.entry,
                decision=AccessDecision.allow,
                ocr_confidence=0.98,
                detected_at=simdi,
                source_camera="cam_entry",
            )
            session.add(l1)
            session.commit()

        # 1. İlk Senkronizasyon
        success = run_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertTrue(success)

        # Hedef bulut veritabanını doğrula
        with self.CloudSession() as cloud_session:
            cloud_vehicles = cloud_session.query(Vehicle).all()
            cloud_logs = cloud_session.query(AccessLog).all()

            self.assertEqual(len(cloud_vehicles), 1)
            self.assertEqual(len(cloud_logs), 1)
            self.assertEqual(cloud_vehicles[0].sync_id, "test-uuid-vehicle-001")
            self.assertEqual(cloud_vehicles[0].normalized_plate, "34SYNC01")
            self.assertEqual(cloud_logs[0].sync_id, "test-uuid-log-001")
            self.assertEqual(cloud_logs[0].decision, AccessDecision.allow)

        # 2. İkinci Senkronizasyon (İdempotentlik Testi)
        success_repeat = run_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertTrue(success_repeat)

        # Mükerrer kayıt oluşmadığını doğrula
        with self.CloudSession() as cloud_session:
            cloud_vehicles_2 = cloud_session.query(Vehicle).all()
            cloud_logs_2 = cloud_session.query(AccessLog).all()

            self.assertEqual(len(cloud_vehicles_2), 1)
            self.assertEqual(len(cloud_logs_2), 1)

    def test_4_dry_run_mode_does_not_modify_cloud_db(self):
        """
        --dry-run seçeneği bulut veritabanına kayıt yazmamalı.
        """
        simdi = utc_now()
        with self.LocalSession() as session:
            v_dry = Vehicle(
                sync_id="test-uuid-dry-001",
                plate_text="34DRY01",
                normalized_plate="34DRY01",
                status=VehicleStatus.pending,
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            session.add(v_dry)
            session.commit()

        # Dry-run senkronizasyonu çalıştır
        success = run_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=True)
        self.assertTrue(success)

        # Hedef bulut veritabanının hâlâ boş olduğunu doğrula
        with self.CloudSession() as cloud_session:
            cloud_vehicles = cloud_session.query(Vehicle).all()
            self.assertEqual(len(cloud_vehicles), 0)


if __name__ == "__main__":
    unittest.main()
