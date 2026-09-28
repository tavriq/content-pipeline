from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from publication_scheduler import (  # noqa: E402
    MOSCOW,
    PublicationStore,
    future_slots,
    moscow_datetime,
    parse_author_date,
    parse_author_time,
)


class ScriptedCursor:
    def __init__(self, *, one_rows=None, all_rows=None, rowcount=1):
        self.one_rows = list(one_rows or [])
        self.all_rows = list(all_rows or [])
        self.rowcount = rowcount
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params=None):
        self.executions.append((query, params))

    def fetchone(self):
        return self.one_rows.pop(0)

    def fetchall(self):
        return self.all_rows.pop(0)


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class MoscowTimeTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 21, 15, 30, tzinfo=MOSCOW)

    def test_parses_short_date_in_moscow_year(self):
        self.assertEqual(parse_author_date("25.08", self.now), date(2026, 8, 25))

    def test_rejects_past_and_invalid_dates(self):
        with self.assertRaisesRegex(ValueError, "прошла"):
            parse_author_date("20.08", self.now)
        with self.assertRaisesRegex(ValueError, "формате"):
            parse_author_date("next monday", self.now)

    def test_parses_clock_and_filters_past_slots(self):
        self.assertEqual(parse_author_time("18:30"), time(18, 30))
        self.assertEqual(future_slots(self.now.date(), self.now), (time(18), time(21)))
        self.assertEqual(
            moscow_datetime(date(2026, 8, 22), time(10)).utcoffset().total_seconds(),
            3 * 3600,
        )


class PublicationStoreTest(unittest.TestCase):
    def test_schedules_only_approved_telegram_material(self):
        cursor = ScriptedCursor(
            one_rows=[("telegram", "approved", 3), ("Спокойный образовательный текст.",)]
        )
        connection = FakeConnection(cursor)
        store = PublicationStore(connection, id_factory=lambda: "pub-fixed")
        scheduled_at = datetime(2026, 8, 22, 10, tzinfo=MOSCOW)

        publication_id = store.schedule(
            material_id="material-1",
            scheduled_at=scheduled_at,
            now=datetime(2026, 8, 21, 10, tzinfo=MOSCOW),
        )

        self.assertEqual(publication_id, "pub-fixed")
        self.assertEqual(connection.commits, 1)
        insert = next(
            params
            for query, params in cursor.executions
            if "INSERT INTO publications" in query
        )
        # Версия фиксируется в момент планирования: правка после этого
        # больше не подменяет текст, уходящий в канал.
        self.assertEqual(
            insert, ("pub-fixed", "material-1", scheduled_at, "author", 3, None)
        )

    def test_rejects_reels_and_past_time(self):
        now = datetime(2026, 8, 21, 10, tzinfo=MOSCOW)
        with self.assertRaisesRegex(ValueError, "прошло"):
            PublicationStore(FakeConnection(ScriptedCursor())).schedule(
                material_id="material-1", scheduled_at=now, now=now
            )

        connection = FakeConnection(ScriptedCursor(one_rows=[("reels", "approved", 1)]))
        with self.assertRaisesRegex(ValueError, "Telegram-поста"):
            PublicationStore(connection).schedule(
                material_id="material-1",
                scheduled_at=datetime(2026, 8, 22, 10, tzinfo=MOSCOW),
                now=now,
            )
        self.assertEqual(connection.rollbacks, 1)


class PublicationGuardTest(unittest.TestCase):
    """Проверки стоят до очереди, а не в момент отправки."""

    def _store(self, body):
        cursor = ScriptedCursor(one_rows=[("telegram", "approved", 1), (body,)])
        connection = FakeConnection(cursor)
        return PublicationStore(connection, id_factory=lambda: "pub-fixed"), connection

    def test_refuses_text_breaking_red_lines(self):
        store, connection = self._store(
            "Схема простая: принимать по 50\nмкг утром, дальше по самочувствию."
        )
        with self.assertRaisesRegex(ValueError, "красные линии"):
            store.schedule(
                material_id="material-1",
                scheduled_at=datetime(2026, 8, 22, 10, tzinfo=MOSCOW),
                now=datetime(2026, 8, 21, 10, tzinfo=MOSCOW),
            )
        self.assertEqual(connection.rollbacks, 1)

    def test_refuses_text_longer_than_telegram_limit(self):
        store, _ = self._store("а" * 4200)
        with self.assertRaisesRegex(ValueError, "длиннее"):
            store.schedule(
                material_id="material-1",
                scheduled_at=datetime(2026, 8, 22, 10, tzinfo=MOSCOW),
                now=datetime(2026, 8, 21, 10, tzinfo=MOSCOW),
            )

    def test_claim_due_pins_version_and_checks_material_status(self):
        cursor = ScriptedCursor(one_rows=[None])
        PublicationStore(FakeConnection(cursor)).claim_due()
        query = cursor.executions[0][0]
        self.assertIn("mv.version = p.material_version", query)
        self.assertIn("m.status IN ('approved', 'scheduled')", query)
        self.assertNotIn("publishing", query.split("WHERE", 1)[1])

    def test_interrupted_send_is_held_for_a_human(self):
        # Между отправкой и записью message_id бот мог умереть. Повторять
        # вслепую нельзя: пост уже мог уйти в канал.
        cursor = ScriptedCursor(all_rows=[[("pub-1",), ("pub-2",)]])
        connection = FakeConnection(cursor)

        stuck = PublicationStore(connection).release_stuck()

        self.assertEqual(stuck, ("pub-1", "pub-2"))
        query = cursor.executions[0][0]
        self.assertIn("status = 'held'", query)
        self.assertEqual(connection.commits, 1)


if __name__ == "__main__":
    unittest.main()
