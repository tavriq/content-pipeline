from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from database import (  # noqa: E402
    DatabaseConfigurationError,
    DatabaseSettings,
    migration_files,
)


class DatabaseSettingsTest(unittest.TestCase):
    def test_requires_database_url(self):
        with self.assertRaisesRegex(DatabaseConfigurationError, "DATABASE_URL"):
            DatabaseSettings.from_mapping({})

    def test_rejects_non_postgres_url(self):
        with self.assertRaisesRegex(DatabaseConfigurationError, "PostgreSQL"):
            DatabaseSettings.from_mapping({"DATABASE_URL": "sqlite:///tmp/blog.db"})

    def test_redacts_password(self):
        settings = DatabaseSettings.from_mapping(
            {"DATABASE_URL": "postgresql://blog:very-secret@postgres:5432/blog_content"}
        )
        self.assertEqual(
            settings.safe_url(),
            "postgresql://blog:***@postgres:5432/blog_content",
        )
        self.assertNotIn("very-secret", settings.safe_url())


class MigrationDiscoveryTest(unittest.TestCase):
    def test_returns_only_sorted_sql_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "002_second.sql").write_text("SELECT 2", encoding="utf-8")
            (folder / "001_first.sql").write_text("SELECT 1", encoding="utf-8")
            (folder / "README.md").write_text("ignore", encoding="utf-8")
            self.assertEqual(
                [path.name for path in migration_files(folder)],
                ["001_first.sql", "002_second.sql"],
            )


if __name__ == "__main__":
    unittest.main()
