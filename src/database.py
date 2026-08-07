import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from models import Base

# ─────────────────────────────────────────────
# Proje kök dizini ve varsayılan veritabanı yolu
# ─────────────────────────────────────────────

# Path(__file__) -> src/database.py
# .parent       -> src/
# .parent       -> proje kökü
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "plate_system.db"


def get_database_url() -> str:
    """
    Çevre değişkenlerinden DATABASE_URL okur.
    Tetiklenmemiş, None veya boş bırakılmışsa varsayılan yerel SQLite yolunu döndürür.
    """
    raw_url = os.environ.get("DATABASE_URL", "")
    if raw_url is None or not raw_url.strip():
        return f"sqlite:///{DEFAULT_DB_PATH}"
    return raw_url.strip()


# Geriye dönük uyumluluk için takma ad (alias)
get_db_url = get_database_url


def sanitize_db_url(url: str) -> str:
    """
    Terminal veya log çıktıları için veritabanı şifre / kimlik bilgilerini maskeler.
    """
    if "@" in url and "://" in url:
        try:
            scheme_user_pass, host_db = url.rsplit("@", 1)
            scheme, user_pass = scheme_user_pass.split("://", 1)
            if ":" in user_pass:
                user, _ = user_pass.split(":", 1)
                return f"{scheme}://{user}:***@{host_db}"
            return f"{scheme}://***@{host_db}"
        except Exception:
            return "DATABASE_URL (kimlik bilgileri gizlendi)"
    return url


# Modül seviyesinde veritabanı URL'si
DATABASE_URL = get_database_url()

# ─────────────────────────────────────────────
# Engine ve Session Factory Yöneticisi
# ─────────────────────────────────────────────

_engine = None
_SessionLocal = None


def get_engine():
    """
    SQLAlchemy Engine nesnesini döndürür veya gerektiğinde tembel (lazy) olarak oluşturur.
    """
    global _engine
    if _engine is None:
        url = get_database_url()
        _connect_args = {}
        if url.startswith("sqlite"):
            _connect_args = {"check_same_thread": False}
        _engine = create_engine(url, connect_args=_connect_args, echo=False)
    return _engine


def get_session_factory():
    """
    SQLAlchemy SessionLocal (sessionmaker) fabrikasını döndürür.
    """
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autocommit=False,
            autoflush=False,
        )
    return _SessionLocal


# Modül seviyesinde ön-başlatma (sürücü eksikliği durumunda hata vermemesi için yakalanır)
try:
    _connect_args = {}
    if DATABASE_URL.startswith("sqlite"):
        _connect_args = {"check_same_thread": False}
    _engine = create_engine(DATABASE_URL, connect_args=_connect_args, echo=False)
    _SessionLocal = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
except Exception:
    pass

engine = _engine
SessionLocal = _SessionLocal


# ─────────────────────────────────────────────
# Veritabanını Başlatma
# ─────────────────────────────────────────────

def init_db() -> None:
    """
    data/ klasörünü gerektiğinde oluşturur ve tüm tabloları veritabanında yaratır.
    Tablolar zaten varsa değiştirilmez.
    """
    url = get_database_url()

    # SQLite için varsayılan data/ klasörünü oluştur (PostgreSQL vb. için gerekli değil)
    if url.startswith("sqlite"):
        try:
            path_str = url.replace("sqlite:///", "").replace("sqlite://", "")
            if path_str and not path_str.startswith(":memory:"):
                db_file = Path(path_str)
                if db_file.parent:
                    db_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    eng = get_engine()
    Base.metadata.create_all(bind=eng)
    sanitized_url = sanitize_db_url(url)
    print(f"Veritabanı hazır: {sanitized_url}")


# ─────────────────────────────────────────────
# Session Yöneticisi
# ─────────────────────────────────────────────

@contextmanager
def get_session():
    """
    Context manager olarak kullanılır.
    Başarılı durumda commit, hata durumunda rollback, her durumda close.

    Kullanım:
        with get_session() as session:
            araç = session.query(Vehicle).first()
    """
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

