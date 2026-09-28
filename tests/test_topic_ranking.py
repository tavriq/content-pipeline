from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from ai_gateway import AIResult  # noqa: E402
from topic_ranking import (  # noqa: E402
    RankingSourceItem,
    TopicCandidate,
    TopicRankingError,
    TopicRankingStore,
    parse_topic_candidates,
    rank_topics,
)


class FakeGateway:
    def __init__(self, text):
        self.text = text
        self.kwargs = None

    def generate(self, **kwargs):
        self.kwargs = kwargs
        return AIResult(
            text=self.text,
            model="openai/gpt-5.6-luna",
            input_tokens=100,
            output_tokens=50,
            estimated_cost_rub=Decimal("0.0108"),
            monthly_spend_rub=Decimal("12.00"),
        )


class FakeCursor:
    def __init__(self, rows=None, rowcount=0, one=None):
        self.rows = rows or []
        self.rowcount = rowcount
        self.one = one
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params):
        self.executions.append((query, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.one


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


def source(source_id=11):
    return RankingSourceItem(
        id=source_id,
        source_name="PubMed",
        title="Thyroid guideline update",
        summary="Clinical Endocrinology",
        url=f"https://pubmed.ncbi.nlm.nih.gov/{source_id}/",
        published_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )


def response_payload(source_id=11):
    return json.dumps(
        {
            "topics": [
                {
                    "title": "Что изменилось в рекомендациях по щитовидной железе",
                    "summary": "Обновление рекомендаций без персональных назначений.",
                    "rationale": "Свежий первичный источник и интерес аудитории.",
                    "score": 92.5,
                    "source_item_ids": [source_id],
                }
            ]
        },
        ensure_ascii=False,
    )


class TopicParsingTest(unittest.TestCase):
    def test_parses_fenced_json_and_known_sources(self):
        candidates = parse_topic_candidates(
            f"```json\n{response_payload()}\n```",
            allowed_source_ids={11},
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_item_ids, (11,))
        self.assertEqual(candidates[0].score, 92.5)

    def test_rejects_hallucinated_source_id(self):
        with self.assertRaisesRegex(TopicRankingError, "неизвестные источники"):
            parse_topic_candidates(response_payload(999), allowed_source_ids={11})

    def test_rejects_duplicate_titles(self):
        payload = json.loads(response_payload())
        payload["topics"].append(dict(payload["topics"][0]))
        with self.assertRaisesRegex(TopicRankingError, "продублировала"):
            parse_topic_candidates(json.dumps(payload), allowed_source_ids={11})


class TopicRankingTest(unittest.TestCase):
    def test_sends_sources_to_fast_model_and_validates_result(self):
        gateway = FakeGateway(response_payload())
        result = rank_topics(gateway, [source()], max_topics=10)

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(gateway.kwargs["task"], "fast")
        self.assertEqual(gateway.kwargs["max_tokens"], 6000)
        self.assertIn('"id":11', gateway.kwargs["content"])


class TopicRankingStoreTest(unittest.TestCase):
    def test_acquires_single_process_advisory_lock(self):
        cursor = FakeCursor(one=(True,))
        store = TopicRankingStore(FakeConnection(cursor))
        self.assertTrue(store.acquire_lock())
        self.assertIn("pg_try_advisory_lock", cursor.executions[0][0])

    def test_loads_only_pending_items(self):
        cursor = FakeCursor(
            rows=[
                (
                    11,
                    "PubMed",
                    "Title",
                    "Journal",
                    "https://example.test/11",
                    datetime(2026, 8, 20, tzinfo=timezone.utc),
                )
            ]
        )
        items = TopicRankingStore(FakeConnection(cursor)).pending_items(25)
        self.assertEqual(items[0].id, 11)
        self.assertIn("ranked_at IS NULL", cursor.executions[0][0])
        self.assertIn("ranking_attempts <", cursor.executions[0][0])
        self.assertEqual(cursor.executions[0][1], (3, 25))

    def test_saves_topics_links_and_marks_sources(self):
        cursor = FakeCursor(rowcount=2)
        connection = FakeConnection(cursor)
        store = TopicRankingStore(connection, id_factory=lambda: "topic-fixed")
        candidate = TopicCandidate(
            title="Тема",
            summary="Кратко",
            rationale="Почему сейчас",
            score=90,
            source_item_ids=(11, 12),
        )
        saved = store.save([candidate], considered_source_ids=[11, 12])

        self.assertEqual(saved.topic_ids, ("topic-fixed",))
        self.assertEqual(saved.sources_marked, 2)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(len(cursor.executions), 4)
        self.assertEqual(cursor.executions[1][1][-1], "primary")
        self.assertEqual(cursor.executions[2][1][-1], "supporting")

    def test_rejects_topic_link_to_unconsidered_source(self):
        connection = FakeConnection(FakeCursor())
        candidate = TopicCandidate(
            title="Тема",
            summary="Кратко",
            rationale="Почему сейчас",
            score=90,
            source_item_ids=(999,),
        )
        with self.assertRaisesRegex(ValueError, "нерассмотренные"):
            TopicRankingStore(connection).save(
                [candidate], considered_source_ids=[11]
            )
        self.assertEqual(connection.commits, 0)

class RankingRetentionTest(unittest.TestCase):
    def test_unused_sources_get_another_chance(self):
        # Модель делает десяток тем из полусотни публикаций. Раньше все
        # пятьдесят помечались обработанными, и сорок исчезали навсегда.
        cursor = FakeCursor()
        store = TopicRankingStore(FakeConnection(cursor), id_factory=lambda: "topic-1")

        store.save(
            [
                TopicCandidate(
                    title="Тема",
                    summary="Кратко",
                    rationale="Почему",
                    score=80.0,
                    source_item_ids=(11,),
                )
            ],
            considered_source_ids=[11, 12, 13],
        )

        updates = [
            (query, params)
            for query, params in cursor.executions
            if "UPDATE source_items" in query
        ]
        self.assertEqual(updates[0][1], ([11],))
        self.assertIn("ranked_at = now()", updates[0][0])
        self.assertIn("ranking_attempts = ranking_attempts + 1", updates[1][0])
        self.assertEqual(updates[1][1], ([12, 13],))

class RankingPromptTest(unittest.TestCase):
    def test_prompt_ranks_by_reader_question_not_clinical_weight(self):
        # Первая шкала — «доказательность и применимость к практике» —
        # выдавала темы для конференции: фазу III препарата от ожирения,
        # антикоагулянты при ВТЭ, протеомные биомаркеры. Читатель блога не
        # врач, и повестку должен задавать его вопрос.
        gateway = FakeGateway('{"topics": []}')
        with self.assertRaises(TopicRankingError):
            rank_topics(gateway, [source(11)], max_topics=5)

        instructions = gateway.kwargs["instructions"]
        self.assertIn("уже задавала поиску", instructions)
        self.assertIn("разговор для коллег", instructions)
        # Препараты для снижения веса непроходны по красным линиям, какой бы
        # весомой ни была публикация.
        self.assertIn("Препараты для снижения веса", instructions)
        self.assertIn("кликабельности запрещено", instructions)
        self.assertIn("называть автора врачом", instructions.casefold())

    def test_candidate_takes_rubric_and_niche_from_model(self):
        payload = json.dumps(
            {
                "topics": [
                    {
                        "title": "ТТГ в норме, а самочувствие плохое",
                        "summary": "Кратко",
                        "rationale": "Почему",
                        "score": 80,
                        "source_item_ids": [11],
                        "rubric": "Р2",
                        "niche": "щитовидка",
                    }
                ]
            },
            ensure_ascii=False,
        )
        candidates = parse_topic_candidates(payload, allowed_source_ids={11})
        self.assertEqual(candidates[0].rubric, "Р2")
        self.assertEqual(candidates[0].niche, "щитовидка")

    def test_unknown_rubric_is_dropped_rather_than_stored(self):
        # Модель охотно придумывает свои коды рубрик. Чужой код в базе
        # означал бы подпись «Р9 · None» под темой у автора.
        payload = json.dumps(
            {
                "topics": [
                    {
                        "title": "Тема",
                        "summary": "Кратко",
                        "rationale": "Почему",
                        "score": 50,
                        "source_item_ids": [11],
                        "rubric": "Р9",
                        "niche": "кардиология",
                    }
                ]
            },
            ensure_ascii=False,
        )
        candidates = parse_topic_candidates(payload, allowed_source_ids={11})
        self.assertIsNone(candidates[0].rubric)
        self.assertIsNone(candidates[0].niche)


if __name__ == "__main__":
    unittest.main()
