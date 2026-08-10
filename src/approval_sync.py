"""
approval_sync.py — Çevrimdışı Öncelikli Buluttan Yerele HTTPS Yetki Senkronizasyon Modülü

Bu modül, web panelinden (Cloud SQL / Cloud Run) verilen araç yetkilendirme kararlarını
(status, approved_at, approved_by, notes) Cloud Run HTTPS API (/api/sync/approvals)
üzerinden yerel SQLite veritabanına aktarır (CLOUD -> LOCAL).

Önemli Kurallar:
1. Yalnızca yetkilendirme alanları (status, approved_at, approved_by, notes) aktarılır.
2. Yerel operasyonel alanlar (first_seen_at, last_seen_at, created_at, plate_text, normalized_plate)
   kesinlikle bulut verisiyle ezilmez, yerel veri korunur.
3. AccessLog kayıtları geriye aktarılmaz.
4. Eşleşmeyen bulut araçları için yerelde otomatik kayıt oluşturulmaz (unmatched olarak sayılır).
5. Senkronizasyon idempottur. Değişiklik yoksa yerel veritabanına yazma yapılmaz.
"""

import sys
import os
import time
import argparse
import json
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone
from typing import Tuple, Dict, Any, List, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

# src klasörünü import yoluna ekle
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from database import DEFAULT_DB_PATH
from models import Base, Vehicle, VehicleStatus, utc_now
from cloud_sync import normalize_dt, normalize_str


def get_local_db_url() -> str:
    """Senkronizasyon için varsayılan yerel SQLite URL'sini döndürür."""
    return f"sqlite:///{DEFAULT_DB_PATH}"


def get_cloud_sync_api_url() -> str:
    """CLOUD_SYNC_API_URL çevre değişkeninden bulut HTTPS API adresini okur."""
    return os.environ.get("CLOUD_SYNC_API_URL", "").strip()


def get_sync_api_token() -> str:
    """SYNC_API_TOKEN çevre değişkeninden senkronizasyon yetki token'ını okur."""
    return os.environ.get("SYNC_API_TOKEN", "").strip()


def fetch_approvals_from_cloud_api(
    sync_api_url: str,
    sync_token: str,
    vehicle_sync_ids: List[str],
    timeout: float = 15.0,
) -> Tuple[bool, List[dict]]:
    """
    Python standart kütüphanesi (urllib.request) ile buluttan yetki kararlarını sorgular.
    Token hiçbir şekilde loglanmaz veya ekrana yazdırılmaz.
    """
    url = sync_api_url.rstrip("/") + "/api/sync/approvals"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {sync_token}",
        "User-Agent": "Plaka-Local-Approval-Sync/1.0",
    }
    payload = {"vehicle_sync_ids": vehicle_sync_ids}

    try:
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                res_body = response.read().decode("utf-8")
                return True, json.loads(res_body)
            else:
                print(f"[APPROVAL SYNC HATA] Sunucu yanıtı: HTTP {response.status}")
                return False, []
    except urllib.error.HTTPError as e:
        print(f"[APPROVAL SYNC HATA] HTTP Hatası: {e.code}")
        return False, []
    except urllib.error.URLError as e:
        print(f"[APPROVAL SYNC HATA] Ağ Bağlantı Hatası: {e.reason}")
        return False, []
    except Exception as e:
        print(f"[APPROVAL SYNC HATA] Yetki senkronizasyon isteği başarısız: {e}")
        return False, []


def parse_iso_dt(dt_str: Optional[str]) -> Optional[datetime]:
    """ISO formatındaki tarih metnini datetime nesnesine dönüştürür."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def get_sync_api_config(
    sync_api_url: str | None = None,
    sync_token: str | None = None,
) -> Tuple[str, str]:
    """
    Parametre olarak verilen veya ortam değişkenlerinden okunan güncel
    HTTPS API URL ve Token değerlerini döndürür.
    """
    resolved_url = (
        sync_api_url
        if sync_api_url is not None
        else os.environ.get("CLOUD_SYNC_API_URL", "").strip()
    )
    resolved_token = (
        sync_token
        if sync_token is not None
        else os.environ.get("SYNC_API_TOKEN", "").strip()
    )
    return resolved_url, resolved_token


def run_approval_sync(
    local_url: str | None = None,
    sync_api_url: str | None = None,
    sync_token: str | None = None,
    cloud_url: str | None = None,
    dry_run: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Buluttan yerele yetkilendirme senkronizasyonunu (CLOUD -> LOCAL) HTTPS API üzerinden yürütür.

    Döndürür:
        Tuple[bool, Dict[str, Any]]: (başarılı_mı, istatistikler_sözlüğü)
    """
    start_time = time.monotonic()
    stats: Dict[str, Any] = {
        "vehicles": {"updated": 0, "unchanged": 0, "unmatched": 0},
        "duration": 0.0,
    }

    resolved_local_url = local_url if local_url is not None else get_local_db_url()
    resolved_api_url, resolved_token = get_sync_api_config(sync_api_url, sync_token)

    if not resolved_api_url or not resolved_token:
        print("[APPROVAL SYNC] CLOUD_SYNC_API_URL veya SYNC_API_TOKEN tanımlanmamış.")
        print("[APPROVAL SYNC] Senkronizasyon atlandı. Yerel uygulama kesintisiz çalışmaya devam ediyor.")
        return True, stats

    try:
        local_connect_args = {"check_same_thread": False} if resolved_local_url.startswith("sqlite") else {}
        local_engine = create_engine(resolved_local_url, connect_args=local_connect_args)
        Base.metadata.create_all(bind=local_engine)
        from database import _ensure_schema_upgrades
        _ensure_schema_upgrades(local_engine)
        LocalSession = sessionmaker(bind=local_engine)

        with LocalSession() as local_session:
            local_vehicles = local_session.query(Vehicle).all()
            if not local_vehicles:
                duration = time.monotonic() - start_time
                stats["duration"] = round(duration, 2)
                print("[APPROVAL SYNC] Yerelde sorgulanacak araç yok.")
                return True, stats

            vehicle_sync_ids = [v.sync_id for v in local_vehicles if v.sync_id]
            if not vehicle_sync_ids:
                duration = time.monotonic() - start_time
                stats["duration"] = round(duration, 2)
                print("[APPROVAL SYNC] Yerelde sync_id tanımlı araç yok.")
                return True, stats

            success, cloud_approvals = fetch_approvals_from_cloud_api(
                resolved_api_url, resolved_token, vehicle_sync_ids
            )
            duration = time.monotonic() - start_time

            if not success:
                print("[APPROVAL SYNC HATA] Yerel veriler korundu. Yerel OCR ve bariyer sistemi çalışmaya devam ediyor.")
                stats["duration"] = round(duration, 2)
                return False, stats

            # Buluttan gelen yanıtları sync_id -> approval_dict olarak indeksle
            approvals_by_sync_id = {item["sync_id"]: item for item in cloud_approvals if "sync_id" in item}

            updated_count = 0
            unchanged_count = 0
            unmatched_count = 0

            for lv in local_vehicles:
                if not lv.sync_id or lv.sync_id not in approvals_by_sync_id:
                    unmatched_count += 1
                    continue

                ca = approvals_by_sync_id[lv.sync_id]
                new_status_str = ca.get("status")
                try:
                    new_status_enum = VehicleStatus(new_status_str) if new_status_str else lv.status
                except ValueError:
                    new_status_enum = lv.status

                new_approved_at = parse_iso_dt(ca.get("approved_at"))
                new_approved_by = ca.get("approved_by")
                new_notes = ca.get("notes")

                # Değişiklik var mı kontrol et
                has_changes = False
                if normalize_str(new_status_enum) != normalize_str(lv.status):
                    has_changes = True
                if normalize_str(new_approved_by) != normalize_str(lv.approved_by):
                    has_changes = True
                if normalize_str(new_notes) != normalize_str(lv.notes):
                    has_changes = True
                if normalize_dt(new_approved_at) != normalize_dt(lv.approved_at):
                    has_changes = True

                if has_changes:
                    if not dry_run:
                        lv.status = new_status_enum
                        lv.approved_at = new_approved_at
                        lv.approved_by = new_approved_by
                        lv.notes = new_notes
                        lv.updated_at = utc_now()
                    updated_count += 1
                else:
                    unchanged_count += 1

            if not dry_run and updated_count > 0:
                local_session.commit()

            stats = {
                "vehicles": {"updated": updated_count, "unchanged": unchanged_count, "unmatched": unmatched_count},
                "duration": round(duration, 2),
            }

            print("[APPROVAL SYNC]")
            print("Vehicles:")
            print(f"  updated: {updated_count}")
            print(f"  unchanged: {unchanged_count}")
            print(f"  unmatched: {unmatched_count}")
            print("")
            print(f"Duration: {duration:.2f} seconds")

            return True, stats

    except Exception as e:
        print(f"[APPROVAL SYNC HATA] Senkronizasyon hatası: {e}")
        print("[APPROVAL SYNC HATA] Yerel veriler korundu. Yerel OCR ve bariyer sistemi çalışmaya devam ediyor.")
        duration = time.monotonic() - start_time
        stats["duration"] = round(duration, 2)
        return False, stats


def run_watch_mode(
    local_url: str | None = None,
    sync_api_url: str | None = None,
    sync_token: str | None = None,
    cloud_url: str | None = None,
    dry_run: bool = False,
    interval: int = 30,
    max_iterations: int | None = None,
) -> None:
    """Sürekli yetki senkronizasyonu (watch) modunu yürütür."""
    if interval <= 0:
        print("[APPROVAL SYNC HATA] --interval 0'dan büyük bir tamsayı olmalıdır.")
        raise ValueError("--interval 0'dan büyük bir tamsayı olmalıdır.")

    print(f"[APPROVAL SYNC] Worker başlatıldı (Interval: {interval}s, Dry-Run: {dry_run})")
    iterations = 0
    try:
        while True:
            run_approval_sync(
                local_url=local_url,
                sync_api_url=sync_api_url,
                sync_token=sync_token,
                cloud_url=cloud_url,
                dry_run=dry_run,
            )
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[APPROVAL SYNC] Worker durduruldu.")


def main() -> None:
    """CLI giriş noktası."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Plaka Tanıma Sistemi - HTTPS API Buluttan Yerele Yetki Senkronizasyonu"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Sürekli yetki senkronizasyonu (worker) modunu başlat",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Sürekli modda senkronizasyon aralığı (saniye, varsayılan: 30)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Yerel veritabanına yazmadan senkronize edilecek yetkileri incele",
    )
    parser.add_argument(
        "--local-url",
        type=str,
        default=None,
        help="Özel yerel veritabanı adresi (Varsayılan: data/plate_system.db)",
    )
    parser.add_argument(
        "--sync-api-url",
        type=str,
        default=None,
        help="Özel bulut HTTPS API adresi (Varsayılan: CLOUD_SYNC_API_URL)",
    )
    parser.add_argument(
        "--sync-token",
        type=str,
        default=None,
        help="Özel senkronizasyon token'ı (Varsayılan: SYNC_API_TOKEN)",
    )

    args = parser.parse_args()

    if args.interval <= 0:
        print("[APPROVAL SYNC HATA] --interval 0'dan büyük bir tamsayı olmalıdır.")
        sys.exit(1)

    if args.watch:
        run_watch_mode(
            local_url=args.local_url,
            sync_api_url=args.sync_api_url,
            sync_token=args.sync_token,
            dry_run=args.dry_run,
            interval=args.interval,
        )
    else:
        success, _ = run_approval_sync(
            local_url=args.local_url,
            sync_api_url=args.sync_api_url,
            sync_token=args.sync_token,
            dry_run=args.dry_run,
        )
        if not success:
            sys.exit(1)


if __name__ == "__main__":
    main()
