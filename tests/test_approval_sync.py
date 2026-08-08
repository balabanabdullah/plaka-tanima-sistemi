"""
tests/test_approval_sync.py — Buluttan Yerele Yetki Senkronizasyonu Birim Testleri

Bu test paketi, approval_sync modülünün:
1. Buluttaki onay (approved) durumunu yereldeki bekleyen (pending) araca doğru aktardığını,
2. Buluttaki ret (rejected) durumunu yereldeki araca aktardığını,
3. Değişmeyen yetki değerlerinde gereksiz veritabanı güncellemesi yapmadığını,
4. Yerel operasyonel alanları (first_seen_at, last_seen_at, created_at, plate_text, normalized_plate) KESİNLİKLE KORUDUĞUNU,
5. Yerelde karşılığı olmayan bulut araçları için yerelde otomatik sahte kayıt OLUŞTURMADIĞINI (unmatched),
6. Senkronizasyonun idempotent olduğunu,
7. --dry-run seçeneğinin yerel veritabanına yazmadığını,
8. Bulut bağlantı hatalarında watch modunun çökmediğini doğrular.

Not: Gerçek Google Cloud kaynaklarına erişim yapılmaz; geçici yerel SQLite veritabanları kullanılır.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone

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
    sync_approvals_from_cloud,
    vehicle_approval_has_changes,
)


class TestApprovalSync(unittest.TestCase):
    """
    Buluttan yerele yetki senkronizasyonu testleri.
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

    def test_1_approved_cloud_status_updates_local_pending_vehicle(self):
        """
        Bulutta onaylanan (approved) araç yereldeki pending aracı güncellemeli.
        """
        simdi = utc_now()
        with self.LocalSession() as local_session:
            v_local = Vehicle(
                sync_id="test-uuid-app-001",
                plate_text="34APP01",
                normalized_plate="34APP01",
                status=VehicleStatus.pending,
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            local_session.add(v_local)
            local_session.commit()

        with self.CloudSession() as cloud_session:
            v_cloud = Vehicle(
                sync_id="test-uuid-app-001",
                plate_text="34APP01",
                normalized_plate="34APP01",
                status=VehicleStatus.approved,
                approved_at=simdi,
                approved_by="web_admin",
                notes="Güvenlik Onayladı",
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            cloud_session.add(v_cloud)
            cloud_session.commit()

        # Yetki Senkronizasyonunu Çalıştır (CLOUD -> LOCAL)
        success, stats = run_approval_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["updated"], 1)

        # Yerel veritabanını doğrula
        with self.LocalSession() as local_session:
            lv = local_session.query(Vehicle).filter_by(normalized_plate="34APP01").one()
            self.assertEqual(lv.status, VehicleStatus.approved)
            self.assertEqual(lv.approved_by, "web_admin")
            self.assertEqual(lv.notes, "Güvenlik Onayladı")

    def test_2_rejected_cloud_status_updates_local_pending_vehicle(self):
        """
        Bulutta reddedilen (rejected) araç yereldeki pending aracı güncellemeli.
        """
        simdi = utc_now()
        with self.LocalSession() as local_session:
            v_local = Vehicle(
                sync_id="test-uuid-app-002",
                plate_text="34REJ01",
                normalized_plate="34REJ01",
                status=VehicleStatus.pending,
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            local_session.add(v_local)
            local_session.commit()

        with self.CloudSession() as cloud_session:
            v_cloud = Vehicle(
                sync_id="test-uuid-app-002",
                plate_text="34REJ01",
                normalized_plate="34REJ01",
                status=VehicleStatus.rejected,
                notes="Yasaklı Araç Listesi",
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            cloud_session.add(v_cloud)
            cloud_session.commit()

        success, stats = run_approval_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["updated"], 1)

        with self.LocalSession() as local_session:
            lv = local_session.query(Vehicle).filter_by(normalized_plate="34REJ01").one()
            self.assertEqual(lv.status, VehicleStatus.rejected)
            self.assertEqual(lv.notes, "Yasaklı Araç Listesi")

    def test_3_unchanged_values_stay_unchanged(self):
        """
        Bulut ve yerel yetkiler aynı olduğunda updated=0, unchanged=1 olmalıdır.
        """
        simdi = utc_now()
        with self.LocalSession() as local_session:
            v_local = Vehicle(
                sync_id="test-uuid-app-003",
                plate_text="34UNC01",
                normalized_plate="34UNC01",
                status=VehicleStatus.approved,
                approved_at=simdi,
                approved_by="admin",
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            local_session.add(v_local)
            local_session.commit()

        with self.CloudSession() as cloud_session:
            v_cloud = Vehicle(
                sync_id="test-uuid-app-003",
                plate_text="34UNC01",
                normalized_plate="34UNC01",
                status=VehicleStatus.approved,
                approved_at=simdi,
                approved_by="admin",
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            cloud_session.add(v_cloud)
            cloud_session.commit()

        success, stats = run_approval_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["updated"], 0)
        self.assertEqual(stats["vehicles"]["unchanged"], 1)

    def test_4_local_operational_fields_preserved(self):
        """
        Buluttan yerele yetki aktarılırken first_seen_at, last_seen_at, created_at, plate_text kesinlikle korunmalıdır.
        """
        t_local_first = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
        t_local_last = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)
        t_cloud = datetime(2026, 8, 5, 0, 0, 0, tzinfo=timezone.utc)

        with self.LocalSession() as local_session:
            v_local = Vehicle(
                sync_id="test-uuid-app-004",
                plate_text="34 LOCAL RAW",
                normalized_plate="34LOCALRAW",
                status=VehicleStatus.pending,
                first_seen_at=t_local_first,
                last_seen_at=t_local_last,
                created_at=t_local_first,
                updated_at=t_local_last,
            )
            local_session.add(v_local)
            local_session.commit()

        with self.CloudSession() as cloud_session:
            v_cloud = Vehicle(
                sync_id="test-uuid-app-004",
                plate_text="34 CLOUD OVERWRITE ATTEMPT",
                normalized_plate="34LOCALRAW",
                status=VehicleStatus.approved,
                approved_by="cloud_user",
                first_seen_at=t_cloud,
                last_seen_at=t_cloud,
                created_at=t_cloud,
                updated_at=t_cloud,
            )
            cloud_session.add(v_cloud)
            cloud_session.commit()

        run_approval_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)

        with self.LocalSession() as local_session:
            lv = local_session.query(Vehicle).filter_by(normalized_plate="34LOCALRAW").one()
            # Yetki güncellenmiş olmalı
            self.assertEqual(lv.status, VehicleStatus.approved)
            self.assertEqual(lv.approved_by, "cloud_user")
            # Operasyonel alanlar KORUNMUŞ olmalı
            self.assertEqual(lv.plate_text, "34 LOCAL RAW")
            self.assertEqual(lv.first_seen_at.replace(tzinfo=timezone.utc), t_local_first)
            self.assertEqual(lv.last_seen_at.replace(tzinfo=timezone.utc), t_local_last)

    def test_5_unmatched_cloud_vehicle_does_not_create_local_record(self):
        """
        Bulutta var olan ancak yerelde henüz kameraya takılmamış bir araç için yerelde otomatik kayıt açılmamalıdır.
        """
        simdi = utc_now()
        with self.CloudSession() as cloud_session:
            v_cloud = Vehicle(
                sync_id="test-uuid-unmatched-001",
                plate_text="34ONLYCLOUD",
                normalized_plate="34ONLYCLOUD",
                status=VehicleStatus.approved,
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            cloud_session.add(v_cloud)
            cloud_session.commit()

        success, stats = run_approval_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["unmatched"], 1)
        self.assertEqual(stats["vehicles"]["updated"], 0)

        with self.LocalSession() as local_session:
            count = local_session.query(Vehicle).count()
            self.assertEqual(count, 0)

    def test_6_repeated_sync_is_idempotent(self):
        """
        Tekrarlanan yetki senkronizasyonu mükerrer veritabanı güncellemesi yapmamalıdır.
        """
        simdi = utc_now()
        with self.LocalSession() as local_session:
            v_local = Vehicle(
                sync_id="test-uuid-idemp-001",
                plate_text="34IDEMP01",
                normalized_plate="34IDEMP01",
                status=VehicleStatus.pending,
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            local_session.add(v_local)
            local_session.commit()

        with self.CloudSession() as cloud_session:
            v_cloud = Vehicle(
                sync_id="test-uuid-idemp-001",
                plate_text="34IDEMP01",
                normalized_plate="34IDEMP01",
                status=VehicleStatus.approved,
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            cloud_session.add(v_cloud)
            cloud_session.commit()

        # İlk senkronizasyon -> updated: 1
        _, stats1 = run_approval_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertEqual(stats1["vehicles"]["updated"], 1)

        # İkinci senkronizasyon -> unchanged: 1, updated: 0
        _, stats2 = run_approval_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertEqual(stats2["vehicles"]["updated"], 0)
        self.assertEqual(stats2["vehicles"]["unchanged"], 1)

    def test_7_dry_run_does_not_modify_local_db(self):
        """
        --dry-run seçeneği yerel veritabanını değiştirmemelidir.
        """
        simdi = utc_now()
        with self.LocalSession() as local_session:
            v_local = Vehicle(
                sync_id="test-uuid-dry-app-001",
                plate_text="34DRYAPP01",
                normalized_plate="34DRYAPP01",
                status=VehicleStatus.pending,
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            local_session.add(v_local)
            local_session.commit()

        with self.CloudSession() as cloud_session:
            v_cloud = Vehicle(
                sync_id="test-uuid-dry-app-001",
                plate_text="34DRYAPP01",
                normalized_plate="34DRYAPP01",
                status=VehicleStatus.approved,
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            cloud_session.add(v_cloud)
            cloud_session.commit()

        success, stats = run_approval_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=True)
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["updated"], 1)

        # Yerel veritabanının HÂLÂ pending olduğunu doğrula
        with self.LocalSession() as local_session:
            lv = local_session.query(Vehicle).filter_by(normalized_plate="34DRYAPP01").one()
            self.assertEqual(lv.status, VehicleStatus.pending)

    @patch("time.sleep", return_value=None)
    def test_8_simulated_cloud_failure_does_not_crash_watch_mode(self, mock_sleep):
        """
        Hatalı bir bulut veritabanı durumunda watch modu çökmeyip devretmelidir.
        """
        invalid_cloud_url = "sqlite:///invalid_folder/cloud.db"

        run_watch_mode(
            local_url=self.local_url,
            cloud_url=invalid_cloud_url,
            interval=1,
            max_iterations=2,
        )

        self.assertEqual(mock_sleep.call_count, 1)


if __name__ == "__main__":
    unittest.main()
