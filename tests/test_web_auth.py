"""Web panel login, session, CSRF ve sync API ayrimi testleri."""

import os
import re
import sys
import io
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from models import Base, Vehicle, VehicleStatus, utc_now
from web_app import app


class TestWebPanelAuth(unittest.TestCase):
    def setUp(self):
        self.auth_env = {
            "APP_ENV": "development",
            "WEB_ADMIN_USERNAME": "test_admin",
            "WEB_ADMIN_PASSWORD": "test_password_value",
            "WEB_SESSION_SECRET": "test_session_secret_value",
            "SYNC_API_TOKEN": "test_sync_token_value",
        }
        self.env_patcher = patch.dict(os.environ, self.auth_env)
        self.env_patcher.start()

        self.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine)

        self.session_patcher = patch("web_app.get_session")
        self.mock_get_session = self.session_patcher.start()

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

        self.mock_get_session.side_effect = mock_session
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.session_patcher.stop()
        Base.metadata.drop_all(bind=self.engine)
        self.engine.dispose()
        self.env_patcher.stop()

    def extract_csrf(self, response) -> str:
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        self.assertIsNotNone(match, "Response icinde CSRF token bulunamadi")
        return match.group(1)

    def login(self):
        login_page = self.client.get("/login")
        csrf_token = self.extract_csrf(login_page)
        response = self.client.post(
            "/login",
            data={
                "username": self.auth_env["WEB_ADMIN_USERNAME"],
                "password": self.auth_env["WEB_ADMIN_PASSWORD"],
                "csrf_token": csrf_token,
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        return response

    def create_pending_vehicle(self) -> int:
        now = utc_now()
        with self.Session() as session:
            vehicle = Vehicle(
                plate_text="34AUTH01",
                normalized_plate="34AUTH01",
                status=VehicleStatus.pending,
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(vehicle)
            session.commit()
            return vehicle.id

    def test_a_login_get_is_public(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Yönetici Girişi", response.text)

    def test_b_anonymous_dashboard_redirects_to_login(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_c_wrong_login_is_generic_and_rejected(self):
        csrf_token = self.extract_csrf(self.client.get("/login"))
        response = self.client.post(
            "/login",
            data={"username": "wrong", "password": "wrong", "csrf_token": csrf_token},
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("Kullanıcı adı veya parola hatalı", response.text)
        self.assertNotIn(self.auth_env["WEB_ADMIN_PASSWORD"], response.text)

    def test_d_e_correct_login_creates_session_and_opens_dashboard(self):
        response = self.login()
        cookie = response.headers.get("set-cookie", "").lower()
        self.assertIn("plate_admin_session=", cookie)
        dashboard = self.client.get("/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("Dashboard", dashboard.text)

    def test_f_logout_clears_session(self):
        self.login()
        dashboard = self.client.get("/")
        csrf_token = self.extract_csrf(dashboard)
        logout = self.client.post(
            "/logout",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(logout.status_code, 303)
        protected = self.client.get("/", follow_redirects=False)
        self.assertEqual(protected.status_code, 303)

    def test_g_anonymous_state_changes_are_blocked(self):
        for action in ("approve", "reject", "delete"):
            response = self.client.post(
                f"/vehicles/1/{action}",
                data={"csrf_token": "invalid"},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/login")

    def test_h_authenticated_missing_csrf_is_forbidden(self):
        self.login()
        response = self.client.post("/vehicles/1/approve")
        self.assertEqual(response.status_code, 403)

    def test_i_correct_csrf_allows_state_change(self):
        vehicle_id = self.create_pending_vehicle()
        self.login()
        dashboard = self.client.get("/")
        csrf_token = self.extract_csrf(dashboard)
        response = self.client.post(
            f"/vehicles/{vehicle_id}/approve",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        with self.Session() as session:
            vehicle = session.query(Vehicle).filter_by(id=vehicle_id).first()
            self.assertEqual(vehicle.status, VehicleStatus.approved)

    def test_j_health_is_public(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_private_api_documentation_is_not_public(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404)

    def test_k_l_sync_apis_remain_bearer_only_without_session(self):
        headers = {"Authorization": f"Bearer {self.auth_env['SYNC_API_TOKEN']}"}
        push = self.client.post("/api/sync/push", headers=headers, json={})
        approvals = self.client.post(
            "/api/sync/approvals",
            headers=headers,
            json={"vehicle_sync_ids": []},
        )
        self.assertEqual(push.status_code, 200)
        self.assertEqual(approvals.status_code, 200)

    def test_m_wrong_bearer_behavior_is_unchanged(self):
        response = self.client.post(
            "/api/sync/push",
            headers={"Authorization": "Bearer wrong"},
            json={},
        )
        self.assertEqual(response.status_code, 403)

    def test_n_production_cookie_security_flags(self):
        with patch.dict(os.environ, {"APP_ENV": "production"}):
            response = self.client.get("/login")
        cookie = response.headers.get("set-cookie", "").lower()
        self.assertIn("secure", cookie)
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertIn("max-age=28800", cookie)

    def test_o_missing_auth_config_fails_closed(self):
        self.client.cookies.clear()
        with patch.dict(os.environ, {"WEB_ADMIN_PASSWORD": ""}):
            protected = self.client.get("/", follow_redirects=False)
            login = self.client.get("/login", follow_redirects=False)
            health = self.client.get("/health")
        self.assertEqual(protected.status_code, 503)
        self.assertEqual(login.status_code, 503)
        self.assertEqual(health.status_code, 200)

    def test_p_secrets_do_not_leak_in_responses(self):
        csrf_token = self.extract_csrf(self.client.get("/login"))
        captured_output = io.StringIO()
        with redirect_stdout(captured_output):
            login = self.client.post(
                "/login",
                data={"username": "wrong", "password": "wrong", "csrf_token": csrf_token},
            )
            bearer = self.client.post(
                "/api/sync/push",
                headers={"Authorization": "Bearer wrong"},
                json={},
            )
        combined = login.text + bearer.text + captured_output.getvalue()
        for secret_value in (
            self.auth_env["WEB_ADMIN_PASSWORD"],
            self.auth_env["WEB_SESSION_SECRET"],
            self.auth_env["SYNC_API_TOKEN"],
        ):
            self.assertNotIn(secret_value, combined)


if __name__ == "__main__":
    unittest.main()
