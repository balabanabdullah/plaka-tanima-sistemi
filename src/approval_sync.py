"""
approval_sync.py — Çevrimdışı Öncelikli Buluttan Yerele Yetki Senkronizasyon Modülü

Bu modül, web panelinden (Cloud SQL PostgreSQL) verilen araç yetkilendirme kararlarını
(status, approved_at, approved_by, notes) yerel SQLite veritabanına aktarır (CLOUD -> LOCAL).

Önemli Kurallar:
1. Yalnızca yetkilendirme alanları (status, approved_at, approved_by, notes) aktarılır.
2. Yerel operasyonel alanlar (first_seen_at, last_seen_at, created_at, plate_text, normalized_plate)
   kesinlikle bulut verisiyle ezilmez, yerel veri korunur.
3. AccessLog kayıtları geriye aktarılmaz.
4. Eşleşmeyen bulut araçları için yerelde otomatik kayıt oluşturulmaz (unmatched olarak sayılır).
5. Senkronizasyon idempottur. Değişiklik yoksa yerel veritabanına yazma yapılmaz.
"""

import sys
import time
import argparse
import uuid
from pathlib import Path
from typing import Tuple, Dict, Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

# src klasörünü import yoluna ekle
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from database import DEFAULT_DB_PATH, sanitize_db_url
from models import Base, Vehicle, VehicleStatus, utc_now
from cloud_sync import normalize_dt, normalize_str


def get_local_db_url() -> str:
    """
    Senkronizasyon için varsayılan yerel SQLite URL'sini döndürür.
    """
    return f"sqlite:///{DEFAULT_DB_PATH}"


def get_cloud_db_url() -> str:
    """
    CLOUD_DATABASE_URL çevre değişkeninden bulut veritabanı adresini okur.
    """
    import os
    return os.environ.get("CLOUD_DATABASE_URL", "").strip()


def vehicle_approval_has_changes(cv: Vehicle, lv: Vehicle) -> bool:
    """
    Bulut araç kaydı (cv) ile yerel araç kaydı (lv) arasındaki yetkilendirme alanlarını karşılaştırır.
    Sadece yetki alanlarında (status, approved_by, notes, approved_at) fark varsa True döner.
    """
    if normalize_str(cv.status) != normalize_str(lv.status):
        return True
    if normalize_str(cv.approved_by) != normalize_str(lv.approved_by):
        return True
    if normalize_str(cv.notes) != normalize_str(lv.notes):
        return True
    if normalize_dt(cv.approved_at) != normalize_dt(lv.approved_at):
        return True
    return False


def sync_approvals_from_cloud(
    cloud_session: Session,
    local_session: Session,
    dry_run: bool = False,
) -> Tuple[int, int, int]:
    """
    Buluttaki araç yetkilendirme kararlarını yerel veritabanına işler (CLOUD -> LOCAL).

    Döndürür:
        Tuple[int, int, int]: (guncellenen, degismeyen, eslesmeyen)
    """
    updated_count = 0
    unchanged_count = 0
    unmatched_count = 0

    cloud_vehicles = cloud_session.query(Vehicle).all()

    for cv in cloud_vehicles:
        # Önce sync_id ile, yoksa normalized_plate ile yerel araç kaydını bul
        lv = None
        if cv.sync_id:
            lv = local_session.query(Vehicle).filter_by(sync_id=cv.sync_id).first()
        if lv is None and cv.normalized_plate:
            lv = local_session.query(Vehicle).filter_by(normalized_plate=cv.normalized_plate).first()

        if lv is None:
            # Bulutta var ama yerel operasyonel veritabanında yok -> Eşleşmedi
            unmatched_count += 1
            continue

        if vehicle_approval_has_changes(cv, lv):
            if not dry_run:
                # Yalnızca yetkilendirme alanlarını güncelle
                lv.status = cv.status
                lv.approved_at = cv.approved_at
                lv.approved_by = cv.approved_by
                lv.notes = cv.notes
                lv.updated_at = utc_now()
                # sync_id yerelde eksikse eşleştir
                if cv.sync_id and not lv.sync_id:
                    lv.sync_id = cv.sync_id
            updated_count += 1
        else:
            unchanged_count += 1

    return updated_count, unchanged_count, unmatched_count


def run_approval_sync(
    local_url: str | None = None,
    cloud_url: str | None = None,
    dry_run: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Buluttan yerele yetkilendirme senkronizasyonunu (CLOUD -> LOCAL) yürütür.

    Döndürür:
        Tuple[bool, Dict[str, Any]]: (başarılı_mı, istatistikler_sözlüğü)
    """
    start_time = time.monotonic()
    stats: Dict[str, Any] = {
        "vehicles": {"updated": 0, "unchanged": 0, "unmatched": 0},
        "duration": 0.0,
    }

    resolved_local_url = local_url or get_local_db_url()
    resolved_cloud_url = cloud_url or get_cloud_db_url()

    if not resolved_cloud_url:
        print("[APPROVAL SYNC] CLOUD_DATABASE_URL tanımlanmamış veya boş.")
        print("[APPROVAL SYNC] Senkronizasyon yapılmadı. Yerel uygulama kesintisiz çalışmaya devam ediyor.")
        return True, stats

    try:
        # 1. Yerel veritabanı bağlantısı ve şema güncellemesi
        local_connect_args = {"check_same_thread": False} if resolved_local_url.startswith("sqlite") else {}
        local_engine = create_engine(resolved_local_url, connect_args=local_connect_args)
        Base.metadata.create_all(bind=local_engine)
        from database import _ensure_schema_upgrades
        _ensure_schema_upgrades(local_engine)
        LocalSession = sessionmaker(bind=local_engine)

        # 2. Bulut veritabanı bağlantısı
        cloud_connect_args = {"check_same_thread": False} if resolved_cloud_url.startswith("sqlite") else {}
        cloud_engine = create_engine(resolved_cloud_url, connect_args=cloud_connect_args)
        Base.metadata.create_all(bind=cloud_engine)
        _ensure_schema_upgrades(cloud_engine)
        CloudSession = sessionmaker(bind=cloud_engine)

        with LocalSession() as local_session:
            with CloudSession() as cloud_session:
                v_upd, v_unc, v_unm = sync_approvals_from_cloud(
                    cloud_session, local_session, dry_run=dry_run
                )
                if not dry_run and v_upd > 0:
                    local_session.commit()

                if dry_run:
                    local_session.rollback()

                duration = time.monotonic() - start_time
                stats = {
                    "vehicles": {"updated": v_upd, "unchanged": v_unc, "unmatched": v_unm},
                    "duration": round(duration, 2),
                }

                print("[APPROVAL SYNC]")
                print("Vehicles:")
                print(f"  updated: {v_upd}")
                print(f"  unchanged: {v_unc}")
                print(f"  unmatched: {v_unm}")
                print("")
                print(f"Duration: {duration:.2f} seconds")

                return True, stats

    except Exception as e:
        sanitized_cloud = sanitize_db_url(resolved_cloud_url)
        print(f"[APPROVAL SYNC HATA] Senkronizasyon hatası ({sanitized_cloud}): {e}")
        print("[APPROVAL SYNC HATA] Yerel veriler korundu. Yerel OCR ve bariyer sistemi çalışmaya devam ediyor.")
        duration = time.monotonic() - start_time
        stats["duration"] = round(duration, 2)
        return False, stats


def run_watch_mode(
    local_url: str | None = None,
    cloud_url: str | None = None,
    dry_run: bool = False,
    interval: int = 30,
    max_iterations: int | None = None,
) -> None:
    """
    Sürekli yetki senkronizasyonu (watch) modunu yürütür.
    """
    if interval <= 0:
        print("[APPROVAL SYNC HATA] --interval 0'dan büyük bir tamsayı olmalıdır.")
        raise ValueError("--interval 0'dan büyük bir tamsayı olmalıdır.")

    print(f"[APPROVAL SYNC] Worker başlatıldı (Interval: {interval}s, Dry-Run: {dry_run})")
    iterations = 0
    try:
        while True:
            run_approval_sync(local_url=local_url, cloud_url=cloud_url, dry_run=dry_run)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[APPROVAL SYNC] Worker durduruldu.")


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
        description="Plaka Tanima Sistemi - Cevrimdisi Oncelikli Buluttan Yerele Yetki Senkronizasyonu"
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
        "--cloud-url",
        type=str,
        default=None,
        help="Özel bulut veritabanı adresi (Varsayılan: CLOUD_DATABASE_URL)",
    )

    args = parser.parse_args()

    if args.interval <= 0:
        print("[APPROVAL SYNC HATA] --interval 0'dan büyük bir tamsayı olmalıdır.")
        sys.exit(1)

    if args.watch:
        run_watch_mode(
            local_url=args.local_url,
            cloud_url=args.cloud_url,
            dry_run=args.dry_run,
            interval=args.interval,
        )
    else:
        success, _ = run_approval_sync(
            local_url=args.local_url,
            cloud_url=args.cloud_url,
            dry_run=args.dry_run,
        )

        if not success:
            sys.exit(1)


if __name__ == "__main__":
    main()
