"""
web_app.py — Plaka Tanıma Sistemi Güvenlik Paneli

FastAPI + Jinja2 tabanlı yerel web paneli.
Güvenlik görevlisi pending plakaları görür, onaylar veya reddeder.

Çalıştırma:
    uvicorn web_app:app --app-dir src --host 127.0.0.1 --port 8000
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from database import init_db, get_session
from models import AccessLog, Vehicle, VehicleStatus
from plate_service import approve_vehicle, reject_vehicle, delete_vehicle

# ─────────────────────────────────────────────
# Proje kök dizini
# Path(__file__) -> src/web_app.py
# .parent       -> src/
# .parent       -> proje kökü
# ─────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Geçerli status değerleri (filtre doğrulaması için)
GECERLI_STATUSLAR = {"pending", "approved", "rejected", "inactive"}


# ─────────────────────────────────────────────
# Zaman Yardımcısı
# ─────────────────────────────────────────────

def yerel_zaman(dt: datetime | None) -> str:
    """
    UTC datetime değerini sistemin yerel saatine çevirir.
    Jinja2 şablonlarında doğrudan kullanılır.

    Parametreler:
        dt: Tarih/saat değeri (None olabilir)

    Döndürür:
        str: "dd.mm.yyyy ss:dd:sn" formatında yerel saat veya "-"
    """
    if dt is None:
        return "-"
    if not isinstance(dt, datetime):
        return str(dt)
    # SQLite timezone-naive datetime dönerse UTC kabul et
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%d.%m.%Y %H:%M:%S")


# ─────────────────────────────────────────────
# Nesne → Dict Dönüştürücüler
# SQLAlchemy session kapatıldıktan sonra nesnelere
# erişim sorun yaratabilir; dönüştürme session içinde yapılır.
# ─────────────────────────────────────────────

def vehicle_to_dict(v: Vehicle) -> dict:
    """Vehicle modelini şablon için sözlüğe dönüştürür."""
    return {
        "id":               v.id,
        "plate_text":       v.plate_text,
        "normalized_plate": v.normalized_plate,
        "status":           v.status.value,
        "first_seen_at":    v.first_seen_at,
        "last_seen_at":     v.last_seen_at,
        "approved_at":      v.approved_at,
        "approved_by":      v.approved_by,
        "notes":            v.notes,
        "created_at":       v.created_at,
        "updated_at":       v.updated_at,
    }


def log_to_dict(log: AccessLog) -> dict:
    """AccessLog modelini şablon için sözlüğe dönüştürür."""
    return {
        "id":               log.id,
        "vehicle_id":       log.vehicle_id,
        "plate_text":       log.plate_text,
        "normalized_plate": log.normalized_plate,
        "direction":        log.direction.value,
        "decision":         log.decision.value,
        "ocr_confidence":   log.ocr_confidence,
        "detected_at":      log.detected_at,
        "source_camera":    log.source_camera or "-",
        "denial_reason":    log.denial_reason or "-",
    }


# ─────────────────────────────────────────────
# Lifespan: uygulama başlarken init_db() çağrılır
# ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Uygulama başladığında veritabanını hazırlar."""
    try:
        init_db()
        print("Veritabanı başarıyla başlatıldı.")
    except Exception as e:
        print(f"HATA: Veritabanı başlatılamadı: {e}")
        raise
    yield


# ─────────────────────────────────────────────
# FastAPI Uygulaması
# ─────────────────────────────────────────────

app = FastAPI(
    title="Plaka Tanıma Güvenlik Paneli",
    description="Yerel plaka yetkilendirme ve takip sistemi.",
    lifespan=lifespan,
)

# Statik dosyalar (CSS)
app.mount(
    "/static",
    StaticFiles(directory=str(PROJECT_ROOT / "static")),
    name="static",
)

# Jinja2 şablon motoru
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))

# yerel_zaman fonksiyonunu tüm şablonlara global olarak ekle
templates.env.globals["yerel_zaman"] = yerel_zaman


# ─────────────────────────────────────────────
# GET /   — Dashboard
# ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """
    Ana sayfa: durum sayaçları, pending araçlar ve son erişim kayıtları.
    """
    with get_session() as session:
        # Durum sayaçları
        stats = {
            "pending":  session.query(Vehicle).filter_by(status=VehicleStatus.pending).count(),
            "approved": session.query(Vehicle).filter_by(status=VehicleStatus.approved).count(),
            "rejected": session.query(Vehicle).filter_by(status=VehicleStatus.rejected).count(),
            "inactive": session.query(Vehicle).filter_by(status=VehicleStatus.inactive).count(),
        }

        # İlk 5 pending araç (en eski önce)
        pending_vehicles = [
            vehicle_to_dict(v)
            for v in session.query(Vehicle)
            .filter_by(status=VehicleStatus.pending)
            .order_by(Vehicle.first_seen_at.asc())
            .limit(5)
            .all()
        ]

        # Son 10 erişim kaydı
        recent_logs = [
            log_to_dict(log)
            for log in session.query(AccessLog)
            .order_by(AccessLog.detected_at.desc())
            .limit(10)
            .all()
        ]

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "stats":            stats,
            "pending_vehicles": pending_vehicles,
            "recent_logs":      recent_logs,
        },
    )


# ─────────────────────────────────────────────
# GET /pending   — Pending araçlar (approve/reject butonlu)
# ─────────────────────────────────────────────

@app.get("/pending", response_class=HTMLResponse)
async def pending_list(request: Request):
    """
    Onay bekleyen tüm araçları Approve / Reject butonlarıyla listeler.
    """
    with get_session() as session:
        vehicles = [
            vehicle_to_dict(v)
            for v in session.query(Vehicle)
            .filter_by(status=VehicleStatus.pending)
            .order_by(Vehicle.first_seen_at.asc())
            .all()
        ]

    return templates.TemplateResponse(
        request=request,
        name="vehicles.html",
        context={
            "vehicles":       vehicles,
            "current_status": "pending",
            "pending_mode":   True,
            "title":          "Onay Bekleyen Araçlar",
        },
    )


# ─────────────────────────────────────────────
# GET /vehicles   — Tüm araçlar (filtreli)
# ─────────────────────────────────────────────

@app.get("/vehicles", response_class=HTMLResponse)
async def vehicles_list(request: Request, status: str | None = None):
    """
    Tüm araçları listeler. Opsiyonel ?status=pending/approved/rejected/inactive filtresi.
    """
    if status is not None and status not in GECERLI_STATUSLAR:
        raise HTTPException(
            status_code=400,
            detail=f"Geçersiz status: '{status}'. Geçerli değerler: {', '.join(sorted(GECERLI_STATUSLAR))}",
        )

    with get_session() as session:
        sorgu = session.query(Vehicle)
        if status:
            sorgu = sorgu.filter_by(status=VehicleStatus(status))
        vehicles = [
            vehicle_to_dict(v)
            for v in sorgu.order_by(Vehicle.id.desc()).all()
        ]

    return templates.TemplateResponse(
        request=request,
        name="vehicles.html",
        context={
            "vehicles":       vehicles,
            "current_status": status or "",
            "pending_mode":   False,
            "title":          "Araç Listesi",
        },
    )


# ─────────────────────────────────────────────
# GET /access-logs   — Erişim kayıtları
# ─────────────────────────────────────────────

@app.get("/access-logs", response_class=HTMLResponse)
async def access_logs_list(request: Request):
    """
    Son 100 erişim kaydını en yeni önce gösterir.
    """
    with get_session() as session:
        logs = [
            log_to_dict(log)
            for log in session.query(AccessLog)
            .order_by(AccessLog.detected_at.desc())
            .limit(100)
            .all()
        ]

    return templates.TemplateResponse(
        request=request,
        name="access_logs.html",
        context={
            "logs": logs,
        },
    )


# ─────────────────────────────────────────────
# POST /vehicles/{vehicle_id}/approve
# ─────────────────────────────────────────────

@app.post("/vehicles/{vehicle_id}/approve")
async def approve_endpoint(vehicle_id: int):
    """
    Aracı onaylar (status=approved). Sonrası /pending sayfasına yönlendirir.
    """
    try:
        with get_session() as session:
            vehicle = session.query(Vehicle).filter_by(id=vehicle_id).first()
            if vehicle is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Araç bulunamadı: ID={vehicle_id}",
                )
            approve_vehicle(session, vehicle.normalized_plate, approved_by="web_guard")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Veritabanı hatası: {e}")

    return RedirectResponse("/pending", status_code=303)


# ─────────────────────────────────────────────
# POST /vehicles/{vehicle_id}/reject
# ─────────────────────────────────────────────

@app.post("/vehicles/{vehicle_id}/reject")
async def reject_endpoint(vehicle_id: int, reason: str = Form(default="")):
    """
    Aracı reddeder (status=rejected). Sonrası /pending sayfasına yönlendirir.
    """
    try:
        with get_session() as session:
            vehicle = session.query(Vehicle).filter_by(id=vehicle_id).first()
            if vehicle is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Araç bulunamadı: ID={vehicle_id}",
                )
            reject_vehicle(session, vehicle.normalized_plate, reason=reason or None)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Veritabanı hatası: {e}")

    return RedirectResponse("/pending", status_code=303)


# ─────────────────────────────────────────────
# POST /vehicles/{vehicle_id}/delete
# ─────────────────────────────────────────────

@app.post("/vehicles/{vehicle_id}/delete")
async def delete_endpoint(vehicle_id: int):
    """
    Aracı veritabanından kalıcı olarak siler (geçmiş loglar saklanır).
    Sonrasında /vehicles sayfasına yönlendirir.
    """
    try:
        with get_session() as session:
            vehicle = session.query(Vehicle).filter_by(id=vehicle_id).first()
            if vehicle is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Araç bulunamadı: ID={vehicle_id}",
                )
            delete_vehicle(session, vehicle.normalized_plate)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Veritabanı hatası: {e}")

    return RedirectResponse("/vehicles", status_code=303)

