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

import os
import secrets
import uuid
from typing import List, Optional, Any
from pydantic import BaseModel, Field

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from database import init_db, get_session
from models import AccessLog, Vehicle, VehicleStatus, AccessDirection, AccessDecision, utc_now
from plate_service import approve_vehicle, reject_vehicle, delete_vehicle, get_vehicles_presence_map

# ─────────────────────────────────────────────
# Proje kök dizini
# Path(__file__) -> src/web_app.py
# .parent       -> src/
# .parent       -> proje kökü
# ─────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Geçerli status değerleri (filtre doğrulaması için)
GECERLI_STATUSLAR = {"pending", "approved", "rejected", "inactive"}
WEB_SESSION_MAX_AGE = 8 * 60 * 60


class AppEnvSessionMiddleware(SessionMiddleware):
    """APP_ENV production iken session cookie'sine Secure bayragi ekler."""

    async def __call__(self, scope, receive, send):
        self.security_flags = "httponly; samesite=lax"
        if os.environ.get("APP_ENV", "development").strip().lower() == "production":
            self.security_flags += "; secure"
        await super().__call__(scope, receive, send)


def get_web_auth_config() -> tuple[str, str, str]:
    """Web auth ayarlarini ortamdan okur; degerleri loglamaz."""
    return (
        os.environ.get("WEB_ADMIN_USERNAME", "").strip(),
        os.environ.get("WEB_ADMIN_PASSWORD", ""),
        os.environ.get("WEB_SESSION_SECRET", "").strip(),
    )


def ensure_web_auth_configured() -> tuple[str, str, str]:
    """Eksik auth ayarinda paneli fail-closed tutar."""
    username, password, session_secret = get_web_auth_config()
    if not username or not password or not session_secret:
        raise HTTPException(
            status_code=503,
            detail="Web panel authentication is not configured.",
        )
    return username, password, session_secret


def get_or_create_csrf_token(request: Request) -> str:
    """Session icinde tahmin edilemez CSRF token olusturur veya mevcut olani doner."""
    token = request.session.get("csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def verify_csrf_token(request: Request, provided_token: str) -> None:
    """CSRF token'i constant-time karsilastirma ile dogrular."""
    expected_token = request.session.get("csrf_token", "")
    if (
        not isinstance(expected_token, str)
        or not expected_token
        or not provided_token
        or not secrets.compare_digest(provided_token, expected_token)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token.")


def require_web_auth(request: Request) -> str:
    """Panel route'lari icin config ve authenticated session kontrolu."""
    username, _, _ = ensure_web_auth_configured()
    if request.session.get("authenticated") is not True:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    session_username = request.session.get("username", "")
    if not isinstance(session_username, str) or not secrets.compare_digest(session_username, username):
        request.session.clear()
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return session_username


def web_template_context(request: Request, **values) -> dict:
    """Tum panel template'leri icin auth ve CSRF context'i hazirlar."""
    context = {
        "authenticated": request.session.get("authenticated") is True,
        "current_username": request.session.get("username"),
        "csrf_token": get_or_create_csrf_token(request),
    }
    context.update(values)
    return context


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

def vehicle_to_dict(v: Vehicle, presence_info: dict | None = None) -> dict:
    """Vehicle modelini ve isteğe bağlı varlık durumu bilgilerini sözlüğe dönüştürür."""
    d = {
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
        "presence_state":   "outside",
        "presence_label":   "Dışarıda",
        "last_movement_label": "-",
        "last_movement_time": None,
    }
    if presence_info:
        d.update(presence_info)
    return d


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
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# Secret eksikse random process anahtari middleware'in calismasini saglar;
# ensure_web_auth_configured() panel loginini yine 503 ile fail-closed tutar.
_configured_session_secret = os.environ.get("WEB_SESSION_SECRET", "").strip()
app.add_middleware(
    AppEnvSessionMiddleware,
    secret_key=_configured_session_secret or secrets.token_urlsafe(32),
    session_cookie="plate_admin_session",
    max_age=WEB_SESSION_MAX_AGE,
    same_site="lax",
    https_only=False,
)

# Statik dosyalar (CSS)
app.mount(
    "/static",
    StaticFiles(directory=str(PROJECT_ROOT / "static")),
    name="static",
)

# Jinja2 şablon yöneticisini yapılandır
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "templates"))

# yerel_zaman fonksiyonunu tüm şablonlara global olarak kaydet
templates.env.globals["yerel_zaman"] = yerel_zaman


@app.get("/health")
def health_check():
    """
    Docker / Kubernetes health check ve canlılık testi endpoint'i.
    Veritabanına sorgu atmaz, hafif yanıt döner.
    """
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Yonetici login formunu gosterir."""
    ensure_web_auth_configured()
    if request.session.get("authenticated") is True:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=web_template_context(request, error=None),
    )


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(default=""),
):
    """Yonetici bilgilerini dogrular ve authenticated session olusturur."""
    expected_username, expected_password, _ = ensure_web_auth_configured()
    verify_csrf_token(request, csrf_token)

    username_ok = secrets.compare_digest(username, expected_username)
    password_ok = secrets.compare_digest(password, expected_password)
    if not (username_ok and password_ok):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=web_template_context(
                request,
                error="Kullanıcı adı veya parola hatalı",
            ),
            status_code=401,
        )

    request.session.clear()
    request.session["authenticated"] = True
    request.session["username"] = expected_username
    request.session["csrf_token"] = secrets.token_urlsafe(32)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout_submit(
    request: Request,
    csrf_token: str = Form(default=""),
    _username: str = Depends(require_web_auth),
):
    """CSRF dogrulamasi sonrasinda authenticated session'i temizler."""
    verify_csrf_token(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ─────────────────────────────────────────────
# GET /   — Dashboard
# ─────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, _username: str = Depends(require_web_auth)):
    """
    Ana sayfa: durum sayaçları, pending araçlar ve son erişim kayıtları.
    """
    with get_session() as session:
        # İçerideki araç sayısını hesapla
        all_vehicles = session.query(Vehicle).all()
        presence_map = get_vehicles_presence_map(session, all_vehicles)
        inside_count = sum(1 for p in presence_map.values() if p["presence_state"] == "inside")

        # Durum sayaçları
        stats = {
            "pending":  session.query(Vehicle).filter_by(status=VehicleStatus.pending).count(),
            "approved": session.query(Vehicle).filter_by(status=VehicleStatus.approved).count(),
            "rejected": session.query(Vehicle).filter_by(status=VehicleStatus.rejected).count(),
            "inactive": session.query(Vehicle).filter_by(status=VehicleStatus.inactive).count(),
            "inside":   inside_count,
        }

        # İlk 5 pending araç (en eski önce)
        raw_pending = (
            session.query(Vehicle)
            .filter_by(status=VehicleStatus.pending)
            .order_by(Vehicle.first_seen_at.asc())
            .limit(5)
            .all()
        )
        pending_presence = get_vehicles_presence_map(session, raw_pending)
        pending_vehicles = [
            vehicle_to_dict(v, pending_presence.get(v.id))
            for v in raw_pending
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
        context=web_template_context(
            request,
            stats=stats,
            pending_vehicles=pending_vehicles,
            recent_logs=recent_logs,
        ),
    )


# ─────────────────────────────────────────────
# GET /pending   — Pending araçlar (approve/reject butonlu)
# ─────────────────────────────────────────────

@app.get("/pending", response_class=HTMLResponse)
async def pending_list(request: Request, _username: str = Depends(require_web_auth)):
    """
    Onay bekleyen tüm araçları Approve / Reject butonlarıyla listeler.
    """
    with get_session() as session:
        raw_vehicles = (
            session.query(Vehicle)
            .filter_by(status=VehicleStatus.pending)
            .order_by(Vehicle.first_seen_at.asc())
            .all()
        )
        presence_map = get_vehicles_presence_map(session, raw_vehicles)
        vehicles = [
            vehicle_to_dict(v, presence_map.get(v.id))
            for v in raw_vehicles
        ]

    return templates.TemplateResponse(
        request=request,
        name="vehicles.html",
        context=web_template_context(
            request,
            vehicles=vehicles,
            current_status="pending",
            pending_mode=True,
            title="Onay Bekleyen Araçlar",
        ),
    )


# ─────────────────────────────────────────────
# GET /vehicles   — Tüm araçlar (filtreli)
# ─────────────────────────────────────────────

@app.get("/vehicles", response_class=HTMLResponse)
async def vehicles_list(
    request: Request,
    status: str | None = None,
    _username: str = Depends(require_web_auth),
):
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
        raw_vehicles = sorgu.order_by(Vehicle.id.desc()).all()
        presence_map = get_vehicles_presence_map(session, raw_vehicles)
        vehicles = [
            vehicle_to_dict(v, presence_map.get(v.id))
            for v in raw_vehicles
        ]

    return templates.TemplateResponse(
        request=request,
        name="vehicles.html",
        context=web_template_context(
            request,
            vehicles=vehicles,
            current_status=status or "",
            pending_mode=False,
            title="Araç Listesi",
        ),
    )


# ─────────────────────────────────────────────
# GET /access-logs   — Erişim kayıtları
# ─────────────────────────────────────────────

@app.get("/access-logs", response_class=HTMLResponse)
async def access_logs_list(request: Request, _username: str = Depends(require_web_auth)):
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
        context=web_template_context(request, logs=logs),
    )


# ─────────────────────────────────────────────
# POST /vehicles/{vehicle_id}/approve
# ─────────────────────────────────────────────

@app.post("/vehicles/{vehicle_id}/approve")
async def approve_endpoint(
    vehicle_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    _username: str = Depends(require_web_auth),
):
    """
    Aracı onaylar (status=approved). Sonrası /pending sayfasına yönlendirir.
    """
    verify_csrf_token(request, csrf_token)
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
async def reject_endpoint(
    vehicle_id: int,
    request: Request,
    reason: str = Form(default=""),
    csrf_token: str = Form(default=""),
    _username: str = Depends(require_web_auth),
):
    """
    Aracı reddeder (status=rejected). Sonrası /pending sayfasına yönlendirir.
    """
    verify_csrf_token(request, csrf_token)
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
async def delete_endpoint(
    vehicle_id: int,
    request: Request,
    csrf_token: str = Form(default=""),
    _username: str = Depends(require_web_auth),
):
    """
    Aracı veritabanından kalıcı olarak siler (geçmiş loglar saklanır).
    Sonrasında /vehicles sayfasına yönlendirir.
    """
    verify_csrf_token(request, csrf_token)
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


# ─────────────────────────────────────────────
# HTTPS Senkronizasyon API Güvenliği & Modelleri
# ─────────────────────────────────────────────

def verify_sync_token(request: Request) -> bool:
    """
    HTTP isteğindeki Authorization başlığını doğrular.
    - SYNC_API_TOKEN sunucuda tanımlı değilse -> 503
    - Authorization başlığı yoksa -> 401
    - Geçersiz token / format -> 403
    - secrets.compare_digest() ile güvenli zamanlama karşılaştırması yapılır.
    - Token hiçbir şekilde loglanmaz veya hata mesajında döndürülmez.
    """
    expected_token = os.environ.get("SYNC_API_TOKEN", "").strip()
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="SYNC_API_TOKEN is not configured on the server."
        )

    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization header."
        )

    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=403,
            detail="Invalid Authorization header format. Expected 'Bearer <token>'."
        )

    provided_token = parts[1]
    if not secrets.compare_digest(provided_token, expected_token):
        raise HTTPException(
            status_code=403,
            detail="Forbidden: Invalid sync token."
        )

    return True


def ensure_utc_naive(val: Any) -> Optional[datetime]:
    """
    Tarih/saat değerini (dize, offset-aware datetime veya offset-naive datetime)
    standart UTC naive datetime nesnesine dönüştürür.

    Veritabanından (PostgreSQL vs SQLite) gelen aware/naive nesnelerin
    hata vermeden güvenle karşılaştırılabilmesini sağlar.
    """
    if val is None:
        return None

    dt: Optional[datetime] = None

    if isinstance(val, str):
        val_str = val.strip()
        if not val_str:
            return None
        try:
            dt = datetime.fromisoformat(val_str)
        except Exception:
            return None
    elif isinstance(val, datetime):
        dt = val
    else:
        return None

    if dt is not None and dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)

    return dt


def parse_iso_dt(val: Any) -> Optional[datetime]:
    """ISO formatındaki tarih metnini datetime nesnesine dönüştürür (UTC naive)."""
    return ensure_utc_naive(val)


class VehicleSyncItem(BaseModel):
    sync_id: Optional[str] = None
    plate_text: str
    normalized_plate: str
    status: Optional[str] = "pending"
    approved_at: Optional[str] = None
    approved_by: Optional[str] = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AccessLogSyncItem(BaseModel):
    sync_id: Optional[str] = None
    vehicle_sync_id: Optional[str] = None
    plate_text: str
    normalized_plate: str
    direction: str
    decision: str
    ocr_confidence: float
    source_camera: str
    detected_at: Optional[str] = None


class PushSyncRequest(BaseModel):
    vehicles: List[VehicleSyncItem] = Field(default_factory=list)
    access_logs: List[AccessLogSyncItem] = Field(default_factory=list)


class ApprovalsSyncRequest(BaseModel):
    vehicle_sync_ids: List[str] = Field(default_factory=list)


# ─────────────────────────────────────────────
# POST /api/sync/push — Yerelden Buluta Aktarım
# ─────────────────────────────────────────────

@app.post("/api/sync/push")
async def sync_push_endpoint(payload: PushSyncRequest, request: Request):
    """
    Yerel SQLite veritabanından gelen Araç ve AccessLog kayıtlarını buluta aktarır.
    İdempotenttir; mükerrer satır oluşturmaz.
    """
    verify_sync_token(request)

    vehicle_stats = {"new": 0, "updated": 0, "unchanged": 0}
    log_stats = {"new": 0, "updated": 0, "unchanged": 0}

    with get_session() as session:
        # 1. Araç Senkronizasyonu
        for vp in payload.vehicles:
            cv = None
            if vp.sync_id:
                cv = session.query(Vehicle).filter_by(sync_id=vp.sync_id).first()
            if cv is None and vp.normalized_plate:
                cv = session.query(Vehicle).filter_by(normalized_plate=vp.normalized_plate).first()

            if cv is None:
                try:
                    v_status = VehicleStatus(vp.status) if vp.status else VehicleStatus.pending
                except ValueError:
                    v_status = VehicleStatus.pending

                new_sync_id = vp.sync_id or str(uuid.uuid4())
                now_naive = ensure_utc_naive(utc_now())
                cv = Vehicle(
                    sync_id=new_sync_id,
                    plate_text=vp.plate_text,
                    normalized_plate=vp.normalized_plate,
                    status=v_status,
                    approved_at=ensure_utc_naive(vp.approved_at),
                    approved_by=vp.approved_by,
                    first_seen_at=ensure_utc_naive(vp.first_seen_at) or now_naive,
                    last_seen_at=ensure_utc_naive(vp.last_seen_at) or now_naive,
                    created_at=ensure_utc_naive(vp.created_at) or now_naive,
                    updated_at=ensure_utc_naive(vp.updated_at) or now_naive,
                    notes=vp.notes,
                )
                session.add(cv)
                session.flush()
                vehicle_stats["new"] += 1
            else:
                has_changes = False
                if vp.sync_id and cv.sync_id != vp.sync_id:
                    cv.sync_id = vp.sync_id
                    has_changes = True

                if vp.last_seen_at:
                    incoming_last_seen = ensure_utc_naive(vp.last_seen_at)
                    existing_last_seen = ensure_utc_naive(cv.last_seen_at)
                    if incoming_last_seen and (existing_last_seen is None or incoming_last_seen > existing_last_seen):
                        cv.last_seen_at = incoming_last_seen
                        has_changes = True

                if vp.notes and cv.notes != vp.notes:
                    cv.notes = vp.notes
                    has_changes = True

                if has_changes:
                    cv.updated_at = ensure_utc_naive(utc_now())
                    vehicle_stats["updated"] += 1
                else:
                    vehicle_stats["unchanged"] += 1

        # 2. AccessLog Senkronizasyonu
        for lp in payload.access_logs:
            if not lp.sync_id:
                continue

            existing_log = session.query(AccessLog).filter_by(sync_id=lp.sync_id).first()
            if existing_log is not None:
                log_stats["unchanged"] += 1
                continue

            # Bulut aracını vehicle_sync_id veya normalized_plate üzerinden bul
            cv = None
            if lp.vehicle_sync_id:
                cv = session.query(Vehicle).filter_by(sync_id=lp.vehicle_sync_id).first()
            if cv is None and lp.normalized_plate:
                cv = session.query(Vehicle).filter_by(normalized_plate=lp.normalized_plate).first()

            if cv is None:
                continue

            try:
                dir_enum = AccessDirection(lp.direction)
            except ValueError:
                dir_enum = AccessDirection.unknown

            try:
                dec_enum = AccessDecision(lp.decision)
            except ValueError:
                dec_enum = AccessDecision.wait_for_approval

            log_detected_at = ensure_utc_naive(lp.detected_at) or ensure_utc_naive(utc_now())
            new_log = AccessLog(
                sync_id=lp.sync_id,
                vehicle_id=cv.id,
                plate_text=lp.plate_text,
                normalized_plate=lp.normalized_plate,
                direction=dir_enum,
                decision=dec_enum,
                ocr_confidence=lp.ocr_confidence,
                source_camera=lp.source_camera,
                detected_at=log_detected_at,
            )
            session.add(new_log)
            log_stats["new"] += 1

        session.commit()

    return {
        "vehicles": vehicle_stats,
        "access_logs": log_stats,
    }


# ─────────────────────────────────────────────
# POST /api/sync/approvals — Buluttan Yerele Yetki Aktarımı
# ─────────────────────────────────────────────

@app.post("/api/sync/approvals")
async def sync_approvals_endpoint(payload: ApprovalsSyncRequest, request: Request):
    """
    Yerel istemcinin gönderdiği vehicle_sync_ids kümesine karşılık gelen
    bulut yetkilendirme kararlarını (status, approved_at, approved_by, notes) döndürür.
    """
    verify_sync_token(request)

    if not payload.vehicle_sync_ids:
        return []

    with get_session() as session:
        vehicles = (
            session.query(Vehicle)
            .filter(Vehicle.sync_id.in_(payload.vehicle_sync_ids))
            .all()
        )
        results = []
        for v in vehicles:
            approved_dt = ensure_utc_naive(v.approved_at)
            results.append({
                "sync_id": v.sync_id,
                "status": v.status.value if hasattr(v.status, "value") else str(v.status),
                "approved_at": approved_dt.isoformat() if approved_dt else None,
                "approved_by": v.approved_by,
                "notes": v.notes,
            })
        return results
