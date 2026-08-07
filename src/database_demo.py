"""
database_demo.py

Veritabanı katmanını OCR/kamera olmadan terminal üzerinde test eder.
Aşağıdaki senaryolar sırayla çalıştırılır:

    1.  Veritabanını oluştur.
    2.  İlk kez görülen plaka → pending + wait_for_approval
    3.  Aynı plakayı tekrar ekle → yeni araç oluşmamalı
    4.  Plakayı onayla → approved + allow
    5.  Yeni erişim kaydı oluştur.
    6.  Cooldown kontrolü → False (10 saniye dolmadı)
    7.  Başka bir plakayı reddet → rejected + deny
    8.  Başka bir plakayı devre dışı bırak → inactive + deny
    9.  Normalize testi → üç farklı yazım aynı sonucu üretmeli
    10. Tüm kayıtları terminalde okunabilir şekilde göster.

Demo tekrar çalıştırıldığında UNIQUE hatasıyla çökmez (idempotent).
"""

import sys
from pathlib import Path

# src/ klasörü Python yoluna ekleniyor
sys.path.insert(0, str(Path(__file__).resolve().parent))

from database import init_db, get_session
from models import AccessDirection, AccessDecision, VehicleStatus
from plate_service import (
    normalize_plate,
    get_or_create_vehicle,
    evaluate_access,
    create_access_log,
    should_log,
    approve_vehicle,
    reject_vehicle,
    deactivate_vehicle,
)


# ─────────────────────────────────────────────
# Yardımcı: Başlık yazdırma
# ─────────────────────────────────────────────

def baslik(metin: str) -> None:
    print()
    print("=" * 55)
    print(f"  {metin}")
    print("=" * 55)


# ─────────────────────────────────────────────
# SENARYO 1: Veritabanını oluştur
# ─────────────────────────────────────────────

baslik("1. Veritabanı oluşturuluyor")

# Temiz başlangıç: varsa eski veritabanı dosyasını sil
_db_dosyasi = Path(__file__).resolve().parent.parent / "data" / "plate_system.db"
if _db_dosyasi.exists():
    _db_dosyasi.unlink()
    print(f"Eski veritabanı silindi: {_db_dosyasi.name}")

init_db()


# ─────────────────────────────────────────────
# SENARYO 2: İlk kez görülen plaka
# ─────────────────────────────────────────────

baslik("2. İlk kez görülen plaka → pending + wait_for_approval")

PLAKA_HAM_1 = "34 ABC 123"

with get_session() as session:
    norm1 = normalize_plate(PLAKA_HAM_1)
    arac1, yeni_mi = get_or_create_vehicle(session, PLAKA_HAM_1, norm1)
    karar1 = evaluate_access(arac1)

    print(f"Ham plaka       : {PLAKA_HAM_1!r}")
    print(f"Normalize       : {norm1!r}")
    print(f"Yeni mi?        : {yeni_mi}")
    print(f"Durum           : {arac1.status.value}")
    print(f"Karar           : {karar1.value}")

    assert arac1.status == VehicleStatus.pending, "Durum pending olmalı!"
    assert karar1 == AccessDecision.wait_for_approval, "Karar wait_for_approval olmalı!"
    print("✓ Senaryo 2 BAŞARILI")


# ─────────────────────────────────────────────
# SENARYO 3: Aynı plakayı ikinci kez ekle
# ─────────────────────────────────────────────

baslik("3. Aynı plaka ikinci kez ekleniyor → yeni araç oluşmamalı")

with get_session() as session:
    norm1 = normalize_plate(PLAKA_HAM_1)
    arac1b, yeni_mi_b = get_or_create_vehicle(session, PLAKA_HAM_1, norm1)

    print(f"Araç ID         : {arac1b.id}")
    print(f"Yeni mi?        : {yeni_mi_b}")

    assert not yeni_mi_b, "İkinci çağrıda yeni araç oluşmamalı!"
    print("✓ Senaryo 3 BAŞARILI")


# ─────────────────────────────────────────────
# SENARYO 4: Plakayı onayla
# ─────────────────────────────────────────────

baslik("4. Plaka onaylanıyor → approved + allow")

with get_session() as session:
    norm1 = normalize_plate(PLAKA_HAM_1)
    arac1c = approve_vehicle(session, norm1, approved_by="guvenlik_01")
    karar1c = evaluate_access(arac1c)

    print(f"Yeni durum      : {arac1c.status.value}")
    print(f"Onaylayan       : {arac1c.approved_by}")
    print(f"Onay zamanı     : {arac1c.approved_at}")
    print(f"Karar           : {karar1c.value}")

    assert arac1c.status == VehicleStatus.approved, "Durum approved olmalı!"
    assert karar1c == AccessDecision.allow, "Karar allow olmalı!"
    print("✓ Senaryo 4 BAŞARILI")


# ─────────────────────────────────────────────
# SENARYO 5: Yeni erişim kaydı oluştur
# ─────────────────────────────────────────────

baslik("5. Yeni erişim kaydı oluşturuluyor")

with get_session() as session:
    norm1 = normalize_plate(PLAKA_HAM_1)
    # Aynı session içinde aracı al (ID mevcut session'a bağlı olmalı)
    arac1d, _ = get_or_create_vehicle(session, PLAKA_HAM_1, norm1)

    log1 = create_access_log(
        session=session,
        vehicle=arac1d,
        plate_text=PLAKA_HAM_1,
        normalized_plate=norm1,
        direction=AccessDirection.entry,
        decision=AccessDecision.allow,
        ocr_confidence=0.91,
        source_camera="cam_0",
    )

    print(f"Log ID          : {log1.id}")
    print(f"Yön             : {log1.direction.value}")
    print(f"Karar           : {log1.decision.value}")
    print(f"OCR Güven       : {log1.ocr_confidence}")
    print("✓ Senaryo 5 BAŞARILI")


# ─────────────────────────────────────────────
# SENARYO 6: Cooldown kontrolü
# ─────────────────────────────────────────────

baslik("6. Cooldown kontrolü → False (10 saniye dolmadı)")

with get_session() as session:
    norm1 = normalize_plate(PLAKA_HAM_1)
    sonuc = should_log(session, norm1, AccessDirection.entry, cooldown_seconds=10)

    print(f"Yeni log yazılsın mı? : {sonuc}")

    assert not sonuc, "Cooldown içinde False dönmeli!"
    print("✓ Senaryo 6 BAŞARILI")


# ─────────────────────────────────────────────
# SENARYO 7: Başka bir plakayı reddet
# ─────────────────────────────────────────────

baslik("7. Başka bir plaka ekleniyor ve reddediliyor → deny")

PLAKA_HAM_2 = "06-XY-999"

with get_session() as session:
    norm2 = normalize_plate(PLAKA_HAM_2)
    arac2, _ = get_or_create_vehicle(session, PLAKA_HAM_2, norm2)
    arac2 = reject_vehicle(session, norm2, reason="Şüpheli araç")
    karar2 = evaluate_access(arac2)

    print(f"Plaka           : {norm2!r}")
    print(f"Durum           : {arac2.status.value}")
    print(f"Karar           : {karar2.value}")
    print(f"Not             : {arac2.notes}")

    assert arac2.status == VehicleStatus.rejected, "Durum rejected olmalı!"
    assert karar2 == AccessDecision.deny, "Karar deny olmalı!"
    print("✓ Senaryo 7 BAŞARILI")


# ─────────────────────────────────────────────
# SENARYO 8: Başka bir plakayı devre dışı bırak
# ─────────────────────────────────────────────

baslik("8. Başka bir plaka ekleniyor ve devre dışı bırakılıyor → deny")

PLAKA_HAM_3 = "35.TZ.777"

with get_session() as session:
    norm3 = normalize_plate(PLAKA_HAM_3)
    arac3, _ = get_or_create_vehicle(session, PLAKA_HAM_3, norm3)
    # Önce onayla sonra devre dışı bırak
    approve_vehicle(session, norm3, approved_by="sistem")
    arac3 = deactivate_vehicle(session, norm3)
    karar3 = evaluate_access(arac3)

    print(f"Plaka           : {norm3!r}")
    print(f"Durum           : {arac3.status.value}")
    print(f"Karar           : {karar3.value}")

    assert arac3.status == VehicleStatus.inactive, "Durum inactive olmalı!"
    assert karar3 == AccessDecision.deny, "Karar deny olmalı!"
    print("✓ Senaryo 8 BAŞARILI")


# ─────────────────────────────────────────────
# SENARYO 9: Normalize testi
# ─────────────────────────────────────────────

baslik("9. Normalize testi → üç farklı yazım aynı sonucu üretmeli")

yazilar = ["34 ABC 123", "34-ABC-123", "34ABC123"]
for yazi in yazilar:
    sonuc = normalize_plate(yazi)
    eslesme = sonuc == "34ABC123"
    print(f"  {yazi!r:20} → {sonuc!r}  {'✓' if eslesme else '✗'}")
    assert eslesme, f"Normalize başarısız: {yazi!r} → {sonuc!r}"

print("✓ Senaryo 9 BAŞARILI")


# ─────────────────────────────────────────────
# SENARYO 10: Tüm kayıtları listele
# ─────────────────────────────────────────────

baslik("10. Tüm Vehicles kayıtları")

import models as _m

with get_session() as session:
    araclar = session.query(_m.Vehicle).order_by(_m.Vehicle.id).all()
    print(f"Toplam araç kaydı: {len(araclar)}")
    print()
    for a in araclar:
        print(
            f"  [{a.id:>3}] {a.normalized_plate:<12} "
            f"durum={a.status.value:<10} "
            f"ilk_gorulme={a.first_seen_at.strftime('%H:%M:%S')}"
        )

baslik("10. Tüm AccessLogs kayıtları")

with get_session() as session:
    loglar = session.query(_m.AccessLog).order_by(_m.AccessLog.id).all()
    print(f"Toplam erişim kaydı: {len(loglar)}")
    print()
    for log in loglar:
        print(
            f"  [{log.id:>3}] {log.normalized_plate:<12} "
            f"karar={log.decision.value:<20} "
            f"yon={log.direction.value:<7} "
            f"guven={log.ocr_confidence:.2f}"
        )

baslik("Tüm senaryolar tamamlandı ✓")
