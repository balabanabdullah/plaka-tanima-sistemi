"""
test_event_and_direction.py — Olay Dedeplikasyonu, Otomatik Yön ve Varlık Durumu Testleri

Donanımsız (Hardware-Free) birim testleri:
1. is_probable_same_plate() ile OCR varyasyon bastırma ve muhafazakar eşleşme
2. get_next_auto_direction() ve get_vehicle_presence_state() ile otomatik yön döngüsü (ENTRY -> EXIT -> ENTRY)
3. WAIT_FOR_APPROVAL ve DENY durumlarının yön/varlık değiştirmemesi
4. DB türevli durum yönetiminin uygulama yeniden başlatılmasını simüle etmesi
5. Web paneli presence haritası ve sayaç hesaplamaları
"""

import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
from unittest.mock import patch

# Proje dizinlerini import yoluna ekle
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (
    Base, Vehicle, AccessLog,
    VehicleStatus, AccessDirection, AccessDecision,
    utc_now,
)
from plate_service import (
    normalize_plate,
    get_or_create_vehicle,
    evaluate_access,
    create_access_log,
    approve_vehicle,
    reject_vehicle,
    is_probable_same_plate,
    levenshtein_distance,
    get_last_successful_allow_log,
    get_vehicle_presence_state,
    get_next_auto_direction,
    get_vehicles_presence_map,
)
from ocr_reader import process_plate_access


from sqlalchemy.pool import StaticPool

class TestEventAndDirection(unittest.TestCase):
    """
    Olay tekilleştirme, otomatik yön ve varlık durumu birim testleri.
    """

    def setUp(self):
        """Her test için geçici bellekte SQLite veritabanı hazırlar."""
        self.engine = create_engine(
            "sqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            echo=False
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        """Test sonrası veritabanı bağlantılarını kapatır."""
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()

    # ─────────────────────────────────────────────────────────────
    # 1. OCR Varyasyon / Muhafazakar Eşleşme Testleri
    # ─────────────────────────────────────────────────────────────

    def test_is_probable_same_plate_exact_and_variants(self):
        """Birebir aynı ve belirgin gürültü varyasyonlarını doğru algılamalı."""
        # Exact match
        self.assertTrue(is_probable_same_plate("34FRK052", "34FRK052"))
        self.assertTrue(is_probable_same_plate("34 ABC 123", "34ABC123"))

        # OCR gürültü varyasyonları (aynı fiziksel plaka)
        self.assertTrue(is_probable_same_plate("82TR37", "TR82TR37"))
        self.assertTrue(is_probable_same_plate("34FRK052", "BLFRK052"))
        self.assertTrue(is_probable_same_plate("34FRK052", "BFRK052"))

    def test_is_probable_same_plate_conservative_false_positives(self):
        """Gerçekten farklı plakaları birleştirmemeli (muhafazakar yaklaşım)."""
        # Farklı plakalar (yanlış pozitif üretilmemeli)
        self.assertFalse(is_probable_same_plate("34ABC123", "06XYZ999"))
        self.assertFalse(is_probable_same_plate("34FRK052", "34XYZ999"))

        # Uzunluk farkı > 2 olanlar
        self.assertFalse(is_probable_same_plate("34A", "34ABC12345"))

        # Boş / geçersiz girdi
        self.assertFalse(is_probable_same_plate("", "34ABC123"))
        self.assertFalse(is_probable_same_plate("34ABC123", None))

    # ─────────────────────────────────────────────────────────────
    # 2. Otomatik Yön (AUTO Direction) ve Varlık Durumu Testleri
    # ─────────────────────────────────────────────────────────────

    def test_auto_direction_initial_state_is_entry(self):
        """Hiç ALLOW kaydı olmayan yeni araç için bir sonraki yön ENTRY olmalıdır."""
        with self.Session() as session:
            vehicle, _ = get_or_create_vehicle(session, "34ABC123", "34ABC123")
            session.commit()

            presence = get_vehicle_presence_state(session, vehicle)
            next_dir = get_next_auto_direction(session, vehicle)

            self.assertEqual(presence, "outside")
            self.assertEqual(next_dir, AccessDirection.entry)

    def test_auto_direction_alternating_cycle(self):
        """
        Giriş (ENTRY) sonrası varlık 'inside', sonraki yön EXIT olmalı.
        Çıkış (EXIT) sonrası varlık 'outside', sonraki yön ENTRY olmalı.
        (ENTRY -> EXIT -> ENTRY -> EXIT)
        """
        with self.Session() as session:
            vehicle, _ = get_or_create_vehicle(session, "34FRK052", "34FRK052")
            approve_vehicle(session, "34FRK052", approved_by="admin")
            session.commit()

            # 1. Aşama: İlk onaylı geçiş -> ENTRY
            dir1 = get_next_auto_direction(session, vehicle)
            self.assertEqual(dir1, AccessDirection.entry)

            create_access_log(
                session=session,
                vehicle=vehicle,
                plate_text="34FRK052",
                normalized_plate="34FRK052",
                direction=dir1,
                decision=AccessDecision.allow,
                ocr_confidence=0.95,
            )
            session.commit()

            # Şimdi içeride (inside) olmalı, sonraki yön EXIT olmalı
            self.assertEqual(get_vehicle_presence_state(session, vehicle), "inside")
            dir2 = get_next_auto_direction(session, vehicle)
            self.assertEqual(dir2, AccessDirection.exit)

            # 2. Aşama: Çıkış geçişi -> EXIT
            create_access_log(
                session=session,
                vehicle=vehicle,
                plate_text="34FRK052",
                normalized_plate="34FRK052",
                direction=dir2,
                decision=AccessDecision.allow,
                ocr_confidence=0.92,
            )
            session.commit()

            # Şimdi dışarıda (outside) olmalı, sonraki yön ENTRY olmalı
            self.assertEqual(get_vehicle_presence_state(session, vehicle), "outside")
            dir3 = get_next_auto_direction(session, vehicle)
            self.assertEqual(dir3, AccessDirection.entry)

    def test_pending_and_deny_logs_do_not_affect_presence(self):
        """
        WAIT_FOR_APPROVAL veya DENY kararları aracın varlık durumunu veya
        otomatik yönünü KESİNLİKLE değiştirmemelidir.
        """
        with self.Session() as session:
            vehicle, _ = get_or_create_vehicle(session, "06ANK99", "06ANK99")
            session.commit()

            # Pending araç için WAIT_FOR_APPROVAL logu oluşturuluyor
            create_access_log(
                session=session,
                vehicle=vehicle,
                plate_text="06ANK99",
                normalized_plate="06ANK99",
                direction=AccessDirection.entry,
                decision=AccessDecision.wait_for_approval,
                ocr_confidence=0.85,
            )
            session.commit()

            # Varlık durumu hala outside kalmalı, yön hala entry olmalı!
            self.assertEqual(get_vehicle_presence_state(session, vehicle), "outside")
            self.assertEqual(get_next_auto_direction(session, vehicle), AccessDirection.entry)

            # Araç onaylanıp ENTRY yapılıyor
            approve_vehicle(session, "06ANK99", approved_by="admin")
            create_access_log(
                session=session,
                vehicle=vehicle,
                plate_text="06ANK99",
                normalized_plate="06ANK99",
                direction=AccessDirection.entry,
                decision=AccessDecision.allow,
                ocr_confidence=0.90,
            )
            session.commit()

            # Şu an 'inside'
            self.assertEqual(get_vehicle_presence_state(session, vehicle), "inside")

            # Sonradan başarısız/ret veya ikincil denemeler gelse bile 'inside' korunmalı
            create_access_log(
                session=session,
                vehicle=vehicle,
                plate_text="06ANK99",
                normalized_plate="06ANK99",
                direction=AccessDirection.entry,
                decision=AccessDecision.deny,
                ocr_confidence=0.80,
            )
            session.commit()

            # Varlık hala 'inside' olmalıdır!
            self.assertEqual(get_vehicle_presence_state(session, vehicle), "inside")
            self.assertEqual(get_next_auto_direction(session, vehicle), AccessDirection.exit)

    def test_presence_state_survives_process_restart(self):
        """Varlık durumu bellek değişkenine değil DB sorgusuna dayandığı için yeniden başlatmada korunmalıdır."""
        # Session 1: Araç girişi kaydet
        with self.Session() as session1:
            v, _ = get_or_create_vehicle(session1, "34RESTART", "34RESTART")
            approve_vehicle(session1, "34RESTART", approved_by="admin")
            create_access_log(
                session=session1,
                vehicle=v,
                plate_text="34RESTART",
                normalized_plate="34RESTART",
                direction=AccessDirection.entry,
                decision=AccessDecision.allow,
                ocr_confidence=0.99,
            )
            session1.commit()

        # Session 2: Süreç yeniden başlatıldı (Tamamen yeni DB oturumu)
        with self.Session() as session2:
            v_fresh = session2.query(Vehicle).filter_by(normalized_plate="34RESTART").first()
            self.assertIsNotNone(v_fresh)
            self.assertEqual(get_vehicle_presence_state(session2, v_fresh), "inside")
            self.assertEqual(get_next_auto_direction(session2, v_fresh), AccessDirection.exit)

    # ─────────────────────────────────────────────────────────────
    # 3. process_plate_access ve Runtime AUTO Yön Testi
    # ─────────────────────────────────────────────────────────────

    @patch("ocr_reader.get_session")
    def test_process_plate_access_with_auto_direction(self, mock_get_session):
        """process_plate_access 'auto' parametresi aldığında DB'ye çözümlenmiş AccessDirection kaydetmelidir."""
        @contextmanager
        def mock_session():
            session = self.Session()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        mock_get_session.side_effect = mock_session

        res1 = process_plate_access("34AUTO1", 0.90, "auto", "cam_0")
        self.assertIsNotNone(res1)
        norm1, status1, decision1, log1, resolved_dir1 = res1

        self.assertEqual(norm1, "34AUTO1")
        self.assertEqual(status1, "pending")
        self.assertEqual(decision1, "wait_for_approval")
        self.assertTrue(log1)
        self.assertEqual(resolved_dir1, AccessDirection.entry)

        # Veritabanında saklanan log yönünün string "auto" değil, "entry" olduğunu doğrula!
        with self.Session() as session:
            last_log = session.query(AccessLog).order_by(AccessLog.id.desc()).first()
            self.assertIsNotNone(last_log)
            self.assertEqual(last_log.direction, AccessDirection.entry)

    # ─────────────────────────────────────────────────────────────
    # 4. Web Paneli Harita (get_vehicles_presence_map) Testi
    # ─────────────────────────────────────────────────────────────

    def test_get_vehicles_presence_map(self):
        """Araç listesi için varlık haritası doğru etiketlerle üretilmelidir."""
        with self.Session() as session:
            v1, _ = get_or_create_vehicle(session, "34INSIDE", "34INSIDE")
            approve_vehicle(session, "34INSIDE", approved_by="admin")
            create_access_log(session, v1, "34INSIDE", "34INSIDE", AccessDirection.entry, AccessDecision.allow, 0.9)

            v2, _ = get_or_create_vehicle(session, "34OUTSIDE", "34OUTSIDE")
            approve_vehicle(session, "34OUTSIDE", approved_by="admin")
            create_access_log(session, v2, "34OUTSIDE", "34OUTSIDE", AccessDirection.entry, AccessDecision.allow, 0.9)
            create_access_log(session, v2, "34OUTSIDE", "34OUTSIDE", AccessDirection.exit, AccessDecision.allow, 0.9)

            v3, _ = get_or_create_vehicle(session, "34NOALLOW", "34NOALLOW")

            session.commit()

            pmap = get_vehicles_presence_map(session, [v1, v2, v3])

            self.assertEqual(pmap[v1.id]["presence_state"], "inside")
            self.assertEqual(pmap[v1.id]["presence_label"], "İçeride")
            self.assertEqual(pmap[v1.id]["last_movement_label"], "Giriş")

            self.assertEqual(pmap[v2.id]["presence_state"], "outside")
            self.assertEqual(pmap[v2.id]["presence_label"], "Dışarıda")
            self.assertEqual(pmap[v2.id]["last_movement_label"], "Çıkış")

            self.assertEqual(pmap[v3.id]["presence_state"], "outside")
            self.assertEqual(pmap[v3.id]["presence_label"], "Dışarıda")
            self.assertEqual(pmap[v3.id]["last_movement_label"], "-")

    # ─────────────────────────────────────────────────────────────
    # 5. Kritik Regresyon Testleri (A - G)
    # ─────────────────────────────────────────────────────────────

    def test_regression_a_same_approved_plate_continuously_visible(self):
        """TEST A: Aynı onaylı plaka sürekli kamerada kaldığında yalnızca 1 AccessLog ve 1 OPEN üretilmelidir."""
        with self.Session() as session:
            get_or_create_vehicle(session, "82TR37", "82TR37")
            approve_vehicle(session, "82TR37", approved_by="admin")
            session.commit()

        sim = OCREventSimulator(self.Session, direction="auto")

        # 100 kare boyunca aynı araç görünür
        t = 100.0
        for _ in range(100):
            sim.step_frame(t, plate_detected=True, ocr_text="82TR37")
            t += 0.05

        with self.Session() as session:
            logs = session.query(AccessLog).all()
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].direction, AccessDirection.entry)
            self.assertEqual(logs[0].decision, AccessDecision.allow)

        self.assertEqual(sim.open_commands_sent, 1)

    def test_regression_b_no_entry_exit_toggle_during_same_event(self):
        """TEST B: Sürekli görünür araç AUTO modunda Asla görünürlük bitmeden ENTRY'den EXIT'e geçmemelidir."""
        with self.Session() as session:
            get_or_create_vehicle(session, "82TR37", "82TR37")
            approve_vehicle(session, "82TR37", approved_by="admin")
            session.commit()

        sim = OCREventSimulator(self.Session, direction="auto")

        # 100 kare aynı araç
        t = 100.0
        for _ in range(100):
            sim.step_frame(t, plate_detected=True, ocr_text="82TR37")
            t += 0.05

        with self.Session() as session:
            logs = session.query(AccessLog).all()
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].direction, AccessDirection.entry)
            # Kesinlikle EXIT oluşmamış olmalı!

    def test_regression_c_short_absence_under_timeout(self):
        """TEST C: Zaman aşımından kısa süreli kaybolma (<3.0s) yeni fiziksel olay oluşturmamalıdır."""
        with self.Session() as session:
            get_or_create_vehicle(session, "82TR37", "82TR37")
            approve_vehicle(session, "82TR37", approved_by="admin")
            session.commit()

        sim = OCREventSimulator(self.Session, direction="auto")

        t = 100.0
        # 15 kare araç var
        for _ in range(15):
            sim.step_frame(t, plate_detected=True, ocr_text="82TR37")
            t += 0.05

        # 1.5 saniye kayboldu (< 3.0s)
        t += 1.5

        # 15 kare daha araç var
        for _ in range(15):
            sim.step_frame(t, plate_detected=True, ocr_text="82TR37")
            t += 0.05

        with self.Session() as session:
            logs = session.query(AccessLog).all()
            self.assertEqual(len(logs), 1)

    def test_regression_d_absence_over_timeout_triggers_new_exit_event(self):
        """TEST D: Zaman aşımını aşan kaybolma (>3.0s) sonrasında dönen araç EXIT olarak yeni olay başlatmalıdır."""
        with self.Session() as session:
            get_or_create_vehicle(session, "82TR37", "82TR37")
            approve_vehicle(session, "82TR37", approved_by="admin")
            session.commit()

        sim = OCREventSimulator(self.Session, direction="auto")

        t = 100.0
        # Olay 1 (ENTRY)
        for _ in range(15):
            sim.step_frame(t, plate_detected=True, ocr_text="82TR37")
            t += 0.05

        # 4.0 saniye yok oldu (> 3.0s event reset)
        t += 4.0

        # Olay 2 (EXIT)
        for _ in range(15):
            sim.step_frame(t, plate_detected=True, ocr_text="82TR37")
            t += 0.05

        with self.Session() as session:
            logs = session.query(AccessLog).order_by(AccessLog.id.asc()).all()
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0].direction, AccessDirection.entry)
            self.assertEqual(logs[1].direction, AccessDirection.exit)

        self.assertEqual(sim.open_commands_sent, 2)

    def test_regression_e_leave_over_timeout_returns_to_entry(self):
        """TEST E: Olay 1 (ENTRY) -> Ayrılma (>3s) -> Olay 2 (EXIT) -> Ayrılma (>3s) -> Olay 3 (ENTRY) döngüsü."""
        with self.Session() as session:
            get_or_create_vehicle(session, "82TR37", "82TR37")
            approve_vehicle(session, "82TR37", approved_by="admin")
            session.commit()

        sim = OCREventSimulator(self.Session, direction="auto")

        t = 100.0
        # Olay 1 (ENTRY)
        for _ in range(15):
            sim.step_frame(t, plate_detected=True, ocr_text="82TR37")
            t += 0.05

        t += 12.0  # Reset ve DB cooldown süresi (10s) geçmesi için 12s ayrılma

        # Olay 2 (EXIT)
        for _ in range(15):
            sim.step_frame(t, plate_detected=True, ocr_text="82TR37")
            t += 0.05

        t += 12.0  # Reset ve DB cooldown

        # Olay 3 (ENTRY)
        for _ in range(15):
            sim.step_frame(t, plate_detected=True, ocr_text="82TR37")
            t += 0.05

        with self.Session() as session:
            logs = session.query(AccessLog).order_by(AccessLog.id.asc()).all()
            self.assertEqual(len(logs), 3)
            self.assertEqual(logs[0].direction, AccessDirection.entry)
            self.assertEqual(logs[1].direction, AccessDirection.exit)
            self.assertEqual(logs[2].direction, AccessDirection.entry)

    def test_regression_f_pending_continuous_presence_single_log(self):
        """TEST F: Onaysız (PENDING) araç sürekli görünür kaldığında yalnızca 1 WAIT_FOR_APPROVAL logu oluşturmalıdır."""
        sim = OCREventSimulator(self.Session, direction="auto")

        t = 100.0
        for _ in range(100):
            sim.step_frame(t, plate_detected=True, ocr_text="34PENDING1")
            t += 0.05

        with self.Session() as session:
            logs = session.query(AccessLog).all()
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].decision, AccessDecision.wait_for_approval)

        self.assertEqual(sim.open_commands_sent, 0)

    def test_regression_g_pending_approved_remotely_while_visible_no_open_until_return(self):
        """TEST G: Görünürken uzaktan onaylanan araç aynı olayda bariyer açmamalı; ancak ayrılıp döndüğünde açmalıdır."""
        sim = OCREventSimulator(self.Session, direction="auto")

        t = 100.0
        # 1. 15 kare PENDING araç görünür -> wait_for_approval loglanır
        for _ in range(15):
            sim.step_frame(t, plate_detected=True, ocr_text="34PENDING2")
            t += 0.05

        self.assertEqual(sim.open_commands_sent, 0)

        # 2. Araç tam o sırada kameranın önündeyken web panelden onaylanır
        with self.Session() as session:
            approve_vehicle(session, "34PENDING2", approved_by="admin")
            session.commit()

        # 3. Araç 50 kare daha kameranın önünde durmaya devam eder
        for _ in range(50):
            sim.step_frame(t, plate_detected=True, ocr_text="34PENDING2")
            t += 0.05

        # AYNI FİZİKSEL OLAYDA BARİYER AÇILMAMALIDIR!
        self.assertEqual(sim.open_commands_sent, 0)

        # 4. Araç 12.0 saniye ayrılır (>3.0s reset & DB cooldown) ve geri döner
        t += 12.0
        for _ in range(15):
            sim.step_frame(t, plate_detected=True, ocr_text="34PENDING2")
            t += 0.05

        # Artık yeni olay başladığı için ALLOW kararı bariyeri açmalıdır!
        self.assertEqual(sim.open_commands_sent, 1)


class OCREventSimulator:
    """Birim testlerde fiziksel olay döngüsünü taklit eden test yardımcısı."""

    def __init__(self, session_factory, camera_source="cam_0", direction="auto"):
        from collections import deque
        from plate_service import is_probable_same_plate
        from ocr_reader import FINAL_HISTORY_SIZE, PLATE_ABSENCE_RESET_SECONDS, process_plate_access, get_final_plate_candidate

        self.Session = session_factory
        self.camera_source = camera_source
        self.direction = direction

        self.active_event_text = ""
        self.active_event_processed = False
        self.active_event_direction = None
        self.active_event_decision = ""
        self.active_event_status = ""
        self.barrier_opened_for_event = False
        self.son_veritabani_metin = ""
        self.son_db_durum = ""
        self.son_db_karar = ""
        self.last_plate_detection_time = 0.0
        self.open_commands_sent = 0

        self.final_ocr_history = deque(maxlen=FINAL_HISTORY_SIZE)

    def step_frame(self, current_time: float, plate_detected: bool, ocr_text: str = None, ocr_confidence: float = 0.90):
        from plate_service import is_probable_same_plate
        from ocr_reader import PLATE_ABSENCE_RESET_SECONDS, process_plate_access, get_final_plate_candidate
        from unittest.mock import patch

        # 1. Yokluk Kontrolü (Nihai tespitten ÖNCE çalışır)
        if self.last_plate_detection_time > 0 and (current_time - self.last_plate_detection_time >= PLATE_ABSENCE_RESET_SECONDS) and (self.son_veritabani_metin or self.active_event_text):
            self.active_event_text = ""
            self.active_event_processed = False
            self.active_event_direction = None
            self.active_event_decision = ""
            self.active_event_status = ""
            self.barrier_opened_for_event = False
            self.son_veritabani_metin = ""
            self.son_db_durum = ""
            self.son_db_karar = ""
            self.final_ocr_history.clear()

        # 2. Yeni Plaka Algılandıysa
        if plate_detected and ocr_text:
            self.last_plate_detection_time = current_time

            if self.active_event_text:
                if is_probable_same_plate(ocr_text, self.active_event_text):
                    pass

            self.final_ocr_history.append((ocr_text, ocr_confidence))
            final_aday = get_final_plate_candidate(self.final_ocr_history)

            if final_aday is not None:
                final_metin, final_guven, tekrar_sayisi = final_aday

                if not self.active_event_text:
                    self.active_event_text = final_metin

                if is_probable_same_plate(final_metin, self.active_event_text):
                    if not self.active_event_processed:
                        with patch("ocr_reader.get_session") as mock_get_session, patch("ocr_reader.should_log", return_value=True):
                            from contextlib import contextmanager
                            @contextmanager
                            def mock_sess():
                                s = self.Session()
                                try:
                                    yield s
                                    s.commit()
                                except Exception:
                                    s.rollback()
                                    raise
                                finally:
                                    s.close()

                            mock_get_session.side_effect = mock_sess

                            db_sonuc = process_plate_access(
                                plate_text=final_metin,
                                ocr_confidence=final_guven,
                                direction=self.direction,
                                source_camera=self.camera_source,
                            )
                            if db_sonuc is not None:
                                norm, durum, karar, log_created, resolved_dir = db_sonuc
                                self.active_event_processed = True
                                self.active_event_text = norm
                                self.active_event_direction = resolved_dir
                                self.active_event_decision = karar.upper()
                                self.active_event_status = durum.upper()
                                self.son_veritabani_metin = norm
                                self.son_db_durum = self.active_event_status
                                self.son_db_karar = self.active_event_decision

                                if karar == "allow" and log_created and not self.barrier_opened_for_event:
                                    self.open_commands_sent += 1
                                    self.barrier_opened_for_event = True


def get_session_direct(session_factory):
    """Birim testlerde custom session döner."""
    from contextlib import contextmanager
    @contextmanager
    def _inner():
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    return _inner()


if __name__ == "__main__":
    unittest.main()
