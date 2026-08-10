"""
cloud_sync.py — Çevrimdışı Öncelikli Tek Yönlü HTTPS Senkronizasyon Modülü

Bu modül, yerel SQLite veritabanındaki Araç (Vehicle) ve Erişim Kaydı (AccessLog)
verilerini Cloud Run üzerinde çalışan FastAPI HTTPS API servisine (/api/sync/push)
tek yönlü (LOCAL -> CLOUD) olarak aktarır.

Önemli İlkeler:
1. Yerel SQLite ana çalışma ortamıdır; kamera, OCR ve bariyer kararları bulut
   bağlantısından bağımsız çalışmaya devam eder.
2. Senkronizasyon hatası veya internet kesintisi hiçbir şekilde OCR veya bariyer sistemini durdurmaz.
3. CLOUD_SYNC_API_URL veya SYNC_API_TOKEN tanımlanmamışsa senkronizasyon güvenle atlanır.
4. Doğrudan veritabanı portları (3307 / 5433) kullanılmaz; standart HTTPS (443) kullanılır.
5. Senkronizasyon idempottur (tekrarlanabilir); aynı veriler tekrar aktarıldığında mükerrer kayıt oluşmaz.
6. Veriler birincil anahtar (ID) yerine benzersiz UUID (sync_id) üzerinden eşleştirilir.
"""

import sys
import os
import time
import argparse
import uuid
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
from models import Base, Vehicle, AccessLog


def get_local_db_url() -> str:
    """Senkronizasyon için varsayılan yerel SQLite URL'sini döndürür."""
    return f"sqlite:///{DEFAULT_DB_PATH}"


def get_cloud_sync_api_url() -> str:
    """CLOUD_SYNC_API_URL çevre değişkeninden bulut HTTPS API adresini okur."""
    return os.environ.get("CLOUD_SYNC_API_URL", "").strip()


def get_sync_api_token() -> str:
    """SYNC_API_TOKEN çevre değişkeninden senkronizasyon yetki token'ını okur."""
    return os.environ.get("SYNC_API_TOKEN", "").strip()


def ensure_local_sync_ids(local_session: Session) -> Tuple[int, int]:
    """
    Mevcut yerel kayıtlarda sync_id (UUID) bulunmayan satırlara yeni UUID atar.
    Bu işlem geriye dönük uyumluluk sağlar ve mevcut verileri bozmaz.
    """
    updated_vehicles = 0
    updated_logs = 0

    vehicles_without_sync_id = local_session.query(Vehicle).filter(Vehicle.sync_id.is_(None)).all()
    for v in vehicles_without_sync_id:
        v.sync_id = str(uuid.uuid4())
        updated_vehicles += 1

    logs_without_sync_id = local_session.query(AccessLog).filter(AccessLog.sync_id.is_(None)).all()
    for log in logs_without_sync_id:
        log.sync_id = str(uuid.uuid4())
        updated_logs += 1

    if updated_vehicles > 0 or updated_logs > 0:
        local_session.commit()

    return updated_vehicles, updated_logs


def normalize_dt(dt: Any) -> float | None:
    """
    Tarih/saat değerlerini karşılaştırma için saniye duyarlılığında UTC Unix timestamp'ine dönüştürür.
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
    """
    if val is None:
        return None
    if hasattr(val, "value"):
        val = val.value
    s = str(val).strip()
    return s if s else None


def send_https_push(
    sync_api_url: str,
    sync_token: str,
    payload: dict,
    timeout: float = 15.0,
) -> Tuple[bool, dict]:
    """
    Python standart kütüphanesi (urllib.request) ile HTTPS POST isteği gönderir.
    Token hiçbir şekilde loglanmaz veya ekrana yazdırılmaz.
    """
    url = sync_api_url.rstrip("/") + "/api/sync/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {sync_token}",
        "User-Agent": "Plaka-Local-Sync/1.0",
    }

    try:
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                res_body = response.read().decode("utf-8")
                return True, json.loads(res_body)
            else:
                print(f"[CLOUD SYNC HATA] Sunucu yanıtı: HTTP {response.status}")
                return False, {}
    except urllib.error.HTTPError as e:
        print(f"[CLOUD SYNC HATA] HTTP Hatası: {e.code}")
        return False, {}
    except urllib.error.URLError as e:
        print(f"[CLOUD SYNC HATA] Ağ Bağlantı Hatası: {e.reason}")
        return False, {}
    except Exception as e:
        print(f"[CLOUD SYNC HATA] Senkronizasyon isteği başarısız: {e}")
        return False, {}


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


def run_sync(
    local_url: str | None = None,
    sync_api_url: str | None = None,
    sync_token: str | None = None,
    cloud_url: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Tek yönlü (LOCAL -> CLOUD HTTPS API) senkronizasyon işlemini yürütür.

    Döndürür:
        Tuple[bool, Dict[str, Any]]: (başarılı_mı, istatistikler_sözlüğü)
    """
    start_time = time.monotonic()
    stats: Dict[str, Any] = {
        "vehicles": {"new": 0, "updated": 0, "unchanged": 0},
        "access_logs": {"new": 0, "updated": 0, "unchanged": 0},
        "duration": 0.0,
    }

    resolved_local_url = local_url if local_url is not None else get_local_db_url()
    resolved_api_url, resolved_token = get_sync_api_config(sync_api_url, sync_token)

    if not resolved_api_url or not resolved_token:
        print("[CLOUD SYNC] CLOUD_SYNC_API_URL veya SYNC_API_TOKEN tanımlanmamış.")
        print("[CLOUD SYNC] Senkronizasyon atlandı. Yerel uygulama kesintisiz çalışmaya devam ediyor.")
        return True, stats

    try:
        local_connect_args = {"check_same_thread": False} if resolved_local_url.startswith("sqlite") else {}
        local_engine = create_engine(resolved_local_url, connect_args=local_connect_args)
        Base.metadata.create_all(bind=local_engine)
        from database import _ensure_schema_upgrades
        _ensure_schema_upgrades(local_engine)
        LocalSession = sessionmaker(bind=local_engine)

        with LocalSession() as local_session:
            up_v, up_l = ensure_local_sync_ids(local_session)
            if verbose and (up_v > 0 or up_l > 0):
                print(f"[LOCAL RECOVERY] {up_v} araç ve {up_l} erişim loguna yeni UUID atandı.")

            local_vehicles = local_session.query(Vehicle).all()
            local_logs = local_session.query(AccessLog).all()

            vehicles_payload = []
            for v in local_vehicles:
                vehicles_payload.append({
                    "sync_id": v.sync_id,
                    "plate_text": v.plate_text,
                    "normalized_plate": v.normalized_plate,
                    "status": v.status.value if hasattr(v.status, "value") else str(v.status),
                    "approved_at": v.approved_at.isoformat() if v.approved_at else None,
                    "approved_by": v.approved_by,
                    "first_seen_at": v.first_seen_at.isoformat() if v.first_seen_at else None,
                    "last_seen_at": v.last_seen_at.isoformat() if v.last_seen_at else None,
                    "notes": v.notes,
                })

            v_id_to_sync_id = {v.id: v.sync_id for v in local_vehicles if v.id and v.sync_id}

            logs_payload = []
            for log in local_logs:
                v_sync_id = v_id_to_sync_id.get(log.vehicle_id)
                logs_payload.append({
                    "sync_id": log.sync_id,
                    "vehicle_sync_id": v_sync_id,
                    "plate_text": log.plate_text,
                    "normalized_plate": log.normalized_plate,
                    "direction": log.direction.value if hasattr(log.direction, "value") else str(log.direction),
                    "decision": log.decision.value if hasattr(log.decision, "value") else str(log.decision),
                    "ocr_confidence": log.ocr_confidence,
                    "source_camera": log.source_camera,
                    "detected_at": log.detected_at.isoformat() if log.detected_at else None,
                })

            payload = {
                "vehicles": vehicles_payload,
                "access_logs": logs_payload,
            }

            if dry_run:
                duration = time.monotonic() - start_time
                stats = {
                    "vehicles": {"new": len(vehicles_payload), "updated": 0, "unchanged": 0},
                    "access_logs": {"new": len(logs_payload), "updated": 0, "unchanged": 0},
                    "duration": round(duration, 2),
                }
                print("[CLOUD SYNC] DRY RUN — Sunucuya veri gönderilmedi.")
                print(f"Hazırlanan araç: {len(vehicles_payload)}, erişim logu: {len(logs_payload)}")
                return True, stats

            success, api_res = send_https_push(resolved_api_url, resolved_token, payload)
            duration = time.monotonic() - start_time

            if success and api_res:
                v_stats = api_res.get("vehicles", {})
                l_stats = api_res.get("access_logs", {})
                v_new = v_stats.get("new", 0)
                v_upd = v_stats.get("updated", 0)
                v_unc = v_stats.get("unchanged", 0)
                l_new = l_stats.get("new", 0)
                l_upd = l_stats.get("updated", 0)
                l_unc = l_stats.get("unchanged", 0)

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
            else:
                print("[CLOUD SYNC HATA] Yerel veriler korundu. Yerel OCR sistemi çalışmaya devam ediyor.")
                stats["duration"] = round(duration, 2)
                return False, stats

    except Exception as e:
        print(f"[CLOUD SYNC HATA] Senkronizasyon hatası: {e}")
        print("[CLOUD SYNC HATA] Yerel veriler korundu. Yerel OCR sistemi çalışmaya devam ediyor.")
        duration = time.monotonic() - start_time
        stats["duration"] = round(duration, 2)
        return False, stats


def run_watch_mode(
    local_url: str | None = None,
    sync_api_url: str | None = None,
    sync_token: str | None = None,
    cloud_url: str | None = None,
    dry_run: bool = False,
    interval: int = 60,
    max_iterations: int | None = None,
) -> None:
    """Sürekli senkronizasyon (watch) modunu yürütür."""
    if interval <= 0:
        print("[CLOUD SYNC HATA] --interval 0'dan büyük bir tamsayı olmalıdır.")
        raise ValueError("--interval 0'dan büyük bir tamsayı olmalıdır.")

    print(f"[CLOUD SYNC] Worker başlatıldı (Interval: {interval}s, Dry-Run: {dry_run})")
    iterations = 0
    try:
        while True:
            run_sync(
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
        print("\n[CLOUD SYNC] Worker durduruldu.")


def main() -> None:
    """CLI giriş noktası."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="Plaka Tanıma Sistemi - HTTPS API Bulut Senkronizasyonu"
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
        help="Sunucuya veri göndermeden hazırlanacak paket bilgisini incele",
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
        print("[CLOUD SYNC HATA] --interval 0'dan büyük bir tamsayı olmalıdır.")
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
        success, _ = run_sync(
            local_url=args.local_url,
            sync_api_url=args.sync_api_url,
            sync_token=args.sync_token,
            dry_run=args.dry_run,
        )
        if not success:
            sys.exit(1)


if __name__ == "__main__":
    main()
