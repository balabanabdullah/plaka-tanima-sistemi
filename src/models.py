import enum
from datetime import datetime, timezone
from typing import Optional, List

from sqlalchemy import String, Float, DateTime, ForeignKey, Enum as SAEnum, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ─────────────────────────────────────────────
# Yardımcı: UTC zaman üretimi
# ─────────────────────────────────────────────

def utc_now() -> datetime:
    """
    Şu anki UTC zamanını döndürür.
    Tüm tarih/saat alanları bu fonksiyon üzerinden doldurulmalıdır.
    """
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────
# Temel Model Sınıfı (SQLAlchemy 2.x)
# ─────────────────────────────────────────────

class Base(DeclarativeBase):
    """
    SQLAlchemy 2.x modern yaklaşımı.
    Eski declarative_base() kullanılmıyor.
    """
    pass


# ─────────────────────────────────────────────
# Enum Sınıfları
# ─────────────────────────────────────────────

class VehicleStatus(str, enum.Enum):
    """
    Araç kaydının mevcut durumu.
    - pending:  İlk kez görüldü, güvenlik onayı bekleniyor.
    - approved: Güvenlik görevlisi onayladı, geçiş yetkili.
    - rejected: Güvenlik görevlisi reddetti, geçiş yasak.
    - inactive: Daha önce onaylıydı, yetkisi kaldırıldı.
    """
    pending  = "pending"
    approved = "approved"
    rejected = "rejected"
    inactive = "inactive"


class AccessDirection(str, enum.Enum):
    """
    Aracın hareket yönü.
    - entry:   Araç giriş yapıyor.
    - exit:    Araç çıkış yapıyor.
    - unknown: Yön belirlenemedi.
    """
    entry   = "entry"
    exit    = "exit"
    unknown = "unknown"


class AccessDecision(str, enum.Enum):
    """
    Sisteme geçiş kararı.
    - allow:              Bariyer otomatik açılır.
    - wait_for_approval:  Bariyer kapalı, güvenlik ekranına düşer.
    - deny:               Bariyer kapalı, ret loglanır.
    - manual_override:    Güvenlik görevlisi elle müdahale etti.
    """
    allow              = "allow"
    wait_for_approval  = "wait_for_approval"
    deny               = "deny"
    manual_override    = "manual_override"


# ─────────────────────────────────────────────
# Vehicle (Araç) Modeli
# ─────────────────────────────────────────────

class Vehicle(Base):
    """
    Tespit edilen her plakaya ait ana kayıt.
    normalized_plate alanı UNIQUE olduğu için aynı plaka bir kez kaydedilir.
    """
    __tablename__ = "vehicles"

    # Birincil anahtar
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # OCR'dan gelen ham plaka metni (örn: "34 ABC 123")
    plate_text: Mapped[str] = mapped_column(String, nullable=False)

    # Temizlenmiş, karşılaştırılabilir plaka metni (örn: "34ABC123")
    # UNIQUE: aynı plakadan ikinci kayıt oluşmasını engeller
    normalized_plate: Mapped[str] = mapped_column(
        String, nullable=False, unique=True, index=True
    )

    # Aracın mevcut yetki durumu
    status: Mapped[VehicleStatus] = mapped_column(
        SAEnum(VehicleStatus, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=VehicleStatus.pending,
    )

    # İlk kez kamerada görüldüğü zaman (UTC)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # En son kamerada görüldüğü zaman (UTC)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Veritabanı kaydının oluşturulma zamanı (UTC)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Kaydın son güncellenme zamanı (UTC)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Onay zamanı (yalnızca approved araçlarda dolu) (UTC)
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Onaylayan operatör adı veya sistem kimliği
    approved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Serbest not alanı
    notes: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Bu araca ait erişim kayıtları (ilişki)
    access_logs: Mapped[List["AccessLog"]] = relationship(
        "AccessLog", back_populates="vehicle"
    )

    def __repr__(self) -> str:
        return (
            f"<Vehicle id={self.id} plate={self.normalized_plate!r} "
            f"status={self.status.value!r}>"
        )


# ─────────────────────────────────────────────
# AccessLog (Erişim Kaydı) Modeli
# ─────────────────────────────────────────────

class AccessLog(Base):
    """
    Her geçiş denemesinin tam kaydı.
    Hem başarılı hem başarısız tüm girişimler burada tutulur.
    """
    __tablename__ = "access_logs"

    # Birincil anahtar
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Bağlı araç kaydı (None: plaka henüz tanımlanamadı)
    vehicle_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("vehicles.id"), nullable=True, index=True
    )

    # OCR'dan gelen ham plaka metni
    plate_text: Mapped[str] = mapped_column(String, nullable=False)

    # Temizlenmiş plaka metni
    normalized_plate: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # Aracın hareket yönü (giriş / çıkış / bilinmiyor)
    direction: Mapped[AccessDirection] = mapped_column(
        SAEnum(AccessDirection, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=AccessDirection.unknown,
    )

    # Verilen geçiş kararı
    decision: Mapped[AccessDecision] = mapped_column(
        SAEnum(AccessDecision, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )

    # OCR güven değeri (0.0 – 1.0)
    ocr_confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )

    # Kamera tespiti zamanı (UTC)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Kırpılmış plaka görüntüsünün dosya yolu (isteğe bağlı)
    image_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Kameranın kimliği (örn: "cam_0", "cam_entry")
    source_camera: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Ret gerekçesi (yalnızca deny kararlarında dolu)
    denial_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Bağlı araç nesnesi (ilişki)
    vehicle: Mapped[Optional["Vehicle"]] = relationship(
        "Vehicle", back_populates="access_logs"
    )

    def __repr__(self) -> str:
        return (
            f"<AccessLog id={self.id} plate={self.normalized_plate!r} "
            f"decision={self.decision.value!r} direction={self.direction.value!r}>"
        )
