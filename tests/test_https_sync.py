"""
test_https_sync.py — HTTPS API Senkronizasyonu Birim Testleri

Bu modül, Cloud Run FastAPI HTTPS senkronizasyon API uç noktalarını (push/approvals),
güvenlik token doğrulamasını ve yerel istemci işçilerini (cloud_sync.py, approval_sync.py,
sync_manager.py) donanım veya gerçek Google Cloud bağımlılığı olmadan test eder.
"""

import os
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# src klasörünü import yoluna ekle
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from web_app import app
from models import Base, Vehicle, AccessLog, VehicleStatus, AccessDirection, AccessDecision, utc_now
import cloud_sync
import approval_sync
import sync_manager


def create_test_vehicle(sync_id=None, plate="34ABC123", status=VehicleStatus.pending, **kwargs):
    now = utc_now()
    return Vehicle(
        sync_id=sync_id or str(uuid.uuid4()),
        plate_text=plate,
        normalized_plate=plate,
        status=status,
        first_seen_at=now,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
        **kwargs,
    )


class TestHTTPSSyncAPI(unittest.TestCase):
    """FastAPI HTTPS senkronizasyon sunucu uç noktaları testleri."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

        self.session_patcher = patch("web_app.get_session")
        self.mock_get_session = self.session_patcher.start()

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

        self.mock_get_session.side_effect = mock_sess
        self.client = TestClient(app)

    def tearDown(self):
        self.session_patcher.stop()
        Base.metadata.drop_all(bind=self.engine)

    def test_1_push_without_token_unauthorized(self):
        """Token olmadan yapılan push isteği HTTP 401 dönmelidir."""
        with patch.dict(os.environ, {"SYNC_API_TOKEN": "secret123"}):
            res = self.client.post("/api/sync/push", json={"vehicles": []})
            self.assertEqual(res.status_code, 401)

    def test_2_push_invalid_token_forbidden(self):
        """Hatalı token ile yapılan push isteği HTTP 403 dönmelidir."""
        with patch.dict(os.environ, {"SYNC_API_TOKEN": "secret123"}):
            res = self.client.post(
                "/api/sync/push",
                headers={"Authorization": "Bearer wrong_token"},
                json={"vehicles": []},
            )
            self.assertEqual(res.status_code, 403)

    def test_3_push_missing_server_token_service_unavailable(self):
        """Sunucuda SYNC_API_TOKEN yapılandırılmamışsa HTTP 503 dönmelidir."""
        with patch.dict(os.environ, {"SYNC_API_TOKEN": ""}):
            res = self.client.post(
                "/api/sync/push",
                headers={"Authorization": "Bearer secret123"},
                json={"vehicles": []},
            )
            self.assertEqual(res.status_code, 503)

    def test_4_push_valid_token_accepted(self):
        """Geçerli token ile yapılan push isteği kabul edilmeli (200 OK)."""
        with patch.dict(os.environ, {"SYNC_API_TOKEN": "secret123"}):
            res = self.client.post(
                "/api/sync/push",
                headers={"Authorization": "Bearer secret123"},
                json={"vehicles": [{"plate_text": "34ABC123", "normalized_plate": "34ABC123"}]},
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["vehicles"]["new"], 1)

    def test_5_duplicate_vehicle_push_idempotent(self):
        """Tekrarlanan araç push istekleri mükerrer kayıt oluşturmamalıdır."""
        with patch.dict(os.environ, {"SYNC_API_TOKEN": "secret123"}):
            v_sync_id = str(uuid.uuid4())
            payload = {
                "vehicles": [
                    {
                        "sync_id": v_sync_id,
                        "plate_text": "34ABC123",
                        "normalized_plate": "34ABC123",
                        "status": "pending",
                    }
                ]
            }
            res1 = self.client.post("/api/sync/push", headers={"Authorization": "Bearer secret123"}, json=payload)
            self.assertEqual(res1.json()["vehicles"]["new"], 1)

            res2 = self.client.post("/api/sync/push", headers={"Authorization": "Bearer secret123"}, json=payload)
            self.assertEqual(res2.json()["vehicles"]["new"], 0)
            self.assertEqual(res2.json()["vehicles"]["unchanged"], 1)

    def test_6_duplicate_access_log_push_idempotent(self):
        """Tekrarlanan AccessLog push istekleri mükerrer log oluşturmamalıdır."""
        with patch.dict(os.environ, {"SYNC_API_TOKEN": "secret123"}):
            v_sync_id = str(uuid.uuid4())
            l_sync_id = str(uuid.uuid4())
            payload = {
                "vehicles": [{"sync_id": v_sync_id, "plate_text": "34ABC123", "normalized_plate": "34ABC123"}],
                "access_logs": [
                    {
                        "sync_id": l_sync_id,
                        "vehicle_sync_id": v_sync_id,
                        "plate_text": "34ABC123",
                        "normalized_plate": "34ABC123",
                        "direction": "entry",
                        "decision": "allow",
                        "ocr_confidence": 0.9,
                        "source_camera": "cam_0",
                    }
                ],
            }
            res1 = self.client.post("/api/sync/push", headers={"Authorization": "Bearer secret123"}, json=payload)
            self.assertEqual(res1.json()["access_logs"]["new"], 1)

            res2 = self.client.post("/api/sync/push", headers={"Authorization": "Bearer secret123"}, json=payload)
            self.assertEqual(res2.json()["access_logs"]["new"], 0)
            self.assertEqual(res2.json()["access_logs"]["unchanged"], 1)

    def test_7_access_log_maps_vehicle_through_vehicle_sync_id(self):
        """AccessLog veritabanına eklenirken vehicle_sync_id üzerinden doğru araca bağlanmalıdır."""
        with patch.dict(os.environ, {"SYNC_API_TOKEN": "secret123"}):
            v_sync_id = str(uuid.uuid4())
            l_sync_id = str(uuid.uuid4())
            payload = {
                "vehicles": [{"sync_id": v_sync_id, "plate_text": "34MAP123", "normalized_plate": "34MAP123"}],
                "access_logs": [
                    {
                        "sync_id": l_sync_id,
                        "vehicle_sync_id": v_sync_id,
                        "plate_text": "34MAP123",
                        "normalized_plate": "34MAP123",
                        "direction": "entry",
                        "decision": "allow",
                        "ocr_confidence": 0.95,
                        "source_camera": "cam_0",
                    }
                ],
            }
            self.client.post("/api/sync/push", headers={"Authorization": "Bearer secret123"}, json=payload)

            with self.Session() as session:
                log = session.query(AccessLog).filter_by(sync_id=l_sync_id).first()
                self.assertIsNotNone(log)
                self.assertIsNotNone(log.vehicle)
                self.assertEqual(log.vehicle.sync_id, v_sync_id)

    def test_8_entry_and_exit_direction_preserved(self):
        """giriş (entry) ve çıkış (exit) yönleri aynen korunarak kaydedilmelidir."""
        with patch.dict(os.environ, {"SYNC_API_TOKEN": "secret123"}):
            v_sync_id = str(uuid.uuid4())
            l1_id = str(uuid.uuid4())
            l2_id = str(uuid.uuid4())
            payload = {
                "vehicles": [{"sync_id": v_sync_id, "plate_text": "34DIR123", "normalized_plate": "34DIR123"}],
                "access_logs": [
                    {
                        "sync_id": l1_id,
                        "vehicle_sync_id": v_sync_id,
                        "plate_text": "34DIR123",
                        "normalized_plate": "34DIR123",
                        "direction": "entry",
                        "decision": "allow",
                        "ocr_confidence": 0.9,
                        "source_camera": "cam0",
                    },
                    {
                        "sync_id": l2_id,
                        "vehicle_sync_id": v_sync_id,
                        "plate_text": "34DIR123",
                        "normalized_plate": "34DIR123",
                        "direction": "exit",
                        "decision": "allow",
                        "ocr_confidence": 0.9,
                        "source_camera": "cam0",
                    },
                ],
            }
            self.client.post("/api/sync/push", headers={"Authorization": "Bearer secret123"}, json=payload)

            with self.Session() as session:
                log1 = session.query(AccessLog).filter_by(sync_id=l1_id).first()
                log2 = session.query(AccessLog).filter_by(sync_id=l2_id).first()
                self.assertEqual(log1.direction, AccessDirection.entry)
                self.assertEqual(log2.direction, AccessDirection.exit)

    def test_9_approval_endpoint_returns_authorization_fields_only(self):
        """Yetki uç noktası yalnızca yetkilendirme alanlarını döndürmeli; operasyonel alanları ezmemelidir."""
        with patch.dict(os.environ, {"SYNC_API_TOKEN": "secret123"}):
            v_sync_id = str(uuid.uuid4())
            with self.Session() as session:
                v = create_test_vehicle(
                    sync_id=v_sync_id,
                    plate="34AUTH123",
                    status=VehicleStatus.approved,
                    approved_by="admin_user",
                    notes="Authorized vehicle",
                    approved_at=utc_now(),
                )
                session.add(v)
                session.commit()

            res = self.client.post(
                "/api/sync/approvals",
                headers={"Authorization": "Bearer secret123"},
                json={"vehicle_sync_ids": [v_sync_id]},
            )
            self.assertEqual(res.status_code, 200)
            items = res.json()
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item["sync_id"], v_sync_id)
            self.assertEqual(item["status"], "approved")
            self.assertEqual(item["approved_by"], "admin_user")
            self.assertEqual(item["notes"], "Authorized vehicle")
            self.assertIn("approved_at", item)
            self.assertNotIn("first_seen_at", item)

    def test_10_credentials_never_returned_in_error_responses(self):
        """Hata yanıtlarında secret token bilgisi sızdırılmamalıdır."""
        with patch.dict(os.environ, {"SYNC_API_TOKEN": "secret_token_val"}):
            res = self.client.post(
                "/api/sync/push",
                headers={"Authorization": "Bearer wrong_token_val"},
                json={},
            )
            self.assertNotIn("secret_token_val", res.text)
            self.assertNotIn("wrong_token_val", res.text)

    def test_21_offset_aware_and_naive_datetime_comparison_no_500(self):
        """
        DB'de tz-aware veya tz-naive datetime varken, gelen offset-aware ISO dizesi (+03:00 / Z)
        karşılaştırıldığında HTTP 500 fırlatmamalı, idempotent güncellenmelidir.
        """
        with patch.dict(os.environ, {"SYNC_API_TOKEN": "secret123"}):
            v_sync_id = str(uuid.uuid4())
            l_sync_id = str(uuid.uuid4())

            # 1. Aşama: İlk push (offset-aware +03:00 zaman damgaları)
            payload1 = {
                "vehicles": [
                    {
                        "sync_id": v_sync_id,
                        "plate_text": "34TZ123",
                        "normalized_plate": "34TZ123",
                        "status": "pending",
                        "last_seen_at": "2026-08-10T09:00:00+03:00",
                        "first_seen_at": "2026-08-10T09:00:00+03:00",
                        "created_at": "2026-08-10T09:00:00+03:00",
                        "updated_at": "2026-08-10T09:00:00+03:00",
                    }
                ],
                "access_logs": [
                    {
                        "sync_id": l_sync_id,
                        "vehicle_sync_id": v_sync_id,
                        "plate_text": "34TZ123",
                        "normalized_plate": "34TZ123",
                        "direction": "entry",
                        "decision": "allow",
                        "ocr_confidence": 0.95,
                        "source_camera": "cam0",
                        "detected_at": "2026-08-10T09:00:00+03:00",
                    }
                ],
            }
            res1 = self.client.post("/api/sync/push", headers={"Authorization": "Bearer secret123"}, json=payload1)
            self.assertEqual(res1.status_code, 200)
            self.assertEqual(res1.json()["vehicles"]["new"], 1)
            self.assertEqual(res1.json()["access_logs"]["new"], 1)

            # 2. Aşama: İkinci push - güncellenmiş daha ileri bir last_seen_at (UTC Z formatı)
            payload2 = {
                "vehicles": [
                    {
                        "sync_id": v_sync_id,
                        "plate_text": "34TZ123",
                        "normalized_plate": "34TZ123",
                        "status": "pending",
                        "last_seen_at": "2026-08-10T07:00:00Z",
                    }
                ],
                "access_logs": [
                    {
                        "sync_id": l_sync_id,
                        "vehicle_sync_id": v_sync_id,
                        "plate_text": "34TZ123",
                        "normalized_plate": "34TZ123",
                        "direction": "entry",
                        "decision": "allow",
                        "ocr_confidence": 0.95,
                        "source_camera": "cam0",
                        "detected_at": "2026-08-10T09:00:00+03:00",
                    }
                ],
            }
            res2 = self.client.post("/api/sync/push", headers={"Authorization": "Bearer secret123"}, json=payload2)
            self.assertEqual(res2.status_code, 200)
            self.assertEqual(res2.json()["vehicles"]["updated"], 1)
            self.assertEqual(res2.json()["access_logs"]["unchanged"], 1)

            # 3. Aşama: Üçüncü push - aynı kayıt tekrar gönderildiğinde idempotent kalmalı
            res3 = self.client.post("/api/sync/push", headers={"Authorization": "Bearer secret123"}, json=payload2)
            self.assertEqual(res3.status_code, 200)
            self.assertEqual(res3.json()["vehicles"]["unchanged"], 1)
            self.assertEqual(res3.json()["access_logs"]["unchanged"], 1)

    def test_22_db_aware_datetime_compared_with_naive_incoming_push(self):
        """DB'deki araç nesnesi tz-aware (PostgreSQL simülasyonu) iken push yapıldığında hata almamalı."""
        from datetime import datetime, timezone
        with patch.dict(os.environ, {"SYNC_API_TOKEN": "secret123"}):
            v_sync_id = str(uuid.uuid4())
            with self.Session() as session:
                now_aware = datetime.now(timezone.utc)
                v = Vehicle(
                    sync_id=v_sync_id,
                    plate_text="34AWARE1",
                    normalized_plate="34AWARE1",
                    status=VehicleStatus.pending,
                    first_seen_at=now_aware,
                    last_seen_at=now_aware,
                    created_at=now_aware,
                    updated_at=now_aware,
                )
                session.add(v)
                session.commit()

            # Naive ISO dizesi ile push gönder
            payload = {
                "vehicles": [
                    {
                        "sync_id": v_sync_id,
                        "plate_text": "34AWARE1",
                        "normalized_plate": "34AWARE1",
                        "last_seen_at": "2026-08-10T12:00:00",
                    }
                ]
            }
            res = self.client.post("/api/sync/push", headers={"Authorization": "Bearer secret123"}, json=payload)
            self.assertEqual(res.status_code, 200)


class TestLocalCloudSyncHTTPS(unittest.TestCase):
    """Yerel cloud_sync.py HTTPS istemci testleri."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(bind=self.engine)

    def test_11_missing_api_url_graceful(self):
        """CLOUD_SYNC_API_URL eksikse çökmeden güvenle atlanmalıdır."""
        success, stats = cloud_sync.run_sync(
            local_url="sqlite:///:memory:",
            sync_api_url="",
            sync_token="token123",
        )
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["new"], 0)

    def test_12_missing_token_graceful(self):
        """SYNC_API_TOKEN eksikse çökmeden güvenle atlanmalıdır."""
        success, stats = cloud_sync.run_sync(
            local_url="sqlite:///:memory:",
            sync_api_url="https://cloud.run.app",
            sync_token="",
        )
        self.assertTrue(success)
        self.assertEqual(stats["vehicles"]["new"], 0)

    @patch("cloud_sync.send_https_push")
    def test_13_http_network_failure_local_db_untouched(self, mock_push):
        """Ağ/HTTP hatasında yerel veriler kesinlikle bozulmamalı ve dokunulmamalıdır."""
        mock_push.return_value = (False, {})
        with self.Session() as session:
            v = create_test_vehicle(plate="34ERR123")
            session.add(v)
            session.commit()

        with patch("cloud_sync.create_engine", return_value=self.engine):
            success, stats = cloud_sync.run_sync(
                local_url="sqlite:///:memory:",
                sync_api_url="https://cloud.run.app",
                sync_token="token123",
            )
            self.assertFalse(success)

        with self.Session() as session:
            count = session.query(Vehicle).count()
            self.assertEqual(count, 1)

    @patch("cloud_sync.send_https_push")
    def test_14_successful_mocked_push_counters(self, mock_push):
        """Başarılı HTTP yanıtında özet sayaçları doğru güncellenmelidir."""
        mock_push.return_value = (
            True,
            {
                "vehicles": {"new": 1, "updated": 0, "unchanged": 0},
                "access_logs": {"new": 1, "updated": 0, "unchanged": 0},
            },
        )
        with self.Session() as session:
            v = create_test_vehicle(plate="34OK123")
            session.add(v)
            session.commit()

        with patch("cloud_sync.create_engine", return_value=self.engine):
            success, stats = cloud_sync.run_sync(
                local_url="sqlite:///:memory:",
                sync_api_url="https://cloud.run.app",
                sync_token="token123",
            )
            self.assertTrue(success)
            self.assertEqual(stats["vehicles"]["new"], 1)
            self.assertEqual(stats["access_logs"]["new"], 1)


class TestLocalApprovalSyncHTTPS(unittest.TestCase):
    """Yerel approval_sync.py HTTPS istemci testleri."""

    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        Base.metadata.drop_all(bind=self.engine)

    @patch("approval_sync.fetch_approvals_from_cloud_api")
    def test_15_approved_response_updates_local_pending_vehicle(self, mock_fetch):
        """Bulutta onaylanan araç yereldeki pending aracı güncellemeli."""
        v_sync_id = str(uuid.uuid4())
        with self.Session() as session:
            v = create_test_vehicle(sync_id=v_sync_id, plate="34APP123", status=VehicleStatus.pending)
            session.add(v)
            session.commit()

        mock_fetch.return_value = (
            True,
            [
                {
                    "sync_id": v_sync_id,
                    "status": "approved",
                    "approved_at": "2026-08-10T09:00:00",
                    "approved_by": "admin",
                    "notes": "Approved in web panel",
                }
            ],
        )

        with patch("approval_sync.create_engine", return_value=self.engine):
            success, stats = approval_sync.run_approval_sync(
                local_url="sqlite:///:memory:",
                sync_api_url="https://cloud.run.app",
                sync_token="token123",
            )
            self.assertTrue(success)
            self.assertEqual(stats["vehicles"]["updated"], 1)

        with self.Session() as session:
            v_updated = session.query(Vehicle).filter_by(sync_id=v_sync_id).first()
            self.assertEqual(v_updated.status, VehicleStatus.approved)
            self.assertEqual(v_updated.approved_by, "admin")
            self.assertEqual(v_updated.notes, "Approved in web panel")

    @patch("approval_sync.fetch_approvals_from_cloud_api")
    def test_16_rejected_response_updates_local_vehicle(self, mock_fetch):
        """Bulutta reddedilen araç yereldeki aracı güncellemeli."""
        v_sync_id = str(uuid.uuid4())
        with self.Session() as session:
            v = create_test_vehicle(sync_id=v_sync_id, plate="34REJ123", status=VehicleStatus.pending)
            session.add(v)
            session.commit()

        mock_fetch.return_value = (
            True,
            [
                {
                    "sync_id": v_sync_id,
                    "status": "rejected",
                    "approved_at": None,
                    "approved_by": "admin",
                    "notes": "Unauthorized vehicle",
                }
            ],
        )

        with patch("approval_sync.create_engine", return_value=self.engine):
            success, stats = approval_sync.run_approval_sync(
                local_url="sqlite:///:memory:",
                sync_api_url="https://cloud.run.app",
                sync_token="token123",
            )
            self.assertTrue(success)
            self.assertEqual(stats["vehicles"]["updated"], 1)

        with self.Session() as session:
            v_updated = session.query(Vehicle).filter_by(sync_id=v_sync_id).first()
            self.assertEqual(v_updated.status, VehicleStatus.rejected)

    @patch("approval_sync.fetch_approvals_from_cloud_api")
    def test_17_local_operational_fields_preserved(self, mock_fetch):
        """Yetki güncellenirken yerel plaka metni ve operasyonel alanlar korunmalıdır."""
        v_sync_id = str(uuid.uuid4())
        with self.Session() as session:
            v = create_test_vehicle(sync_id=v_sync_id, plate="34KEEP123", status=VehicleStatus.pending)
            session.add(v)
            session.commit()

        mock_fetch.return_value = (
            True,
            [
                {
                    "sync_id": v_sync_id,
                    "status": "approved",
                    "approved_at": "2026-08-10T09:00:00",
                    "approved_by": "admin",
                    "notes": "Approved",
                }
            ],
        )

        with patch("approval_sync.create_engine", return_value=self.engine):
            approval_sync.run_approval_sync(
                local_url="sqlite:///:memory:",
                sync_api_url="https://cloud.run.app",
                sync_token="token123",
            )

        with self.Session() as session:
            v_updated = session.query(Vehicle).filter_by(sync_id=v_sync_id).first()
            self.assertEqual(v_updated.plate_text, "34KEEP123")
            self.assertEqual(v_updated.normalized_plate, "34KEEP123")

    @patch("approval_sync.fetch_approvals_from_cloud_api")
    def test_18_access_logs_untouched(self, mock_fetch):
        """Yetki senkronizasyonu AccessLog kayıtlarına dokunmamalıdır."""
        v_sync_id = str(uuid.uuid4())
        l_sync_id = str(uuid.uuid4())
        with self.Session() as session:
            v = create_test_vehicle(sync_id=v_sync_id, plate="34LOG123", status=VehicleStatus.pending)
            session.add(v)
            session.flush()
            log = AccessLog(
                sync_id=l_sync_id,
                vehicle_id=v.id,
                plate_text="34LOG123",
                normalized_plate="34LOG123",
                direction=AccessDirection.entry,
                decision=AccessDecision.wait_for_approval,
                ocr_confidence=0.9,
                source_camera="cam0",
                detected_at=utc_now(),
            )
            session.add(log)
            session.commit()

        mock_fetch.return_value = (
            True,
            [{"sync_id": v_sync_id, "status": "approved", "approved_at": None, "approved_by": "admin", "notes": ""}],
        )

        with patch("approval_sync.create_engine", return_value=self.engine):
            approval_sync.run_approval_sync(
                local_url="sqlite:///:memory:",
                sync_api_url="https://cloud.run.app",
                sync_token="token123",
            )

        with self.Session() as session:
            log_after = session.query(AccessLog).filter_by(sync_id=l_sync_id).first()
            self.assertIsNotNone(log_after)
            self.assertEqual(log_after.direction, AccessDirection.entry)

    @patch("approval_sync.fetch_approvals_from_cloud_api")
    def test_19_http_failure_does_not_damage_local_data(self, mock_fetch):
        """HTTP yetki isteği başarısız olduğunda yerel veriler korunmalıdır."""
        v_sync_id = str(uuid.uuid4())
        with self.Session() as session:
            v = create_test_vehicle(sync_id=v_sync_id, plate="34FAIL123", status=VehicleStatus.pending)
            session.add(v)
            session.commit()

        mock_fetch.return_value = (False, [])

        with patch("approval_sync.create_engine", return_value=self.engine):
            success, stats = approval_sync.run_approval_sync(
                local_url="sqlite:///:memory:",
                sync_api_url="https://cloud.run.app",
                sync_token="token123",
            )
            self.assertFalse(success)

        with self.Session() as session:
            v_after = session.query(Vehicle).filter_by(sync_id=v_sync_id).first()
            self.assertEqual(v_after.status, VehicleStatus.pending)


class TestSyncManagerHTTPS(unittest.TestCase):
    """sync_manager.py supervisor HTTPS modül testleri."""

    @patch("sync_manager.run_sync")
    @patch("sync_manager.run_approval_sync")
    def test_20_both_https_workers_continue_independently(self, mock_approval, mock_cloud):
        """Bir worker hata verse bile diğer worker çalışmaya devam etmelidir."""
        mock_cloud.side_effect = Exception("Simulated Push Error")
        mock_approval.return_value = (True, {})

        mgr = sync_manager.SyncManager(
            cloud_interval=1,
            approval_interval=1,
            sync_api_url="https://cloud.run.app",
            sync_token="token123",
        )
        mgr.start()
        time.sleep(1.2)
        mgr.stop()

        self.assertTrue(mock_cloud.called)
        self.assertTrue(mock_approval.called)


if __name__ == "__main__":
    unittest.main()
