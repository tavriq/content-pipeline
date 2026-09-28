from __future__ import annotations

import sys
import unittest
import unittest.mock
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from ai_gateway import AIResult  # noqa: E402
import content_workflow  # noqa: E402
from content_workflow import (  # noqa: E402
    ContentStore,
    DigestTopic,
    TopicContext,
    generation_prompt,
    revise_material_from_environment,
    split_check_note,
)


class ScriptedCursor:
    def __init__(self, *, all_rows=None, one_rows=None, rowcount=0):
        self.all_rows = list(all_rows or [])
        self.one_rows = list(one_rows or [])
        self.rowcount = rowcount
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params):
        self.executions.append((query, params))

    def fetchall(self):
        return self.all_rows.pop(0)

    def fetchone(self):
        return self.one_rows.pop(0)


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


def ai_result(text="Готовый материал"):
    return AIResult(
        text=text,
        model="openai/gpt-writer",
        input_tokens=100,
        output_tokens=200,
        estimated_cost_rub=Decimal("0.10"),
    )


class ContentStoreTest(unittest.TestCase):
    def test_digest_mixes_registry_and_fresh_topics(self):
        registry = [
            (
                f"registry-{index}", f"Тема реестра {index}", "Кратко", "Почему",
                60.0, "registry", "Р1", niche,
            )
            for index, niche in enumerate(
                ("питание", "щитовидка", "женские_гормоны", "чекапы", "личное"), start=1
            )
        ]
        fresh = [
            (
                f"topic-{index}", f"Свежая тема {index}", "Кратко", "Почему",
                float(100 - index), "pubmed", None, None,
            )
            for index in range(1, 6)
        ]
        # Первый запрос — реестр, второй — свежее, третий (счётчик ниш) пуст.
        cursor = ScriptedCursor(all_rows=[registry, fresh, []])
        connection = FakeConnection(cursor)
        store = ContentStore(connection, digest_id_factory=lambda: "digest-fixed")

        page = store.create_digest()

        self.assertEqual(page.digest_id, "digest-fixed")
        self.assertEqual([topic.rank for topic in page.topics], [1, 2, 3, 4, 5])
        origins = [topic.origin for topic in page.topics]
        # Обе повестки на первой странице, а не реестр целиком и свежее — на второй.
        self.assertIn("registry", origins)
        self.assertIn("pubmed", origins)
        self.assertTrue(page.has_next)
        self.assertEqual(connection.commits, 1)

    def test_digest_starts_from_underweighted_niche(self):
        registry = [
            ("registry-1", "Про питание", "Кратко", "Почему", 60.0, "registry", "Р5", "питание"),
            ("registry-2", "Про щитовидку", "Кратко", "Почему", 60.0, "registry", "Р2", "щитовидка"),
        ]
        # За последние материалы питание уже вышло четыре раза, щитовидка — ни разу.
        cursor = ScriptedCursor(all_rows=[registry, [], [("питание", 4)]])
        store = ContentStore(FakeConnection(cursor), digest_id_factory=lambda: "digest-fixed")

        page = store.create_digest()

        self.assertEqual(page.topics[0].niche, "щитовидка")

    def test_registry_topic_label_names_rubric_and_origin(self):
        topic = DigestTopic(
            "registry-24", "ТТГ в норме, а самочувствие плохое", "Кратко", "Почему",
            60.0, 1, 1, "registry", "Р2", "щитовидка",
        )
        label = topic.rubric_label
        self.assertIn("Р2", label)
        self.assertIn("что означает показатель", label)
        self.assertIn("щитовидка", label)
        self.assertIn("из плана блога", label)

    def test_loads_latest_digest_when_no_candidates_exist(self):
        topic_row = ("topic-1", "Тема", "Кратко", "Почему", 90, 1, 1, "pubmed", None, None)
        cursor = ScriptedCursor(
            all_rows=[[topic_row]],
            one_rows=[("digest-latest",), None],
        )
        page = ContentStore(FakeConnection(cursor)).latest_digest_page()

        self.assertEqual(page.digest_id, "digest-latest")
        self.assertEqual(page.topics[0].id, "topic-1")
        self.assertFalse(page.has_next)
        self.assertIn("ORDER BY db.created_at DESC", cursor.executions[0][0])

    def test_archives_only_stale_unselected_topics(self):
        cursor = ScriptedCursor(rowcount=3)
        connection = FakeConnection(cursor)

        archived = ContentStore(connection).archive_stale_topics(7)

        self.assertEqual(archived, 3)
        self.assertEqual(connection.commits, 1)
        query, params = cursor.executions[0]
        self.assertIn("status = 'shown'", query)
        self.assertIn("NOT EXISTS", query)
        self.assertEqual(params, (7,))

    def test_marks_delivered_digest_as_sent(self):
        cursor = ScriptedCursor(rowcount=1)
        connection = FakeConnection(cursor)

        changed = ContentStore(connection).mark_digest_sent("digest-1")

        self.assertTrue(changed)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(cursor.executions[0][1], ("digest-1",))

    def test_saves_new_material_and_version_in_one_commit(self):
        cursor = ScriptedCursor(one_rows=[None, None])
        connection = FakeConnection(cursor)
        store = ContentStore(
            connection, material_id_factory=lambda: "material-fixed"
        )

        material_id, version, cancelled = store.save_material_version(
            topic_id="topic-1",
            format_name="telegram",
            body="Текст",
            ai_result=ai_result(),
        )

        self.assertEqual((material_id, version), ("material-fixed", 1))
        self.assertFalse(cancelled)
        self.assertEqual(connection.commits, 1)
        version_insert = next(
            params
            for query, params in cursor.executions
            if "INSERT INTO material_versions" in query
        )
        self.assertEqual(version_insert[0:3], ("material-fixed", 1, "Текст"))


    def test_revision_takes_material_off_the_publication_queue(self):
        # Правка — это новая, ещё не одобренная версия. Пока публикация
        # оставалась в очереди, в канал в назначенное время уходил
        # неодобренный текст.
        cursor = ScriptedCursor(one_rows=[("material-1", 2), ("pub-1",)])
        connection = FakeConnection(cursor)

        _, version, cancelled = ContentStore(connection).save_material_version(
            topic_id="topic-1",
            format_name="telegram",
            body="Исправленный текст",
            ai_result=ai_result(),
            edit_mode="text",
            edit_instruction="убери упоминание бренда",
        )

        self.assertEqual(version, 3)
        self.assertTrue(cancelled)
        cancel_query = next(
            query
            for query, _ in cursor.executions
            if "UPDATE publications" in query
        )
        self.assertIn("status = 'cancelled'", cancel_query)


class PromptTest(unittest.TestCase):
    def setUp(self):
        self.topic = TopicContext(
            id="topic-1",
            title="Новая тема",
            summary="Краткое описание",
            rationale="Свежая публикация",
            sources=(
                {
                    "source": "PubMed",
                    "title": "Primary paper",
                    "summary": "Evidence",
                    "url": "https://example.test/paper",
                },
            ),
        )

    def test_prompt_has_medical_boundaries_and_sources(self):
        instructions, content, max_tokens = generation_prompt(self.topic, "telegram")

        self.assertIn("диагноз или предположение о диагнозе", instructions)
        self.assertIn("называть автора врачом", instructions.casefold())
        self.assertIn("https://example.test/paper", content)
        self.assertEqual(max_tokens, 3500)
        # Роль редактора живёт в core/system-prompt.md в одном экземпляре.
        self.assertIn("TONE OF VOICE", instructions)
        self.assertIn("САМОПРОВЕРКА", instructions)

    def test_rejects_empty_revision_before_external_calls(self):
        with self.assertRaisesRegex(ValueError, "пустой"):
            revise_material_from_environment("material-1", "   ")

    def test_script_prompt_demands_every_field_of_the_spec(self):
        # Прежний промпт просил «хук, текст озвучки и подсказку по футажам».
        # По такому сценарию нельзя снимать: нет ни визуала по секундам, ни
        # подписи, ни пометки, нужна ли автор в кадре.
        instructions, _, _ = generation_prompt(self.topic, "reels")
        for field in (
            "ТЕКСТ НА ЭКРАНЕ В ХУКЕ",
            "ВИЗУАЛ",
            "ТЕКСТ ПОДПИСИ",
            "ХЕШТЕГИ",
            "ДИСКЛЕЙМЕР",
        ):
            self.assertIn(field, instructions)
        self.assertIn("в кадре", instructions)

    def test_prompt_carries_rubric_and_niche(self):
        topic = TopicContext(
            id="registry-024",
            title="ТТГ в норме, а самочувствие плохое",
            summary="Кратко",
            rationale="Тема плана блога",
            sources=(),
            origin="registry",
            rubric="Р2",
            niche="щитовидка",
        )
        instructions, content, _ = generation_prompt(topic, "telegram")
        self.assertIn("что означает показатель", instructions)
        self.assertIn("щитовидка", content)
        # Рубрикатор нужен целиком: без него модель не знает, чем Р2
        # отличается от Р5.
        self.assertIn("РУБРИКАТОР", instructions)

    def test_author_words_outrank_sources(self):
        topic = TopicContext(
            id="registry-027",
            title="Почему заболевания щитовидки чаще у женщин",
            summary="Кратко",
            rationale="Тема плана блога",
            sources=(
                {"source": "PubMed", "title": "Paper", "summary": "Evidence", "url": "u"},
            ),
            origin="registry",
            author_note="На цикле разбирали, что пациентки путают АИТ и гипотиреоз.",
        )
        _, content, _ = generation_prompt(topic, "telegram")
        note_at = content.index("На цикле разбирали")
        sources_at = content.index("Источники:")
        self.assertLess(note_at, sources_at)
        self.assertIn("приоритетный материал", content)

    def test_topic_without_sources_forbids_any_number(self):
        # Тема из плана блога может не найти доказательной базы. Материал всё
        # равно нужен, но без единой цифры: иначе модель придумает и процент,
        # и год исследования.
        topic = TopicContext(
            id="registry-020",
            title="Почему диагноза «гормональный сбой» не существует",
            summary="Кратко",
            rationale="Тема плана блога",
            sources=(),
            origin="registry",
        )
        _, content, _ = generation_prompt(topic, "telegram")
        self.assertIn("ИСТОЧНИКОВ НЕТ", content)
        self.assertIn("Ни одной цифры", content)

class AttachEvidenceTest(unittest.TestCase):
    """Поиск доказательной базы — вспомогательный шаг, а не условие работы."""

    def setUp(self):
        self.topic = TopicContext(
            id="registry-014",
            title="Витамин D — что известно, а что придумали",
            summary="Кратко",
            rationale="Тема плана блога",
            sources=(),
            origin="registry",
            rubric="Р1",
            niche="чекапы",
        )

    def test_failed_query_leaves_topic_without_sources_instead_of_raising(self):
        # Живьём: быстрая модель вернула пустой ответ, и вся генерация
        # свалилась. Автор увидела «не смог подготовить материал» на теме,
        # которую можно было написать и без источников — без единой цифры.
        from ai_gateway import AIResponseError

        store = unittest.mock.Mock()
        store.evidence_query.return_value = ""
        with unittest.mock.patch(
            "content_workflow.build_evidence_query",
            side_effect=AIResponseError("Модель вернула пустой ответ"),
        ):
            with self.assertLogs("content_workflow", level="WARNING") as logs:
                result = content_workflow.attach_evidence(store, self.topic)

        # Молчать тоже нельзя: пустая доказательная база должна быть видна.
        self.assertIn("registry-014", logs.output[0])
        self.assertIs(result, self.topic)
        self.assertEqual(result.sources, ())

    def test_topic_with_sources_makes_no_requests_at_all(self):
        topic = TopicContext(
            id="topic-1",
            title="Тема",
            summary="Кратко",
            rationale="Почему",
            sources=({"source": "PubMed", "title": "t", "summary": "s", "url": "u"},),
        )
        store = unittest.mock.Mock()
        self.assertIs(content_workflow.attach_evidence(store, topic), topic)
        store.evidence_query.assert_not_called()

    def test_found_sources_are_saved_and_linked_to_the_topic(self):
        # Единственный путь, который раньше не проверялся ничем: он ходит и в
        # PubMed, и в базу. Ошибку в имени метода хранилища поймал прод.
        import types

        found = [
            types.SimpleNamespace(external_id="40123456", title="Guideline"),
            types.SimpleNamespace(external_id="40222333", title="Meta-analysis"),
        ]
        store = unittest.mock.Mock()
        store.evidence_query.return_value = "vitamin D AND deficiency"
        enriched = TopicContext(
            id="registry-014",
            title=self.topic.title,
            summary="Кратко",
            rationale="Тема плана блога",
            sources=({"source": "PubMed", "title": "Guideline", "summary": "s", "url": "u"},),
            origin="registry",
        )
        store.topic_context.return_value = enriched

        source_store = unittest.mock.Mock()
        with unittest.mock.patch.dict(
            "sys.modules", {"source_store": source_store}
        ), unittest.mock.patch(
            "collect_sources.search_evidence_from_environment", return_value=found
        ):
            result = content_workflow.attach_evidence(store, self.topic)

        source_store.SourceStore.return_value.save_items.assert_called_once()
        store.link_sources.assert_called_once_with(
            "registry-014", ["40123456", "40222333"]
        )
        self.assertEqual(len(result.sources), 1)

    def test_query_budget_leaves_room_for_the_answer(self):
        # У рассуждающей модели лимит считает и рассуждение, и ответ.
        # Двухсот токенов не хватало: всё уходило в рассуждение.
        import inspect

        source = inspect.getsource(content_workflow.build_evidence_query)
        limit = int(source.split("max_tokens=")[1].split(",")[0])
        self.assertGreaterEqual(limit, 1000)


class TopicRecycleTest(unittest.TestCase):
    def test_unused_registry_topic_returns_to_the_queue(self):
        # Реестр не архивируется — тема ждёт своей очереди месяцами. Но
        # показанная и не взятая в работу зависала в «показана» навсегда, и
        # за десяток дайджестов от тридцати трёх тем не осталось бы ничего.
        cursor = ScriptedCursor(rowcount=2)
        ContentStore(FakeConnection(cursor)).archive_stale_topics(7)

        updates = [query for query, _ in cursor.executions if "UPDATE topics" in query]
        self.assertEqual(len(updates), 2)
        self.assertIn("status = 'archived'", updates[0])
        self.assertIn("origin = 'pubmed'", updates[0])
        self.assertIn("status = 'candidate'", updates[1])
        self.assertIn("origin = 'registry'", updates[1])
        # Тема, по которой уже есть материал, не возвращается: она в работе.
        self.assertIn("NOT EXISTS", updates[1])


class CheckNoteTest(unittest.TestCase):
    def test_check_note_is_cut_from_the_published_text(self):
        body, note = split_check_note(
            "Текст поста про механизм.\n\n"
            "ЧТО ПРОВЕРИТЬ: формулировка про обратную связь гипофиза."
        )
        self.assertNotIn("ЧТО ПРОВЕРИТЬ", body)
        self.assertEqual(note, "формулировка про обратную связь гипофиза.")

    def test_material_without_marker_stays_intact(self):
        body, note = split_check_note("Обычный текст без маркера.")
        self.assertEqual(body, "Обычный текст без маркера.")
        self.assertEqual(note, "")

    def test_prompt_asks_for_the_riskiest_claim(self):
        topic = TopicContext(
            id="topic-1",
            title="Тема",
            summary="Кратко",
            rationale="Почему",
            sources=({"source": "PubMed", "title": "t", "summary": "s", "url": "u"},),
        )
        instructions, _, _ = generation_prompt(topic, "reels")
        self.assertIn("ЧТО ПРОВЕРИТЬ", instructions)


if __name__ == "__main__":
    unittest.main()
