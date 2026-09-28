#!/usr/bin/env python3
"""Подключение к PostgreSQL и последовательное применение миграций."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "db" / "migrations"


class DatabaseConfigurationError(ValueError):
    """DATABASE_URL отсутствует или имеет небезопасный формат."""


@dataclass(frozen=True)
class DatabaseSettings:
    url: str
    connect_timeout_seconds: int = 10

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "DatabaseSettings":
        url = values.get("DATABASE_URL", "").strip()
        if not url:
            raise DatabaseConfigurationError("Не настроен DATABASE_URL")
        parsed = urlsplit(url)
        if parsed.scheme not in ("postgresql", "postgres") or not parsed.hostname:
            raise DatabaseConfigurationError("DATABASE_URL должен быть PostgreSQL URL")

        raw_timeout = values.get("DATABASE_CONNECT_TIMEOUT_SECONDS", "10").strip()
        try:
            timeout = int(raw_timeout)
        except ValueError as error:
            raise DatabaseConfigurationError(
                "DATABASE_CONNECT_TIMEOUT_SECONDS должен быть целым числом"
            ) from error
        if timeout <= 0:
            raise DatabaseConfigurationError(
                "DATABASE_CONNECT_TIMEOUT_SECONDS должен быть больше нуля"
            )
        return cls(url=url, connect_timeout_seconds=timeout)

    @classmethod
    def from_environment(cls) -> "DatabaseSettings":
        return cls.from_mapping(os.environ)

    def safe_url(self) -> str:
        """URL для логов: имя хоста видно, пароль — никогда."""
        parsed = urlsplit(self.url)
        username = parsed.username or ""
        user_part = f"{username}:***@" if username else ""
        host = parsed.hostname or ""
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, user_part + host, parsed.path, parsed.query, ""))


def migration_files(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    return sorted(path for path in directory.glob("*.sql") if path.is_file())


def connect(settings: DatabaseSettings):
    try:
        import psycopg
    except ImportError as error:
        raise RuntimeError("Установите зависимости: pip install -r requirements.txt") from error
    return psycopg.connect(
        settings.url,
        connect_timeout=settings.connect_timeout_seconds,
        application_name="blog-content-pipeline",
    )


def apply_migrations(connection: Any, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Применить ещё не выполненные SQL-файлы в одной транзакции каждый."""
    applied: list[str] = []
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    connection.commit()

    for path in migration_files(directory):
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (path.name,))
            if cursor.fetchone():
                continue
            try:
                cursor.execute(path.read_text(encoding="utf-8"))
                cursor.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            applied.append(path.name)
    return applied


def check_connection(connection: Any) -> tuple[int, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1, current_database()")
        row = cursor.fetchone()
    return int(row[0]), str(row[1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "migrate"))
    args = parser.parse_args()

    settings = DatabaseSettings.from_environment()
    with connect(settings) as connection:
        if args.command == "check":
            marker, database_name = check_connection(connection)
            print(f"PostgreSQL доступен: {marker == 1}; база: {database_name}")
            return 0
        applied = apply_migrations(connection)
        if applied:
            print("Применены миграции: " + ", ".join(applied))
        else:
            print("Новых миграций нет")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
