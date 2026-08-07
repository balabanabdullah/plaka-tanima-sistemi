"""
tests/test_database_config.py — Veritabanı Konfigürasyonu Ortam Değişkeni Testleri

Bu test paketi, DATABASE_URL çevre değişkeninin:
1. Tanımlanmadığında yerel SQLite varsayılanını kullandığını,
2. Boş dize ("" veya "   ") verildiğinde varsayılan yerel SQLite kullandığını,
3. Özel bir PostgreSQL URL'si verildiğinde URL seçiminin doğru yapıldığını ve
   şifre bilgilerinin loglama için maskelendiğini (sanitized) doğrular.
"""

import os
import unittest
from pathlib import Path
import sys

# src klasörünü sys.path'e ekle
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_PATH = PROJECT_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from database import get_database_url, get_db_url, sanitize_db_url, DEFAULT_DB_PATH


class TestDatabaseConfig(unittest.TestCase):
    """
    Veritabanı URL çözümleme ve maskeleme testleri.
    """

    def setUp(self):
        # Orijinal DATABASE_URL değerini sakla
        self.original_env = os.environ.get("DATABASE_URL")

    def tearDown(self):
        # Orijinal DATABASE_URL değerini geri yükle
        if self.original_env is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self.original_env

    def test_1_no_database_url_uses_local_sqlite(self):
        """
        DATABASE_URL çevre değişkeni tanımlanmadığında yerel SQLite seçilmeli.
        """
        os.environ.pop("DATABASE_URL", None)
        url = get_database_url()
        expected = f"sqlite:///{DEFAULT_DB_PATH}"
        self.assertEqual(url, expected)
        self.assertTrue(url.startswith("sqlite:///"))
        self.assertIn("plate_system.db", url)

    def test_2_empty_database_url_uses_local_sqlite(self):
        """
        DATABASE_URL boş dize ("" veya "   ") olduğunda yerel SQLite seçilmeli.
        """
        os.environ["DATABASE_URL"] = ""
        self.assertEqual(get_database_url(), f"sqlite:///{DEFAULT_DB_PATH}")

        os.environ["DATABASE_URL"] = "   "
        self.assertEqual(get_database_url(), f"sqlite:///{DEFAULT_DB_PATH}")

    def test_3_custom_postgresql_url_selection_and_sanitization(self):
        """
        Özel bir PostgreSQL URL'si verildiğinde get_database_url() URL'yi aynen döndürmeli,
        şifre maskelenmeli ve motor (engine) oluşturulmadığı için psycopg2 yükleme hatası yaşanmamalıdır.
        """
        sample_pg_url = "postgresql://user:pass@localhost:5432/testdb"
        os.environ["DATABASE_URL"] = sample_pg_url

        url = get_database_url()
        self.assertEqual(url, "postgresql://user:pass@localhost:5432/testdb")

        sanitized = sanitize_db_url(url)
        self.assertNotIn("pass", sanitized)
        self.assertIn("user:***", sanitized)
        self.assertIn("@localhost:5432/testdb", sanitized)


    def test_4_cloud_sql_unix_socket_url_format(self):
        """
        Cloud SQL Unix socket formatında (postgresql+psycopg2://...) URL verildiğinde
        get_database_url() URL'yi aynen döndürmeli ve şifre maskelenmelidir.
        """
        cloud_sql_url = "postgresql+psycopg2://plaka_user:my_secret_password@/plaka_db?host=/cloudsql/plaka-tanima-abdullah-2026:europe-west1:plaka-postgres"
        os.environ["DATABASE_URL"] = cloud_sql_url

        url = get_database_url()
        self.assertEqual(url, cloud_sql_url)

        sanitized = sanitize_db_url(url)
        self.assertNotIn("my_secret_password", sanitized)
        self.assertIn("plaka_user:***", sanitized)
        self.assertIn("plaka-postgres", sanitized)


if __name__ == "__main__":
    unittest.main()


