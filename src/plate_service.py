import re
from datetime import timedelta

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from models import (
    Vehicle, AccessLog,
    VehicleStatus, AccessDirection, AccessDecision,
    utc_now,
)


# ─────────────────────────────────────────────
# Plaka Normalizasyonu
# ─────────────────────────────────────────────

def normalize_plate(text: str) -> str:
    """
    Ham OCR metnini karşılaştırılabilir standart biçime dönüştürür.

    Kurallar:
    - Başındaki/sonundaki boşlukları kaldır.
    - Tümünü büyük harfe çevir.
    - Tire, nokta, boşluk ve benzeri ayraçları kaldır.
    - Yalnızca A-Z ve 0-9 karakterlerini tut.
    - O/0 veya I/1 gibi tahmine dayalı dönüşüm yapmaz.
    - Ülkeye özel format kontrolü yapmaz.

    Parametreler:
        text (str): Ham OCR metni (örn: "34 ABC-123")

    Döndürür:
        str: Normalize edilmiş plaka (örn: "34ABC123")

    Hatalar:
        ValueError: Metin boş kalırsa üretilir.
    """
    text = text.strip().upper()
    text = re.sub(r"[^A-Z0-9]", "", text)

    if not text:
        raise ValueError("Plaka metni normalize edildikten sonra boş kaldı.")

    return text


# ─────────────────────────────────────────────
# Plaka Benzerliği / Varyasyon Bastırma
# ─────────────────────────────────────────────

def levenshtein_distance(s1: str, s2: str) -> int:
    """
    İki dize arasındaki Levenshtein düzenleme mesafesini (edit distance) hesaplar.
    """
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


def is_probable_same_plate(text_a: str, text_b: str) -> bool:
    """
    İki ham veya normalize edilmiş OCR metninin aynı fiziksel plakaya ait
    olup olmadığını muhafazakar bir yaklaşımla değerlendirir.

    ÖNEMLİ: Bu fonksiyon YALNIZCA aynı canlı kamera geçiş olayı (event) sırasında
    OCR varyasyonlarını (gürültülerini) bastırmak için bellekte (in-memory) kullanılır.
    Kesinlikle veritabanındaki Vehicle kayıtlarını birleştirmez veya değiştirmez.

    Muhafazakar Kurallar (False-Negative Tercih Edilir):
    1. Boş metinler veya normalize edilemeyenler -> False.
    2. Birebir eşitlik -> True.
    3. Karakter uzunluğu farkı > 2 ise kesinlikle farklı plaka -> False.
    4. Metin uzunlukları >= 6 ve fark <= 2 iken alt dize/içerme (örn: TR82TR37 - 82TR37) -> True.
    5. Levenshtein mesafesi:
       - 5-6 karakterli plakalar için max 1 düzenleme.
       - 7 ve üzeri karakterli plakalar için max 2 düzenleme.
    """
    if not text_a or not text_b:
        return False

    try:
        na = normalize_plate(text_a)
        nb = normalize_plate(text_b)
    except ValueError:
        return False

    if na == nb:
        return True

    len_a, len_b = len(na), len(nb)
    if abs(len_a - len_b) > 2:
        return False

    min_len = min(len_a, len_b)
    if min_len < 4:
        return False

    # Alt dize / ek parçalanması (örn. TR82TR37 vs 82TR37, 34FRK052 vs B34FRK052)
    if min_len >= 6 and (na in nb or nb in na):
        return True

    # Aynı uzunluktaki plakalarda genel Levenshtein eşleşmesi tehlikelidir.
    # Örneğin 34ABC123 ve 34ABC128 iki gerçek farklı araç olabilir. Bu nedenle
    # yalnızca ilk iki karakter gürültülüyken ve kalan uzun bölüm birebir aynıysa
    # aynı fiziksel olay varyasyonu kabul edilir (örn. BLFRK052 / 34FRK052).
    if len_a == len_b and len_a >= 7:
        farkli_indeksler = [i for i, (a, b) in enumerate(zip(na, nb)) if a != b]
        if farkli_indeksler and max(farkli_indeksler) < 2 and na[2:] == nb[2:]:
            return levenshtein_distance(na, nb) <= 2

    # Uzunluk farklıysa eksik/fazla okunmuş bir kenar karakteri olasılığı vardır.
    # Aynı uzunluktaki gerçek plakalar bu kola giremez.
    if len_a != len_b:
        distance = levenshtein_distance(na, nb)
        max_len = max(len_a, len_b)
        if max_len >= 7 and distance <= 2:
            return True
        if 5 <= max_len < 7 and distance <= 1:
            return True

    return False


# ─────────────────────────────────────────────
# Otomatik Yön ve Varlık (Presence) Yönetimi
# ─────────────────────────────────────────────

def get_last_successful_allow_log(
    session: Session,
    vehicle: Vehicle,
) -> AccessLog | None:
    """
    Verilen araç için veritabanında kayıtlı en son başarılı (ALLOW) geçiş kaydını döner.
    WAIT_FOR_APPROVAL, DENY veya pending/rejected durumlarındaki loglar hesaba katılmaz.
    """
    if vehicle is None or vehicle.id is None:
        return None

    return (
        session.query(AccessLog)
        .filter(
            AccessLog.vehicle_id == vehicle.id,
            AccessLog.decision == AccessDecision.allow,
        )
        .order_by(AccessLog.detected_at.desc(), AccessLog.id.desc())
        .first()
    )


def get_vehicle_presence_state(
    session: Session,
    vehicle: Vehicle,
) -> str:
    """
    Aracın son başarılı (ALLOW) geçiş kaydına göre mevcut varlık durumunu döner.

    Döndürülen Değerler:
        - "inside"   : En son ALLOW kaydının yönü 'entry' ise
        - "outside"  : En son ALLOW kaydının yönü 'exit' ise veya hiç ALLOW kaydı yoksa
        - "unknown"  : En son ALLOW kaydının yönü 'unknown' ise
    """
    last_allow = get_last_successful_allow_log(session, vehicle)
    if last_allow is None:
        return "outside"

    if last_allow.direction == AccessDirection.entry:
        return "inside"
    elif last_allow.direction == AccessDirection.exit:
        return "outside"
    else:
        return "unknown"


def get_next_auto_direction(
    session: Session,
    vehicle: Vehicle | None,
) -> AccessDirection:
    """
    AUTO yön kipinde (--direction auto), aracın en son ALLOW kaydına göre
    bir sonraki geçiş yönünü belirler.

    Kurallar:
        - Araç None veya hiç ALLOW kaydı yoksa (outside) -> entry
        - En son ALLOW 'entry' ise (inside)              -> exit
        - En son ALLOW 'exit' ise (outside)              -> entry
        - En son ALLOW 'unknown' ise                     -> entry (güvenli varsayılan)
    """
    if vehicle is None:
        return AccessDirection.entry

    presence = get_vehicle_presence_state(session, vehicle)
    if presence == "inside":
        return AccessDirection.exit
    elif presence == "outside":
        return AccessDirection.entry
    else:
        return AccessDirection.entry


def get_vehicles_presence_map(
    session: Session,
    vehicles: list[Vehicle],
) -> dict[int, dict]:
    """
    Verilen araç listesi için varlık durumlarını ve son hareket bilgilerini
    N+1 sorgu problemi yaratmadan verimli şekilde hesaplar.

    Döndürür:
        dict[vehicle_id, {
            "presence_state": "inside" | "outside" | "unknown",
            "presence_label": "İçeride" | "Dışarıda" | "Bilinmiyor",
            "last_movement_label": "Giriş" | "Çıkış" | "-",
            "last_movement_time": datetime | None
        }]
    """
    result = {}
    if not vehicles:
        return result

    vehicle_ids = [v.id for v in vehicles if v.id is not None]
    if not vehicle_ids:
        return result

    from sqlalchemy import func
    subq = (
        session.query(
            AccessLog.vehicle_id,
            func.max(AccessLog.id).label("max_id"),
        )
        .filter(
            AccessLog.vehicle_id.in_(vehicle_ids),
            AccessLog.decision == AccessDecision.allow,
        )
        .group_by(AccessLog.vehicle_id)
        .subquery()
    )

    latest_logs = (
        session.query(AccessLog)
        .join(subq, AccessLog.id == subq.c.max_id)
        .all()
    )

    log_map = {log.vehicle_id: log for log in latest_logs}

    for v in vehicles:
        log = log_map.get(v.id)
        if log is None:
            state = "outside"
            label = "Dışarıda"
            m_label = "-"
            m_time = None
        else:
            m_time = log.detected_at
            if log.direction == AccessDirection.entry:
                state = "inside"
                label = "İçeride"
                m_label = "Giriş"
            elif log.direction == AccessDirection.exit:
                state = "outside"
                label = "Dışarıda"
                m_label = "Çıkış"
            else:
                state = "unknown"
                label = "Bilinmiyor"
                m_label = "-"

        result[v.id] = {
            "presence_state": state,
            "presence_label": label,
            "last_movement_label": m_label,
            "last_movement_time": m_time,
        }

    return result



def get_vehicle_by_plate(
    session: Session,
    normalized_plate: str,
) -> Vehicle | None:
    """
    normalized_plate ile veritabanından Vehicle kaydını getirir.
    Sadece okuma yapar: yeni kayıt oluşturmaz, last_seen_at değiştirmez.

    Parametreler:
        session (Session): Açık veritabanı oturumu.
        normalized_plate (str): Normalize edilmiş plaka.

    Döndürür:
        Vehicle | None: Bulunan araç nesnesi veya None.
    """
    return (
        session.query(Vehicle)
        .filter_by(normalized_plate=normalized_plate)
        .first()
    )


# ─────────────────────────────────────────────
# Araç Kayıt Yönetimi
# ─────────────────────────────────────────────

def get_or_create_vehicle(
    session: Session,
    plate_text: str,
    normalized_plate: str,
) -> tuple[Vehicle, bool]:
    """
    Verilen normalized_plate için araç kaydını getirir.
    Kayıt yoksa yeni bir araç oluşturur.

    Aynı normalized_plate için iki kez araç oluşmaz:
    UNIQUE kısıtlaması + IntegrityError güvencesi sağlar.

    Parametreler:
        session (Session): Açık veritabanı oturumu.
        plate_text (str): Ham OCR metni.
        normalized_plate (str): Normalize edilmiş plaka.

    Döndürür:
        tuple[Vehicle, bool]: (araç_nesnesi, yeni_mi_oluşturuldu)
    """
    # Önce mevcut kaydı ara
    mevcut = (
        session.query(Vehicle)
        .filter_by(normalized_plate=normalized_plate)
        .first()
    )

    if mevcut is not None:
        # Araç zaten var: last_seen_at güncelle
        mevcut.last_seen_at = utc_now()
        mevcut.updated_at = utc_now()
        return mevcut, False

    # Yeni araç oluştur
    simdi = utc_now()
    yeni_arac = Vehicle(
        plate_text=plate_text,
        normalized_plate=normalized_plate,
        status=VehicleStatus.pending,
        first_seen_at=simdi,
        last_seen_at=simdi,
        created_at=simdi,
        updated_at=simdi,
    )

    try:
        session.add(yeni_arac)
        session.flush()  # ID atanır; commit dışarıda yapılır
        return yeni_arac, True
    except IntegrityError:
        # Eş zamanlı iki istek durumunda: UNIQUE ihlali → mevcut kaydı getir
        session.rollback()
        mevcut = (
            session.query(Vehicle)
            .filter_by(normalized_plate=normalized_plate)
            .first()
        )
        return mevcut, False


# ─────────────────────────────────────────────
# Geçiş Kararı
# ─────────────────────────────────────────────

def evaluate_access(vehicle: Vehicle) -> AccessDecision:
    """
    Aracın mevcut durumuna göre geçiş kararı üretir.

    Durum → Karar:
        pending  → wait_for_approval
        approved → allow
        rejected → deny
        inactive → deny

    Parametreler:
        vehicle (Vehicle): Araç kayıt nesnesi.

    Döndürür:
        AccessDecision: Verilen geçiş kararı.
    """
    if vehicle.status == VehicleStatus.approved:
        return AccessDecision.allow
    elif vehicle.status == VehicleStatus.pending:
        return AccessDecision.wait_for_approval
    else:
        # rejected veya inactive → deny
        return AccessDecision.deny


# ─────────────────────────────────────────────
# Erişim Kaydı Oluşturma
# ─────────────────────────────────────────────

def create_access_log(
    session: Session,
    vehicle: "Vehicle | None",
    plate_text: str,
    normalized_plate: str,
    direction: AccessDirection,
    decision: AccessDecision,
    ocr_confidence: float,
    source_camera: str | None = None,
    image_path: str | None = None,
    denial_reason: str | None = None,
) -> AccessLog:
    """
    Her geçiş denemesi için access_logs tablosuna kayıt ekler.

    ocr_confidence değeri 0.0–1.0 aralığında sınırlandırılır.

    Parametreler:
        session:          Açık veritabanı oturumu.
        vehicle:          Araç kayıt nesnesi (None ise bilinmeyen plaka).
        plate_text:       Ham OCR metni.
        normalized_plate: Normalize edilmiş plaka.
        direction:        Giriş / çıkış / bilinmiyor.
        decision:         Verilen geçiş kararı.
        ocr_confidence:   OCR güven değeri (0.0–1.0).
        source_camera:    Kamera kimliği (isteğe bağlı).
        image_path:       Kırpılmış görüntü dosya yolu (isteğe bağlı).
        denial_reason:    Ret gerekçesi (isteğe bağlı).

    Döndürür:
        AccessLog: Oluşturulan kayıt nesnesi.
    """
    # Güven değerini geçerli aralıkta tut
    guven = max(0.0, min(1.0, ocr_confidence))

    kayit = AccessLog(
        vehicle_id=vehicle.id if vehicle is not None else None,
        plate_text=plate_text,
        normalized_plate=normalized_plate,
        direction=direction,
        decision=decision,
        ocr_confidence=guven,
        detected_at=utc_now(),
        source_camera=source_camera,
        image_path=image_path,
        denial_reason=denial_reason,
    )
    session.add(kayit)
    return kayit


# ─────────────────────────────────────────────
# Cooldown Kontrolü
# ─────────────────────────────────────────────

def should_log(
    session: Session,
    normalized_plate: str,
    direction: AccessDirection,
    cooldown_seconds: int = 10,
) -> bool:
    """
    Aynı plaka + yön için son cooldown_seconds saniye içinde kayıt varsa
    False döner (yeni kayıt oluşturma).
    Yoksa True döner (yeni kayıt oluşturulabilir).

    Bu yöntem, araç birkaç saniye kamerada kalsa bile çok sayıda
    tekrar log kaydının oluşmasını önler.

    Parametreler:
        session:          Açık veritabanı oturumu.
        normalized_plate: Normalize edilmiş plaka.
        direction:        Giriş / çıkış / bilinmiyor.
        cooldown_seconds: Cooldown süresi (varsayılan: 10 saniye).

    Döndürür:
        bool: True → log yaz, False → cooldown içinde, atla.
    """
    son_kayit = (
        session.query(AccessLog)
        .filter_by(normalized_plate=normalized_plate, direction=direction)
        .order_by(AccessLog.detected_at.desc())
        .first()
    )

    if son_kayit is None:
        return True

    # SQLite datetime değerlerini timezone-naive saklayabilir.
    # Karşılaştırma için her iki tarafı da timezone bilgisinden arındırıyoruz.
    simdi = utc_now().replace(tzinfo=None)
    kayit_zamani = son_kayit.detected_at.replace(tzinfo=None) \
        if son_kayit.detected_at.tzinfo is not None \
        else son_kayit.detected_at

    sure = simdi - kayit_zamani
    return sure.total_seconds() > cooldown_seconds


# ─────────────────────────────────────────────
# Araç Durumu Güncelleme
# ─────────────────────────────────────────────

def approve_vehicle(
    session: Session,
    normalized_plate: str,
    approved_by: str,
) -> Vehicle:
    """
    Aracı onaylar: status=approved, approved_at ve approved_by alanlarını doldurur.

    Parametreler:
        session:          Açık veritabanı oturumu.
        normalized_plate: Normalize edilmiş plaka.
        approved_by:      Onaylayan operatör adı veya sistem kimliği.

    Döndürür:
        Vehicle: Güncellenen araç nesnesi.

    Hatalar:
        ValueError: Araç bulunamazsa üretilir.
    """
    arac = (
        session.query(Vehicle)
        .filter_by(normalized_plate=normalized_plate)
        .first()
    )
    if arac is None:
        raise ValueError(f"Araç bulunamadı: {normalized_plate!r}")

    simdi = utc_now()
    arac.status = VehicleStatus.approved
    arac.approved_at = simdi
    arac.approved_by = approved_by
    arac.updated_at = simdi
    return arac


def reject_vehicle(
    session: Session,
    normalized_plate: str,
    reason: str | None = None,
) -> Vehicle:
    """
    Aracı reddeder: status=rejected.

    Parametreler:
        session:          Açık veritabanı oturumu.
        normalized_plate: Normalize edilmiş plaka.
        reason:           Ret gerekçesi (isteğe bağlı).

    Döndürür:
        Vehicle: Güncellenen araç nesnesi.
    """
    arac = (
        session.query(Vehicle)
        .filter_by(normalized_plate=normalized_plate)
        .first()
    )
    if arac is None:
        raise ValueError(f"Araç bulunamadı: {normalized_plate!r}")

    arac.status = VehicleStatus.rejected
    arac.notes = reason
    arac.updated_at = utc_now()
    return arac


def deactivate_vehicle(
    session: Session,
    normalized_plate: str,
) -> Vehicle:
    """
    Aracın yetkisini kaldırır: status=inactive.

    Parametreler:
        session:          Açık veritabanı oturumu.
        normalized_plate: Normalize edilmiş plaka.

    Döndürür:
        Vehicle: Güncellenen araç nesnesi.
    """
    arac = (
        session.query(Vehicle)
        .filter_by(normalized_plate=normalized_plate)
        .first()
    )
    if arac is None:
        raise ValueError(f"Araç bulunamadı: {normalized_plate!r}")

    arac.status = VehicleStatus.inactive
    arac.updated_at = utc_now()
    return arac


def delete_vehicle(
    session: Session,
    normalized_plate: str,
) -> None:
    """
    normalized_plate ile belirtilen aracı veritabanından kalıcı olarak siler.
    Geçmiş erişim kayıtlarının (access_logs) silinmemesi için
    ilgili logların vehicle_id alanı None olarak güncellenir.

    Parametreler:
        session:          Açık veritabanı oturumu.
        normalized_plate: Silinecek aracın normalize edilmiş plakası.

    Hatalar:
        ValueError: Araç bulunamazsa üretilir.
    """
    arac = (
        session.query(Vehicle)
        .filter_by(normalized_plate=normalized_plate)
        .first()
    )
    if arac is None:
        raise ValueError(f"Araç bulunamadı: {normalized_plate!r}")

    # Geçmiş erişim kayıtlarının bozulmaması için vehicle_id bağını kopar
    session.query(AccessLog).filter_by(vehicle_id=arac.id).update({"vehicle_id": None})

    # Araç ana kaydını sil
    session.delete(arac)
