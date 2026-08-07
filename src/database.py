import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
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

# DATABASE_URL çevre değişkeni varsa onu kullan; yoksa yerel SQLite
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{DEFAULT_DB_PATH}"
)


# ─────────────────────────────────────────────
# Engine oluşturma
# ─────────────────────────────────────────────

# check_same_thread=False: SQLite'ın farklı thread'lere izin vermesi için
# (FastAPI gibi asenkron ortamlarda gereklidir)
_connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    echo=False,   # True yapılırsa SQL sorguları terminale yazdırılır (hata ayıklama için)
)


# ─────────────────────────────────────────────
# Session Factory
# ─────────────────────────────────────────────

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


# ─────────────────────────────────────────────
# Veritabanını Başlatma
# ─────────────────────────────────────────────

def init_db() -> None:
    """
    data/ klasörünü gerektiğinde oluşturur ve tüm tabloları veritabanında yaratır.
    Tablolar zaten varsa değiştirilmez.
    """
    # SQLite için data/ klasörünü oluştur (PostgreSQL'de gerekli değil)
    if DATABASE_URL.startswith("sqlite"):
        db_file = Path(DATABASE_URL.replace("sqlite:///", ""))
        db_file.parent.mkdir(parents=True, exist_ok=True)

    # Tüm modelleri içe aktarmak Base.metadata'yı doldurur
    Base.metadata.create_all(bind=engine)
    print(f"Veritabanı hazır: {DATABASE_URL}")


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
