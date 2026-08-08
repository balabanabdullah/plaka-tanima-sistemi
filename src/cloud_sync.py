"""
cloud_sync.py — Çevrimdışı Öncelikli Tek Yönlü Veritabanı Senkronizasyon Modülü

Bu modül, yerel SQLite veritabanındaki Araç (Vehicle) ve Erişim Kaydı (AccessLog)
verilerini bulut PostgreSQL (Cloud SQL) veritabanına tek yönlü (LOCAL -> CLOUD)
olarak aktarır.

Önemli İlkeler:
1. Yerel SQLite ana çalışma ortamıdır; kamera, OCR ve bariyer kararları bulut
   bağlantısından bağımsız çalışmaya devam eder.
2. Senkronizasyon hatası veya internet kesintisi hiçbir şekilde OCR veya bariyer sistemini durdurmaz.
3. CLOUD_DATABASE_URL çevre değişkeni tanımlanmamışsa senkronizasyon güvenle sonlanır.
4. Senkronizasyon idempottur (tekrarlanabilir); aynı veriler tekrar aktarıldığında mükerrer kayıt oluşmaz.
5. Veriler birincil anahtar (ID) yerine benzersiz UUID (sync_id) üzerinden eşleştirilir.
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
from models import Base, Vehicle, AccessLog


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


def ensure_local_sync_ids(local_session: Session) -> Tuple[int, int]:
    """
    Mevcut yerel kayıtlarda sync_id (UUID) bulunmayan satırlara yeni UUID atar.
    Bu işlem geriye dönük uyumluluk sağlar ve mevcut verileri bozmaz.

    Döndürür:
        Tuple[int, int]: (guncellenen_arac_sayisi, guncellenen_log_sayisi)
    """
    updated_vehicles = 0
    updated_logs = 0

    # sync_id boş olan araçlar
    vehicles_without_sync_id = local_session.query(Vehicle).filter(Vehicle.sync_id.is_(None)).all()
    for v in vehicles_without_sync_id:
        v.sync_id = str(uuid.uuid4())
        updated_vehicles += 1

    # sync_id boş olan erişim logları
    logs_without_sync_id = local_session.query(AccessLog).filter(AccessLog.sync_id.is_(None)).all()
    for log in logs_without_sync_id:
        log.sync_id = str(uuid.uuid4())
        updated_logs += 1

    if updated_vehicles > 0 or updated_logs > 0:
        local_session.commit()

    return updated_vehicles, updated_logs


from datetime import datetime, timezone


def normalize_dt(dt: Any) -> float | None:
    """
    Tarih/saat değerlerini karşılaştırma için saniye duyarlılığında UTC Unix timestamp'ine dönüştürür.
    Timezone-aware ve timezone-naive uyumsuzluklarını ve mikro-saniye farklarını giderir.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:
            return None
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return round(dt.timestamp(), 0)
    return None


def normalize_str(val: Any) -> str | None:
    """
    Metin ve Enum değerlerini karşılaştırma için standart dize biçimine dönüştürür.
    None ile boş dize farklarını ve Enum nesnesi dize farklarını giderir.
    """
    if val is None:
        return None
    if hasattr(val, "value"):
        val = val.value
    s = str(val).strip()
    return s if s else None


def vehicle_has_changes(lv: Vehicle, cv: Vehicle) -> bool:
    """
    Yerel (lv) ve Bulut (cv) araç kayıtları arasındaki gerçek veri değişikliklerini
    tür ve saat dilimi bağımsız olarak karşılaştırır.
    """
    if lv.sync_id and normalize_str(lv.sync_id) != normalize_str(cv.sync_id):
        return True
    if normalize_str(lv.plate_text) != normalize_str(cv.plate_text):
        return True
    if normalize_str(lv.normalized_plate) != normalize_str(cv.normalized_plate):
        return True
    if normalize_str(lv.status) != normalize_str(cv.status):
        return True
    if normalize_str(lv.approved_by) != normalize_str(cv.approved_by):
        return True
    if normalize_str(lv.notes) != normalize_str(cv.notes):
        return True

    if normalize_dt(lv.first_seen_at) != normalize_dt(cv.first_seen_at):
        return True
    if normalize_dt(lv.last_seen_at) != normalize_dt(cv.last_seen_at):
        return True
    if normalize_dt(lv.created_at) != normalize_dt(cv.created_at):
        return True
    if normalize_dt(lv.updated_at) != normalize_dt(cv.updated_at):
        return True
    if normalize_dt(lv.approved_at) != normalize_dt(cv.approved_at):
        return True

    return False


def sync_vehicles(
    local_session: Session,
    cloud_session: Session,
    dry_run: bool = False,
) -> Tuple[int, int, int]:
    """
    Vehicle kayıtlarını yerelden buluta aktarır/günceller.

    Döndürür:
        Tuple[int, int, int]: (yeni_eklenen, guncellenen, degismeyen)
    """
    created_count = 0
    updated_count = 0
    unchanged_count = 0

    local_vehicles = local_session.query(Vehicle).all()

    for lv in local_vehicles:
        # Önce sync_id ile, yoksa normalized_plate ile bulut veritabanında ara
        cv = None
        if lv.sync_id:
            cv = cloud_session.query(Vehicle).filter_by(sync_id=lv.sync_id).first()
        if cv is None:
            cv = cloud_session.query(Vehicle).filter_by(normalized_plate=lv.normalized_plate).first()

        if cv is None:
            # Bulutta yok: Yeni kayıt oluştur
            new_cv = Vehicle(
                sync_id=lv.sync_id or str(uuid.uuid4()),
                plate_text=lv.plate_text,
                normalized_plate=lv.normalized_plate,
                status=lv.status,
                first_seen_at=lv.first_seen_at,
                last_seen_at=lv.last_seen_at,
                created_at=lv.created_at,
                updated_at=lv.updated_at,
                approved_at=lv.approved_at,
                approved_by=lv.approved_by,
                notes=lv.notes,
            )
            if not dry_run:
                cloud_session.add(new_cv)
            created_count += 1
        else:
            # Bulutta var: Güncelleme gerekiyor mu kontrol et
            if vehicle_has_changes(lv, cv):
                if not dry_run:
                    if lv.sync_id and not cv.sync_id:
                        cv.sync_id = lv.sync_id
                    cv.plate_text = lv.plate_text
                    cv.normalized_plate = lv.normalized_plate
                    cv.status = lv.status
                    cv.first_seen_at = lv.first_seen_at
                    cv.last_seen_at = lv.last_seen_at
                    cv.created_at = lv.created_at
                    cv.updated_at = lv.updated_at
                    cv.approved_at = lv.approved_at
                    cv.approved_by = lv.approved_by
                    cv.notes = lv.notes
                updated_count += 1
            else:
                unchanged_count += 1

    return created_count, updated_count, unchanged_count


def sync_access_logs(
    local_session: Session,
    cloud_session: Session,
    dry_run: bool = False,
) -> Tuple[int, int, int]:
    """
    AccessLog kayıtlarını yerelden buluta aktarır.

    Döndürür:
        Tuple[int, int, int]: (yeni_eklenen, guncellenen, degismeyen)
    """
    created_count = 0
    updated_count = 0
    unchanged_count = 0

    local_logs = local_session.query(AccessLog).all()

    for ll in local_logs:
        # sync_id ile bulut veritabanında ara
        cl = None
        if ll.sync_id:
            cl = cloud_session.query(AccessLog).filter_by(sync_id=ll.sync_id).first()

        if cl is not None:
            unchanged_count += 1
            continue

        # Buluttaki karşılık gelen araç kaydını bul (vehicle_id ilişkilendirmesi için)
        cloud_vehicle_id = None
        if ll.vehicle is not None:
            cv = None
            if ll.vehicle.sync_id:
                cv = cloud_session.query(Vehicle).filter_by(sync_id=ll.vehicle.sync_id).first()
            if cv is None:
                cv = cloud_session.query(Vehicle).filter_by(normalized_plate=ll.vehicle.normalized_plate).first()
            if cv is not None:
                cloud_vehicle_id = cv.id

        new_cl = AccessLog(
            sync_id=ll.sync_id or str(uuid.uuid4()),
            vehicle_id=cloud_vehicle_id,
            plate_text=ll.plate_text,
            normalized_plate=ll.normalized_plate,
            direction=ll.direction,
            decision=ll.decision,
            ocr_confidence=ll.ocr_confidence,
            detected_at=ll.detected_at,
            image_path=ll.image_path,
            source_camera=ll.source_camera,
            denial_reason=ll.denial_reason,
        )

        if not dry_run:
            cloud_session.add(new_cl)
        created_count += 1

    return created_count, updated_count, unchanged_count


def run_sync(
    local_url: str | None = None,
    cloud_url: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Tek yönlü (LOCAL -> CLOUD) senkronizasyon işlemini yürütür.

    Döndürür:
        Tuple[bool, Dict[str, Any]]: (başarılı_mı, istatistikler_sözlüğü)
    """
    start_time = time.monotonic()
    stats: Dict[str, Any] = {
        "vehicles": {"new": 0, "updated": 0, "unchanged": 0},
        "access_logs": {"new": 0, "updated": 0, "unchanged": 0},
        "duration": 0.0,
    }

    resolved_local_url = local_url or get_local_db_url()
    resolved_cloud_url = cloud_url or get_cloud_db_url()

    if not resolved_cloud_url:
        print("[CLOUD SYNC] CLOUD_DATABASE_URL tanımlanmamış veya boş.")
        print("[CLOUD SYNC] Senkronizasyon yapılmadı. Yerel uygulama kesintisiz çalışmaya devam ediyor.")
        return True, stats

    try:
        # 1. Yerel veritabanı bağlantısı ve şema güncellemesi
        local_connect_args = {"check_same_thread": False} if resolved_local_url.startswith("sqlite") else {}
        local_engine = create_engine(resolved_local_url, connect_args=local_connect_args)
        Base.metadata.create_all(bind=local_engine)
        from database import _ensure_schema_upgrades
        _ensure_schema_upgrades(local_engine)
        LocalSession = sessionmaker(bind=local_engine)

        # 2. Bulut veritabanı bağlantısı ve şema güncellemesi
        cloud_connect_args = {"check_same_thread": False} if resolved_cloud_url.startswith("sqlite") else {}
        cloud_engine = create_engine(resolved_cloud_url, connect_args=cloud_connect_args)
        Base.metadata.create_all(bind=cloud_engine)
        _ensure_schema_upgrades(cloud_engine)
        CloudSession = sessionmaker(bind=cloud_engine)

        with LocalSession() as local_session:
            # 3. Yerel tablolara gerekiyorsa eksik UUID'leri yaz
            up_v, up_l = ensure_local_sync_ids(local_session)
            if verbose and (up_v > 0 or up_l > 0):
                print(f"[LOCAL RECOVERY] {up_v} araç ve {up_l} erişim loguna yeni UUID atandı.")

            with CloudSession() as cloud_session:
                # 4. Araçları senkronize et
                v_new, v_upd, v_unc = sync_vehicles(local_session, cloud_session, dry_run=dry_run)
                if not dry_run and (v_new > 0 or v_upd > 0):
                    cloud_session.commit()

                # 5. Erişim kayıtlarını senkronize et
                l_new, l_upd, l_unc = sync_access_logs(local_session, cloud_session, dry_run=dry_run)
                if not dry_run and l_new > 0:
                    cloud_session.commit()

                if dry_run:
                    cloud_session.rollback()

                duration = time.monotonic() - start_time
                stats = {
                    "vehicles": {"new": v_new, "updated": v_upd, "unchanged": v_unc},
                    "access_logs": {"new": l_new, "updated": l_upd, "unchanged": l_unc},
                    "duration": round(duration, 2),
                }

                print("[CLOUD SYNC]")
                print("Vehicles:")
                print(f"  new: {v_new}")
                print(f"  updated: {v_upd}")
                print(f"  unchanged: {v_unc}")
                print("")
                print("Access logs:")
                print(f"  new: {l_new}")
                print(f"  updated: {l_upd}")
                print(f"  unchanged: {l_unc}")
                print("")
                print(f"Duration: {duration:.2f} seconds")

                return True, stats

    except Exception as e:
        sanitized_cloud = sanitize_db_url(resolved_cloud_url)
        print(f"[CLOUD SYNC HATA] Senkronizasyon hatası ({sanitized_cloud}): {e}")
        print("[CLOUD SYNC HATA] Yerel veriler korundu. Yerel OCR sistemi çalışmaya devam ediyor.")
        duration = time.monotonic() - start_time
        stats["duration"] = round(duration, 2)
        return False, stats


def run_watch_mode(
    local_url: str | None = None,
    cloud_url: str | None = None,
    dry_run: bool = False,
    interval: int = 60,
    max_iterations: int | None = None,
) -> None:
    """
    Sürekli senkronizasyon (watch) modunu yürütür.

    Parametreler:
        interval (int): Döngüler arası bekleme süresi (saniye)
        max_iterations (int | None): Testler için maksimum döngü sayısı (None = sonsuz)
    """
    if interval <= 0:
        print("[CLOUD SYNC HATA] --interval 0'dan büyük bir tamsayı olmalıdır.")
        raise ValueError("--interval 0'dan büyük bir tamsayı olmalıdır.")

    print(f"[CLOUD SYNC] Worker başlatıldı (Interval: {interval}s, Dry-Run: {dry_run})")
    iterations = 0
    try:
        while True:
            run_sync(local_url=local_url, cloud_url=cloud_url, dry_run=dry_run)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[CLOUD SYNC] Worker durduruldu.")


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
        description="Plaka Tanima Sistemi - Cevrimdisi Oncelikli Tek Yonlu Bulut Senkronizasyonu"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Sürekli senkronizasyon (worker) modunu başlat",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Sürekli modda senkronizasyon aralığı (saniye, varsayılan: 60)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Bulut veritabanına yazmadan senkronize edilecek verileri incele",
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
        print("[CLOUD SYNC HATA] --interval 0'dan büyük bir tamsayı olmalıdır.")
        sys.exit(1)

    if args.watch:
        run_watch_mode(
            local_url=args.local_url,
            cloud_url=args.cloud_url,
            dry_run=args.dry_run,
            interval=args.interval,
        )
    else:
        success, _ = run_sync(
            local_url=args.local_url,
            cloud_url=args.cloud_url,
            dry_run=args.dry_run,
        )

        if not success:
            sys.exit(1)


if __name__ == "__main__":
    main()
