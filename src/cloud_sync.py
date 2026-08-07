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
import argparse
import uuid
from pathlib import Path
from typing import Tuple

from sqlalchemy import create_engine, select
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


def sync_vehicles(
    local_session: Session,
    cloud_session: Session,
    dry_run: bool = False,
) -> Tuple[int, int]:
    """
    Vehicle kayıtlarını yerelden buluta aktarır/günceller.

    Döndürür:
        Tuple[int, int]: (eklenen_arac_sayisi, guncellenen_arac_sayisi)
    """
    created_count = 0
    updated_count = 0

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
            # Bulutta var: Mevcut kaydı güncelle
            if not dry_run:
                if lv.sync_id and not cv.sync_id:
                    cv.sync_id = lv.sync_id
                cv.plate_text = lv.plate_text
                cv.status = lv.status
                cv.last_seen_at = lv.last_seen_at
                cv.updated_at = lv.updated_at
                cv.approved_at = lv.approved_at
                cv.approved_by = lv.approved_by
                cv.notes = lv.notes
            updated_count += 1

    return created_count, updated_count


def sync_access_logs(
    local_session: Session,
    cloud_session: Session,
    dry_run: bool = False,
) -> Tuple[int, int]:
    """
    AccessLog kayıtlarını yerelden buluta aktarır.

    Döndürür:
        Tuple[int, int]: (eklenen_log_sayisi, atlanan_log_sayisi)
    """
    created_count = 0
    skipped_count = 0

    local_logs = local_session.query(AccessLog).all()

    for ll in local_logs:
        # sync_id ile bulut veritabanında ara
        cl = None
        if ll.sync_id:
            cl = cloud_session.query(AccessLog).filter_by(sync_id=ll.sync_id).first()

        if cl is not None:
            skipped_count += 1
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

    return created_count, skipped_count


def run_sync(
    local_url: str | None = None,
    cloud_url: str | None = None,
    dry_run: bool = False,
) -> bool:
    """
    Tek yönlü (LOCAL -> CLOUD) senkronizasyon işlemini yürütür.

    Parametreler:
        local_url (str | None): Yerel SQLite veritabanı adresi (Varsayılan: data/plate_system.db)
        cloud_url (str | None): Bulut PostgreSQL veritabanı adresi (CLOUD_DATABASE_URL)
        dry_run (bool): True ise veri tabanına yazma yapılmaz, simülasyon çıktısı basılır.

    Döndürür:
        bool: Senkronizasyon başarılı ise True, hata durumunda False.
    """
    resolved_local_url = local_url or get_local_db_url()
    resolved_cloud_url = cloud_url or get_cloud_db_url()

    if not resolved_cloud_url:
        print("[CLOUD SYNC] CLOUD_DATABASE_URL tanımlanmamış veya boş.")
        print("[CLOUD SYNC] Senkronizasyon yapılmadı. Yerel uygulama kesintisiz çalışmaya devam ediyor.")
        return True

    sanitized_local = sanitize_db_url(resolved_local_url)
    sanitized_cloud = sanitize_db_url(resolved_cloud_url)

    print("=" * 60)
    print("  PLAKA TANIMA SİSTEMİ — TEK YÖNLÜ BULUT SENKRONİZASYONU")
    print("=" * 60)
    print(f"  Kaynak (Local)  : {sanitized_local}")
    print(f"  Hedef (Cloud)   : {sanitized_cloud}")
    print(f"  Mod             : {'[DRY RUN - Yalnızca İnceleme]' if dry_run else '[GERÇEK SENKRONİZASYON]'}")
    print("-" * 60)

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
            if up_v > 0 or up_l > 0:
                print(f"[LOCAL RECOVERY] {up_v} araç ve {up_l} erişim loguna yeni UUID atandı.")

            # Bulut tablolarının varlığından emin ol (varsa değiştirmemesi için Base.metadata.create_all)
            Base.metadata.create_all(bind=cloud_engine)

            with CloudSession() as cloud_session:
                # 4. Araçları senkronize et
                v_created, v_updated = sync_vehicles(local_session, cloud_session, dry_run=dry_run)
                if not dry_run and (v_created > 0 or v_updated > 0):
                    cloud_session.commit()

                # 5. Erişim kayıtlarını senkronize et
                l_created, l_skipped = sync_access_logs(local_session, cloud_session, dry_run=dry_run)
                if not dry_run and l_created > 0:
                    cloud_session.commit()

                if dry_run:
                    cloud_session.rollback()

                print("-" * 60)
                print(f"  Araç Kayıtları  : {v_created} yeni eklenecek/eklendi, {v_updated} güncellendi.")
                print(f"  Erişim Logları  : {l_created} yeni eklenecek/eklendi, {l_skipped} zaten mevcut.")
                print("-" * 60)
                print("✓ Senkronizasyon başarıyla tamamlandı.")
                return True

    except Exception as e:
        print(f"[CLOUD SYNC HATA] Senkronizasyon sırasında hata oluştu: {e}")
        print("[CLOUD SYNC HATA] Yerel veriler korundu. Yerel OCR sistemi çalışmaya devam ediyor.")
        return False


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
        "--dry-run",
        action="store_true",
        help="Bulut veri tabanina yazmadan senkronize edilecek verileri incele",
    )
    parser.add_argument(
        "--local-url",
        type=str,
        default=None,
        help="Ozel yerel veri tabani adresi (Varsayilan: data/plate_system.db)",
    )
    parser.add_argument(
        "--cloud-url",
        type=str,
        default=None,
        help="Ozel bulut veri tabani adresi (Varsayilan: CLOUD_DATABASE_URL)",
    )

    args = parser.parse_args()

    success = run_sync(
        local_url=args.local_url,
        cloud_url=args.cloud_url,
        dry_run=args.dry_run,
    )

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
