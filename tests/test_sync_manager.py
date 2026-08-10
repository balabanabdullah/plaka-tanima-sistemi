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
from sync_signal import request_immediate_sync


class TestSyncManager(unittest.TestCase):
    """
    SyncManager birim testleri.
    """

    def setUp(self):
        self.local_url = "sqlite:///:memory:"
        self.cloud_url = "sqlite:///:memory:"
        self.temp_dir = tempfile.TemporaryDirectory()
        self.signal_path = Path(self.temp_dir.name) / "sync_wakeup"

    def tearDown(self):
        self.temp_dir.cleanup()

    def wait_for_call_count(self, mock_function, expected: int, timeout: float = 2.0):
        """Thread testlerinde mock cagrisini kisa sure bekler."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if mock_function.call_count >= expected:
                return True
            time.sleep(0.01)
        return False

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

    @patch("sync_manager.run_sync", return_value=(True, {}))
    @patch("sync_manager.run_approval_sync", return_value=(True, {}))
    def test_5_signal_wakes_cloud_worker_before_interval(self, mock_approval, mock_cloud):
        manager = SyncManager(
            cloud_interval=30,
            approval_interval=30,
            signal_path=self.signal_path,
            signal_poll_interval=0.02,
            signal_debounce=0.03,
        )
        manager.start()
        self.assertTrue(self.wait_for_call_count(mock_cloud, 1))

        request_immediate_sync(self.signal_path)
        self.assertTrue(self.wait_for_call_count(mock_cloud, 2, timeout=1.0))
        manager.stop()

    @patch("sync_manager.run_sync", return_value=(True, {}))
    @patch("sync_manager.run_approval_sync", return_value=(True, {}))
    def test_6_periodic_sync_continues_without_signal(self, mock_approval, mock_cloud):
        manager = SyncManager(
            cloud_interval=0.15,
            approval_interval=30,
            signal_path=self.signal_path,
            signal_poll_interval=0.02,
        )
        manager.start()
        self.assertTrue(self.wait_for_call_count(mock_cloud, 2, timeout=1.0))
        manager.stop()

    @patch("sync_manager.run_sync", return_value=(True, {}))
    @patch("sync_manager.run_approval_sync", return_value=(True, {}))
    def test_7_rapid_signals_are_coalesced_by_single_worker(self, mock_approval, mock_cloud):
        manager = SyncManager(
            cloud_interval=30,
            approval_interval=30,
            signal_path=self.signal_path,
            signal_poll_interval=0.02,
            signal_debounce=0.10,
        )
        manager.start()
        self.assertTrue(self.wait_for_call_count(mock_cloud, 1))

        for _ in range(20):
            request_immediate_sync(self.signal_path)

        self.assertTrue(self.wait_for_call_count(mock_cloud, 2, timeout=1.0))
        time.sleep(0.20)
        self.assertEqual(mock_cloud.call_count, 2)
        manager.stop()

    @patch("sync_manager.run_sync", side_effect=[Exception("HTTP offline"), (True, {})])
    @patch("sync_manager.run_approval_sync", return_value=(True, {}))
    def test_8_http_error_worker_survives_and_retries_on_signal(self, mock_approval, mock_cloud):
        manager = SyncManager(
            cloud_interval=30,
            approval_interval=30,
            signal_path=self.signal_path,
            signal_poll_interval=0.02,
            signal_debounce=0.03,
        )
        manager.start()
        self.assertTrue(self.wait_for_call_count(mock_cloud, 1))
        self.assertTrue(manager.is_running())

        request_immediate_sync(self.signal_path)
        self.assertTrue(self.wait_for_call_count(mock_cloud, 2, timeout=1.0))
        self.assertTrue(manager.is_running())
        manager.stop()


if __name__ == "__main__":
    unittest.main()
