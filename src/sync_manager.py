"""
sync_manager.py — Yerel Senkronizasyon Yöneticisi (Supervisor)

Bu modül, iki bağımsız senkronizasyon işçisini (worker) eşzamanlı (concurrent) olarak çalıştırır:
1. LOCAL -> CLOUD (cloud_sync.py) — Araç ve Erişim Logu aktarımı
2. CLOUD -> LOCAL (approval_sync.py) — Web panel yetki kararları aktarımı

İlkeler:
1. İki senkronizasyon döngüsü (threads) birbirini engellemez.
2. İnternet veya bulut kesintisinde manager kapanmaz; her işçi kendi aralığında tekrar dener.
3. Yerel OCR, kamera ve bariyer sisteminden tamamen bağımsız ayrı bir süreçtir.
4. Ctrl+C ile tüm işçiler durdurma sinyali (Event) alarak temiz bir şekilde sonlanır.
"""

import sys
import time
import argparse
import threading
from pathlib import Path
from typing import Optional

# src klasörünü import yoluna ekle
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from cloud_sync import run_sync
from approval_sync import run_approval_sync


class SyncManager:
    """
    Çift yönlü senkronizasyon işçilerini yöneten Thread Supervisor sınıfı.
    """

    def __init__(
        self,
        cloud_interval: int = 60,
        approval_interval: int = 30,
        local_url: Optional[str] = None,
        cloud_url: Optional[str] = None,
        dry_run: bool = False,
    ):
        if cloud_interval <= 0 or approval_interval <= 0:
            raise ValueError("Senkronizasyon aralıkları 0'dan büyük olmalıdır.")

        self.cloud_interval = cloud_interval
        self.approval_interval = approval_interval
        self.local_url = local_url
        self.cloud_url = cloud_url
        self.dry_run = dry_run

        self._stop_event = threading.Event()
        self._cloud_thread: Optional[threading.Thread] = None
        self._approval_thread: Optional[threading.Thread] = None

    def _cloud_sync_worker(self) -> None:
        """
        LOCAL -> CLOUD senkronizasyon döngüsü (Thread).
        """
        while not self._stop_event.is_set():
            try:
                run_sync(
                    local_url=self.local_url,
                    cloud_url=self.cloud_url,
                    dry_run=self.dry_run,
                )
            except Exception as e:
                print(f"[SYNC MANAGER HATA] LOCAL -> CLOUD worker istisnası: {e}")

            # Bekleme süresi boyunca stop_event'i dinle
            self._stop_event.wait(self.cloud_interval)

    def _approval_sync_worker(self) -> None:
        """
        CLOUD -> LOCAL yetki senkronizasyon döngüsü (Thread).
        """
        while not self._stop_event.is_set():
            try:
                run_approval_sync(
                    local_url=self.local_url,
                    cloud_url=self.cloud_url,
                    dry_run=self.dry_run,
                )
            except Exception as e:
                print(f"[SYNC MANAGER HATA] CLOUD -> LOCAL worker istisnası: {e}")

            # Bekleme süresi boyunca stop_event'i dinle
            self._stop_event.wait(self.approval_interval)

    def start(self) -> None:
        """
        Her iki senkronizasyon işçisini ayrı thread'lerde başlatır.
        """
        print("[SYNC MANAGER] Başlatıldı")
        print(f"LOCAL -> CLOUD interval: {self.cloud_interval}s")
        print(f"CLOUD -> LOCAL interval: {self.approval_interval}s")
        if self.dry_run:
            print("[SYNC MANAGER] Mod: DRY RUN (Yazma yapılmayacak)")

        self._stop_event.clear()

        self._cloud_thread = threading.Thread(
            target=self._cloud_sync_worker,
            name="CloudSyncWorker",
            daemon=True,
        )
        self._approval_thread = threading.Thread(
            target=self._approval_sync_worker,
            name="ApprovalSyncWorker",
            daemon=True,
        )

        self._cloud_thread.start()
        self._approval_thread.start()

    def stop(self) -> None:
        """
        Tüm senkronizasyon işçilerini temiz bir şekilde durdurur.
        """
        self._stop_event.set()
        if self._cloud_thread and self._cloud_thread.is_alive():
            self._cloud_thread.join(timeout=3.0)
        if self._approval_thread and self._approval_thread.is_alive():
            self._approval_thread.join(timeout=3.0)
        print("[SYNC MANAGER] Durduruldu.")

    def is_running(self) -> bool:
        """
        İşçilerin çalışıp çalışmadığını kontrol eder.
        """
        cloud_alive = self._cloud_thread is not None and self._cloud_thread.is_alive()
        approval_alive = self._approval_thread is not None and self._approval_thread.is_alive()
        return cloud_alive and approval_alive


def main() -> None:
    """
    CLI giriş noktası.
    """
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Plaka Tanima Sistemi - Cift Yonlu Senkronizasyon Yoneticisi"
    )
    parser.add_argument(
        "--cloud-interval",
        type=int,
        default=60,
        help="LOCAL -> CLOUD senkronizasyon aralığı (saniye, varsayılan: 60)",
    )
    parser.add_argument(
        "--approval-interval",
        type=int,
        default=30,
        help="CLOUD -> LOCAL yetki senkronizasyonu aralığı (saniye, varsayılan: 30)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Veritabanına yazmadan simülasyon modunda çalıştır",
    )
    parser.add_argument(
        "--local-url",
        type=str,
        default=None,
        help="Özel yerel veritabanı adresi",
    )
    parser.add_argument(
        "--cloud-url",
        type=str,
        default=None,
        help="Özel bulut veritabanı adresi",
    )

    args = parser.parse_args()

    if args.cloud_interval <= 0 or args.approval_interval <= 0:
        print("[SYNC MANAGER HATA] Senkronizasyon aralıkları 0'dan büyük olmalıdır.")
        sys.exit(1)

    manager = SyncManager(
        cloud_interval=args.cloud_interval,
        approval_interval=args.approval_interval,
        local_url=args.local_url,
        cloud_url=args.cloud_url,
        dry_run=args.dry_run,
    )

    try:
        manager.start()
        # Ana thread Ctrl+C gelene kadar bekler
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[SYNC MANAGER] Durdurma sinyali alındı...")
        manager.stop()


if __name__ == "__main__":
    main()
