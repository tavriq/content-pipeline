"""Реестр тем: разбор файла, ниши и порядок выдачи."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from topic_registry import (  # noqa: E402
    RegistryStore,
    load_registry,
    parse_registry,
    registry_score,
    sync_from_environment,
    underweighted_niches,
)

SAMPLE = """
# Реестр тем. Статусы: свободна | в_работе | использована

- id: 6
  блок: питание_и_вес
  рубрика: Р5
  тема: Почему вес — это не про силу воли
  статус: свободна
  где_использована:

- id: 24
  блок: щитовидная_железа
  рубрика: Р2
  тема: ТТГ в норме, а самочувствие плохое
  статус: в_работе
  где_использована: tg/queue/t-004.md

- id: 30
  блок: личное
  рубрика: Р4
  тема: Почему я выбрала эндокринологию
  статус: использована
  где_использована: reels/approved/r-009.md
"""


class ParseRegistryTest(unittest.TestCase):
    def test_reads_fields_without_yaml_library(self):
        topics = parse_registry(SAMPLE)
        self.assertEqual([topic.registry_id for topic in topics], [6, 24, 30])
        self.assertEqual(topics[0].title, "Почему вес — это не про силу воли")
        self.assertEqual(topics[1].rubric, "Р2")
        self.assertEqual(topics[2].used_in, "reels/approved/r-009.md")

    def test_block_maps_to_niche_of_section_four(self):
        topics = parse_registry(SAMPLE)
        self.assertEqual(topics[0].niche, "питание")
        self.assertEqual(topics[1].niche, "щитовидка")
        self.assertEqual(topics[2].niche, "личное")

    def test_only_free_topics_are_available(self):
        topics = parse_registry(SAMPLE)
        self.assertEqual([topic.is_free for topic in topics], [True, False, False])

    def test_rubric_name_is_human_readable(self):
        topics = parse_registry(SAMPLE)
        self.assertEqual(topics[1].rubric_name, "что означает показатель")

    def test_real_registry_is_fully_marked_up(self):
        # Тема без рубрики придёт автору с пустой подписью, а тема без
        # известного блока свалится в чекапы и перекосит доли.
        topics = load_registry()
        self.assertGreaterEqual(len(topics), 33)
        without_rubric = [topic.registry_id for topic in topics if not topic.rubric]
        self.assertEqual(without_rubric, [])
        known = {"питание", "женские_гормоны", "чекапы", "щитовидка", "личное"}
        self.assertTrue(all(topic.niche in known for topic in topics))


class NicheOrderTest(unittest.TestCase):
    def test_empty_history_follows_plan_shares(self):
        self.assertEqual(underweighted_niches({}, 0)[0], "питание")
        self.assertEqual(underweighted_niches({}, 0)[-1], "личное")

    def test_overused_niche_falls_to_the_end(self):
        order = underweighted_niches({"питание": 8, "щитовидка": 1}, 10)
        self.assertEqual(order[-1], "питание")
        self.assertIn(order[0], {"женские_гормоны", "чекапы"})

    def test_score_prefers_niches_with_larger_share(self):
        topics = parse_registry(SAMPLE)
        food = next(topic for topic in topics if topic.niche == "питание")
        personal = next(topic for topic in topics if topic.niche == "личное")
        self.assertGreater(registry_score(food), registry_score(personal))


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.executions = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executions.append((" ".join(query.split()), params))

    def fetchall(self):
        return self.rows.pop(0) if self.rows else []


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class SyncTest(unittest.TestCase):
    def test_used_topics_are_closed_and_free_ones_offered(self):
        cursor = FakeCursor()
        connection = FakeConnection(cursor)
        available, closed = RegistryStore(connection).sync(parse_registry(SAMPLE))

        self.assertEqual(available, 1)  # только «свободна»
        self.assertEqual(closed, 1)  # «использована»
        self.assertEqual(connection.commits, 1)

    def test_topic_in_work_is_stored_but_not_offered(self):
        # Материал по ней уже пишется: показывать её в дайджесте — значит
        # предложить автору сделать то же самое второй раз.
        cursor = FakeCursor()
        RegistryStore(FakeConnection(cursor)).sync(parse_registry(SAMPLE))
        inserts = [
            params for query, params in cursor.executions if "INSERT INTO topics" in query
        ]
        statuses = {params[0]: params[4] for params in inserts}
        self.assertEqual(statuses["registry-006"], "candidate")
        self.assertEqual(statuses["registry-024"], "shown")


class ConnectSignatureTest(unittest.TestCase):
    def test_sync_passes_settings_to_connect(self):
        # Живьём это выглядело так: бот стартовал, ловил TypeError в фоне и
        # работал дальше на одном потоке PubMed, а реестр молча не приезжал.
        import inspect

        import database

        source = inspect.getsource(sync_from_environment)
        self.assertIn("DatabaseSettings.from_environment()", source)
        required = [
            name
            for name, parameter in inspect.signature(database.connect).parameters.items()
            if parameter.default is inspect.Parameter.empty
        ]
        self.assertEqual(required, ["settings"])


if __name__ == "__main__":
    unittest.main()
