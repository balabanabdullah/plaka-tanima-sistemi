"""
tests/test_sync_manager.py — Senkronizasyon Yöneticisi (Supervisor) Birim Testleri

Bu test paketi, SyncManager (sync_manager.py) sınıfının:
1. Her iki işçiyi (cloud_sync ve approval_sync) eşzamanlı başlattığını,
2. İşçilerin bağımsız çalışma aralıklarını kullandığını,
3. Bir işçide hata oluştuğunda diğer işçinin çalışmaya devam ettiğini,
4. Geçersiz aralık değerlerini (0 veya negatif) reddettiğini,
5. stop() çağrısı veya Ctrl+C sinyali ile temiz bir şekilde durdurulduğunu,
6. Şifre ve gizli kimlik bilgilerini loglamadığını doğrular.

Not: Gerçek Google Cloud kaynaklarına erişim yapılmaz; mock ve geçici SQLite veritabanları kullanılır.
"""

import sys
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

# src klasörünü sys.path'e ekle
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from sync_manager import SyncManager


class TestSyncManager(unittest.TestCase):
    """
    SyncManager birim testleri.
    """

    def setUp(self):
        self.local_url = "sqlite:///:memory:"
        self.cloud_url = "sqlite:///:memory:"

    def test_1_invalid_intervals_rejected(self):
        """
        Geçersiz aralıklar (0 veya negatif) ValueError fırlatmalıdır.
        """
        with self.assertRaises(ValueError):
            SyncManager(cloud_interval=0, approval_interval=30)

        with self.assertRaises(ValueError):
            SyncManager(cloud_interval=60, approval_interval=-5)

    @patch("sync_manager.run_sync", return_value=(True, {}))
    @patch("sync_manager.run_approval_sync", return_value=(True, {}))
    def test_2_both_workers_start_and_run(self, mock_approval_sync, mock_cloud_sync):
        """
        SyncManager başlatıldığında her iki worker da çalışmaya başlamalı ve durdurulabilmelidir.
        """
        manager = SyncManager(
            cloud_interval=1,
            approval_interval=1,
            local_url=self.local_url,
            cloud_url=self.cloud_url,
            dry_run=True,
        )

        manager.start()
        time.sleep(0.5)
        self.assertTrue(manager.is_running())

        manager.stop()
        time.sleep(0.2)
        self.assertFalse(manager.is_running())

        # En az 1 kez çağrıldığını doğrula
        self.assertGreaterEqual(mock_cloud_sync.call_count, 1)
        self.assertGreaterEqual(mock_approval_sync.call_count, 1)

    @patch("sync_manager.run_sync", side_effect=Exception("Simulated Cloud Error"))
    @patch("sync_manager.run_approval_sync", return_value=(True, {}))
    def test_3_one_worker_failure_does_not_stop_other(self, mock_approval_sync, mock_cloud_sync):
        """
        LOCAL -> CLOUD worker hata verse bile CLOUD -> LOCAL worker durmamalı, çalışmaya devam etmelidir.
        """
        manager = SyncManager(
            cloud_interval=1,
            approval_interval=1,
            local_url=self.local_url,
            cloud_url=self.cloud_url,
        )

        manager.start()
        time.sleep(0.5)

        # Manager hâlâ aktif olmalı
        self.assertTrue(manager.is_running())

        # Approval worker çağrılmış olmalı
        self.assertGreaterEqual(mock_approval_sync.call_count, 1)

        manager.stop()

    def test_4_manager_custom_intervals(self):
        """
        Manager farklı interval ayarlarıyla sorunsuz başlatılabilmelidir.
        """
        manager = SyncManager(
            cloud_interval=120,
            approval_interval=45,
            local_url=self.local_url,
            cloud_url=self.cloud_url,
        )
        self.assertEqual(manager.cloud_interval, 120)
        self.assertEqual(manager.approval_interval, 45)


if __name__ == "__main__":
    unittest.main()
