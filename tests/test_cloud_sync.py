"""
tests/test_cloud_sync.py — Çevrimdışı Öncelikli Tek Yönlü Senkronizasyon Testleri

Bu test paketi, cloud_sync modülünün:
1. CLOUD_DATABASE_URL boş olduğunda zararsız ve başarılı şekilde sonlandığını,
2. Yerel SQLite veritabanındaki Vehicle ve AccessLog verilerini hedef veritabanına aktardığını,
3. Eklenen (new), güncellenen (updated) ve değişmeyen (unchanged) sayaçların doğru hesaplandığını,
4. sync_id (UUID) bulunmayan eski yerel kayıtlara güvenle UUID atadığını,
5. Senkronizasyonun idempotent (tekrarlanabilir) olduğunu ve mükerrer kayıt oluşturmadığını,
6. --dry-run seçeneğinin veri yazmadan simülasyon yaptığını,
7. --watch modunda bulut hatalarına karşı uygulamanın çökmeyip tekrar denediğini,
8. --interval doğrulamasını ve durdurma işlemlerini doğrular.

Not: Gerçek Google Cloud kaynaklarına erişim yapılmaz; geçici yerel SQLite veritabanları kullanılır.
"""

import os
import sys
import tempfile
import unittest
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
    sync_vehicles,
    sync_access_logs,
)


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
        CLOUD_DATABASE_URL boş veya eksikse run_sync (True, stats) dönmeli ve işlem yapmamalı.
        """
        os.environ.pop("CLOUD_DATABASE_URL", None)
        success, stats = run_sync(local_url=self.local_url, cloud_url="", dry_run=False)
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["new"], 0)

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

    def test_3_summary_counters_and_idempotency(self):
        """
        Senkronizasyon özet sayaçlarını (new, updated, unchanged) ve tekrarlanan senkronizasyonların mükerrer kayıt üretmediğini doğrular.
        """
        simdi = utc_now()
        with self.LocalSession() as session:
            v1 = Vehicle(
                sync_id="test-uuid-v-001",
                plate_text="34STAT01",
                normalized_plate="34STAT01",
                status=VehicleStatus.pending,
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            session.add(v1)
            session.commit()

            l1 = AccessLog(
                sync_id="test-uuid-l-001",
                vehicle_id=v1.id,
                plate_text="34STAT01",
                normalized_plate="34STAT01",
                direction=AccessDirection.entry,
                decision=AccessDecision.wait_for_approval,
                ocr_confidence=0.95,
                detected_at=simdi,
                source_camera="cam_0",
            )
            session.add(l1)
            session.commit()

        # 1. İlk Senkronizasyon (Tüm kayıtlar YENİ olmalı)
        success1, stats1 = run_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertTrue(success1)
        self.assertEqual(stats1["vehicles"]["new"], 1)
        self.assertEqual(stats1["vehicles"]["updated"], 0)
        self.assertEqual(stats1["vehicles"]["unchanged"], 0)
        self.assertEqual(stats1["access_logs"]["new"], 1)
        self.assertEqual(stats1["access_logs"]["unchanged"], 0)

        # 2. İkinci Senkronizasyon (Hiçbir değişiklik yok; tüm kayıtlar UNCHANGED olmalı)
        success2, stats2 = run_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertTrue(success2)
        self.assertEqual(stats2["vehicles"]["new"], 0)
        self.assertEqual(stats2["vehicles"]["updated"], 0)
        self.assertEqual(stats2["vehicles"]["unchanged"], 1)
        self.assertEqual(stats2["access_logs"]["new"], 0)
        self.assertEqual(stats2["access_logs"]["unchanged"], 1)

        # 3. Yerel araç durumunu güncelle (Değişiklik UPDATED olarak sayılmalı)
        with self.LocalSession() as session:
            v = session.query(Vehicle).filter_by(normalized_plate="34STAT01").one()
            v.status = VehicleStatus.approved
            v.approved_by = "test_admin"
            v.updated_at = utc_now()
            session.commit()

        success3, stats3 = run_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertTrue(success3)
        self.assertEqual(stats3["vehicles"]["new"], 0)
        self.assertEqual(stats3["vehicles"]["updated"], 1)
        self.assertEqual(stats3["vehicles"]["unchanged"], 0)
        self.assertEqual(stats3["access_logs"]["unchanged"], 1)

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
        success, stats = run_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=True)
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["new"], 1)

        # Hedef bulut veritabanının hâlâ boş olduğunu doğrula
        with self.CloudSession() as cloud_session:
            cloud_vehicles = cloud_session.query(Vehicle).all()
            self.assertEqual(len(cloud_vehicles), 0)

    def test_5_interval_validation(self):
        """
        Interval 0 veya negatif girildiğinde ValueError fırlatılmalı.
        """
        with self.assertRaises(ValueError):
            run_watch_mode(local_url=self.local_url, cloud_url=self.cloud_url, interval=0, max_iterations=1)

        with self.assertRaises(ValueError):
            run_watch_mode(local_url=self.local_url, cloud_url=self.cloud_url, interval=-10, max_iterations=1)

    @patch("time.sleep", return_value=None)
    def test_6_watch_mode_retry_after_simulated_cloud_failure(self, mock_sleep):
        """
        Hatalı bir bulut veritabanı URL'si durumunda watch modu çökmeyip sonraki döngüde tekrar denemeli.
        """
        invalid_cloud_url = "sqlite:///invalid_dir/non_existent_folder/cloud.db"

        # 2 döngü boyunca çalış ve hataları yakalayarak devretmesini doğrula
        run_watch_mode(
            local_url=self.local_url,
            cloud_url=invalid_cloud_url,
            interval=1,
            max_iterations=2,
        )

    @patch("time.sleep", side_effect=KeyboardInterrupt)
    def test_7_watch_mode_graceful_stop_on_keyboard_interrupt(self, mock_sleep):
        """
        KeyboardInterrupt (Ctrl+C) alındığında worker temiz bir şekilde sonlanmalı.
        """
        try:
            run_watch_mode(
                local_url=self.local_url,
                cloud_url=self.cloud_url,
                interval=1,
            )
        except KeyboardInterrupt:
            self.fail("KeyboardInterrupt dışarı sızmamalı, ele alınmalıydı!")

    def test_8_datetime_normalization_timezone_diff_unchanged(self):
        """
        Timezone-aware ve timezone-naive aynı zamanı temsil eden datetime değerlerinin
        false-positive update üretmediğini doğrular.
        """
        from cloud_sync import normalize_dt, vehicle_has_changes, normalize_str
        from datetime import datetime, timezone

        dt_naive = datetime(2026, 8, 8, 12, 0, 0)
        dt_aware = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)

        self.assertEqual(normalize_dt(dt_naive), normalize_dt(dt_aware))

        # vehicle_has_changes simülasyonu
        simdi_naive = dt_naive
        simdi_aware = dt_aware

        v1 = Vehicle(
            sync_id="tz-test-001",
            plate_text="34TZ01",
            normalized_plate="34TZ01",
            status=VehicleStatus.approved,
            first_seen_at=simdi_naive,
            last_seen_at=simdi_naive,
            created_at=simdi_naive,
            updated_at=simdi_naive,
        )
        v2 = Vehicle(
            sync_id="tz-test-001",
            plate_text="34TZ01",
            normalized_plate="34TZ01",
            status=VehicleStatus.approved,
            first_seen_at=simdi_aware,
            last_seen_at=simdi_aware,
            created_at=simdi_aware,
            updated_at=simdi_aware,
        )

        self.assertFalse(vehicle_has_changes(v1, v2))

    def test_9_vehicle_one_field_update_and_subsequent_sync_unchanged(self):
        """
        Bir araç alanı güncellendiğinde ilk senkronizasyonda updated=1 sayılmalı,
        sonraki senkronizasyonlarda değişiklik yoksa unchanged=1 olmalıdır.
        """
        simdi = utc_now()
        with self.LocalSession() as session:
            v = Vehicle(
                sync_id="uuid-step-001",
                plate_text="34STEP01",
                normalized_plate="34STEP01",
                status=VehicleStatus.pending,
                first_seen_at=simdi,
                last_seen_at=simdi,
                created_at=simdi,
                updated_at=simdi,
            )
            session.add(v)
            session.commit()

        # 1. İlk Senkronizasyon (YENİ)
        _, stats1 = run_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertEqual(stats1["vehicles"]["new"], 1)
        self.assertEqual(stats1["vehicles"]["updated"], 0)
        self.assertEqual(stats1["vehicles"]["unchanged"], 0)

        # 2. Değişiklik yok (UNCHANGED)
        _, stats2 = run_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertEqual(stats2["vehicles"]["new"], 0)
        self.assertEqual(stats2["vehicles"]["updated"], 0)
        self.assertEqual(stats2["vehicles"]["unchanged"], 1)

        # 3. 1 Alan Değişikliği (UPDATED: 1)
        with self.LocalSession() as session:
            v_mod = session.query(Vehicle).filter_by(normalized_plate="34STEP01").one()
            v_mod.notes = "VIP Misafir"
            v_mod.updated_at = utc_now()
            session.commit()

        _, stats3 = run_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertEqual(stats3["vehicles"]["new"], 0)
        self.assertEqual(stats3["vehicles"]["updated"], 1)
        self.assertEqual(stats3["vehicles"]["unchanged"], 0)

        # 4. Takip eden senkronizasyon (Değişiklik yapılmadı -> UNCHANGED: 1)
        _, stats4 = run_sync(local_url=self.local_url, cloud_url=self.cloud_url, dry_run=False)
        self.assertEqual(stats4["vehicles"]["new"], 0)
        self.assertEqual(stats4["vehicles"]["updated"], 0)
        self.assertEqual(stats4["vehicles"]["unchanged"], 1)


if __name__ == "__main__":
    unittest.main()

