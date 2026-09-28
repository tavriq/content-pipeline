#!/usr/bin/env python3
"""Дайджест тем и генерация версионных материалов для Telegram-бота."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import logging

from ai_gateway import AIGateway, AIResult
from topic_registry import RUBRIC_NAMES, RegistryStore, underweighted_niches
from database import DatabaseSettings, connect
from lint import check_text, hard_violations

PAGE_SIZE = 5
MAX_DIGEST_TOPICS = 10
FORMATS = frozenset({"telegram", "reels", "tiktok"})
FORMAT_LABELS = {
    "telegram": "Telegram-пост",
    "reels": "сценарий Reels",
    "tiktok": "сценарий TikTok",
}
ROOT = Path(__file__).resolve().parent.parent
RED_LINES_PATH = ROOT / "core" / "red-lines.md"
SYSTEM_PROMPT_PATH = ROOT / "core" / "system-prompt.md"

log = logging.getLogger("content_workflow")

# Сколько раз перегенерировать материал, попавший в неотключаемое правило.
GENERATION_ATTEMPTS = 2

# Маркер, которым модель отделяет проверяемое утверждение от текста.
# В публикацию он уходить не должен, поэтому режется до сохранения.
CHECK_MARKER = "ЧТО ПРОВЕРИТЬ:"


def split_check_note(text: str) -> tuple[str, str]:
    """Отделяет «что проверить» от публикуемого текста."""
    marker_at = text.rfind(CHECK_MARKER)
    if marker_at == -1:
        return text.strip(), ""
    note = text[marker_at + len(CHECK_MARKER) :].strip()
    return text[:marker_at].strip(), " ".join(note.split())


@dataclass(frozen=True)
class DigestTopic:
    id: str
    title: str
    summary: str
    rationale: str
    score: float
    page: int
    rank: int
    origin: str = "pubmed"
    rubric: str | None = None
    niche: str | None = None

    @property
    def rubric_label(self) -> str:
        """Подпись под темой: «Р2 · что означает показатель · щитовидка»."""
        parts = []
        if self.rubric:
            name = RUBRIC_NAMES.get(self.rubric, "")
            parts.append(f"{self.rubric} · {name}" if name else self.rubric)
        if self.niche:
            parts.append(self.niche.replace("_", " "))
        parts.append("из плана блога" if self.origin == "registry" else "свежее исследование")
        return " · ".join(parts)


@dataclass(frozen=True)
class DigestPage:
    digest_id: str
    page: int
    topics: tuple[DigestTopic, ...]
    has_next: bool


@dataclass(frozen=True)
class TopicContext:
    id: str
    title: str
    summary: str
    rationale: str
    sources: tuple[dict[str, str], ...]
    origin: str = "pubmed"
    rubric: str | None = None
    niche: str | None = None
    author_note: str = ""


@dataclass(frozen=True)
class MaterialResult:
    material_id: str
    topic_id: str
    format: str
    version: int
    body: str
    ai_result: AIResult
    warnings: tuple[str, ...] = ()
    schedule_cancelled: bool = False
    check_note: str = ""
    title: str = ""
    sources: tuple[dict[str, str], ...] = ()


class RedLineViolation(ValueError):
    """Модель дважды выдала текст с нарушением разделов 1–2 красных линий."""


class ContentStore:
    def __init__(
        self,
        connection: Any,
        *,
        digest_id_factory: Callable[[], str] | None = None,
        material_id_factory: Callable[[], str] | None = None,
    ):
        self.connection = connection
        self.digest_id_factory = digest_id_factory or (
            lambda: f"digest-{uuid.uuid4().hex[:12]}"
        )
        self.material_id_factory = material_id_factory or (
            lambda: f"material-{uuid.uuid4().hex}"
        )

    def _pick_topics(self, cursor: Any, origin: str, limit: int) -> list[tuple]:
        """Кандидаты одного происхождения, лучшие по весу.

        Реестровые темы упорядочены нишей: наверх поднимается та, что
        недобрана против долей раздела 4 системного промпта. Свежие — весом,
        который выставило ранжирование.
        """
        if limit <= 0:
            return []
        cursor.execute(
            """
            SELECT id, title, summary, rationale, score, origin, rubric, niche
            FROM topics
            WHERE status = 'candidate' AND origin = %s
            ORDER BY score DESC, last_seen_at DESC
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (origin, limit),
        )
        return list(cursor.fetchall())

    def _order_registry(self, rows: list[tuple]) -> list[tuple]:
        """Разложить реестровые темы по недобранным нишам, без повторов ниши подряд."""
        try:
            counts = RegistryStore(self.connection).niche_counts()
        except Exception:
            counts = {}
        priority = underweighted_niches(counts, sum(counts.values()))
        order = {niche: index for index, niche in enumerate(priority)}
        buckets: dict[str, list[tuple]] = {}
        for row in rows:
            buckets.setdefault(row[7] or "чекапы", []).append(row)
        result: list[tuple] = []
        # По кругу, начиная с самой отстающей ниши: пять тем подряд про
        # щитовидку — это не дайджест, а одна тема с пятью заголовками.
        while buckets:
            for niche in sorted(buckets, key=lambda name: order.get(name, 99)):
                result.append(buckets[niche].pop(0))
                if not buckets[niche]:
                    del buckets[niche]
        return result

    def create_digest(self, limit: int = MAX_DIGEST_TOPICS) -> DigestPage | None:
        if not 1 <= limit <= MAX_DIGEST_TOPICS:
            raise ValueError("limit должен быть от 1 до 10")
        try:
            with self.connection.cursor() as cursor:
                # Повестку задают обе стороны: реестр — вопросы, которые
                # читатель уже задавал поиску, поток PubMed — то, что вышло на
                # этой неделе. Поровну; если в одной стороне кандидатов не
                # хватило, недостачу добирает другая.
                # Оба запроса подряд, до упорядочивания: счётчик ниш ходит в
                # базу тем же курсором, и вклиниваться между выборками ему
                # незачем.
                registry_pool = self._pick_topics(cursor, "registry", limit)
                fresh_rows = self._pick_topics(cursor, "pubmed", limit)
                registry_rows = self._order_registry(registry_pool)
                want_registry = min((limit + 1) // 2, len(registry_rows))
                want_fresh = min(limit - want_registry, len(fresh_rows))
                want_registry = min(limit - want_fresh, len(registry_rows))
                rows = _interleave(
                    registry_rows[:want_registry], fresh_rows[:want_fresh]
                )
                if not rows:
                    self.connection.rollback()
                    return None
                digest_id = self.digest_id_factory()
                cursor.execute(
                    """
                    INSERT INTO digest_batches (id, scheduled_for, status)
                    VALUES (%s, now(), 'ready')
                    """,
                    (digest_id,),
                )
                topics: list[DigestTopic] = []
                for index, row in enumerate(rows):
                    page = index // PAGE_SIZE + 1
                    rank = index + 1
                    topic = DigestTopic(
                        row[0], row[1], row[2], row[3], row[4], page, rank,
                        row[5], row[6], row[7],
                    )
                    topics.append(topic)
                    cursor.execute(
                        """
                        INSERT INTO digest_topics (digest_id, topic_id, page, rank)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (digest_id, topic.id, page, rank),
                    )
                cursor.execute(
                    """
                    UPDATE topics SET status = 'shown', last_seen_at = now()
                    WHERE id = ANY(%s)
                    """,
                    ([topic.id for topic in topics],),
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        first_page = tuple(topic for topic in topics if topic.page == 1)
        return DigestPage(
            digest_id=digest_id,
            page=1,
            topics=first_page,
            has_next=any(topic.page == 2 for topic in topics),
        )

    def digest_page(self, digest_id: str, page: int) -> DigestPage | None:
        if page not in (1, 2):
            raise ValueError("page должна быть 1 или 2")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT t.id, t.title, t.summary, t.rationale, t.score, dt.page, dt.rank,
                       t.origin, t.rubric, t.niche
                FROM digest_topics AS dt
                JOIN topics AS t ON t.id = dt.topic_id
                WHERE dt.digest_id = %s AND dt.page = %s
                  AND t.status = 'shown'
                ORDER BY dt.rank
                """,
                (digest_id, page),
            )
            rows = cursor.fetchall()
            if not rows:
                return None
            cursor.execute(
                """
                SELECT 1
                FROM digest_topics AS dt
                JOIN topics AS t ON t.id = dt.topic_id
                WHERE dt.digest_id = %s AND dt.page > %s
                  AND t.status = 'shown'
                LIMIT 1
                """,
                (digest_id, page),
            )
            has_next = cursor.fetchone() is not None
        return DigestPage(
            digest_id=digest_id,
            page=page,
            topics=tuple(DigestTopic(*row) for row in rows),
            has_next=has_next,
        )

    def latest_digest_page(self, page: int = 1) -> DigestPage | None:
        """Вернуть последний созданный дайджест, даже если новых тем нет."""
        if page not in (1, 2):
            raise ValueError("page должна быть 1 или 2")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT db.id
                FROM digest_batches AS db
                WHERE db.status IN ('ready', 'sent')
                  AND EXISTS (
                      SELECT 1
                      FROM digest_topics AS dt
                      JOIN topics AS t ON t.id = dt.topic_id
                      WHERE dt.digest_id = db.id AND dt.page = %s
                        AND t.status = 'shown'
                  )
                ORDER BY db.created_at DESC
                LIMIT 1
                """,
                (page,),
            )
            row = cursor.fetchone()
        return self.digest_page(str(row[0]), page) if row else None

    def mark_digest_sent(self, digest_id: str) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE digest_batches
                SET status = 'sent', sent_at = COALESCE(sent_at, now()), error = NULL
                WHERE id = %s AND status IN ('ready', 'sent')
                """,
                (digest_id,),
            )
            changed = cursor.rowcount == 1
        self.connection.commit()
        return changed

    def archive_stale_topics(self, retention_days: int = 7) -> int:
        """Скрыть старые показанные темы, сохранив историю и связи."""
        if retention_days <= 0:
            raise ValueError("retention_days должен быть больше нуля")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE topics
                SET status = 'archived'
                WHERE status = 'shown'
                  AND origin = 'pubmed'
                  AND last_seen_at < now() - make_interval(days => %s)
                  AND NOT EXISTS (
                      SELECT 1 FROM materials WHERE materials.topic_id = topics.id
                  )
                """,
                (retention_days,),
            )
            archived = int(cursor.rowcount)
            # Тема реестра, показанная и не взятая в работу, возвращается в
            # очередь. Без этого она зависала бы в «показана» навсегда:
            # архивации реестр не подлежит, и за десяток дайджестов от
            # тридцати трёх тем не осталось бы ни одной.
            cursor.execute(
                """
                UPDATE topics
                SET status = 'candidate'
                WHERE status = 'shown'
                  AND origin = 'registry'
                  AND last_seen_at < now() - make_interval(days => %s)
                  AND NOT EXISTS (
                      SELECT 1 FROM materials WHERE materials.topic_id = topics.id
                  )
                """,
                (retention_days,),
            )
            returned = int(cursor.rowcount)
        self.connection.commit()
        if returned:
            log.info("вернул в очередь тем реестра: %s", returned)
        return archived

    def topic_context(self, topic_id: str) -> TopicContext | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, title, summary, rationale, origin, rubric, niche, author_note
                FROM topics
                WHERE id = %s AND status <> 'archived'
                """,
                (topic_id,),
            )
            topic = cursor.fetchone()
            if topic is None:
                return None
            cursor.execute(
                """
                SELECT s.name, si.title, si.summary, si.url
                FROM topic_sources AS ts
                JOIN source_items AS si ON si.id = ts.source_item_id
                JOIN sources AS s ON s.id = si.source_id
                WHERE ts.topic_id = %s
                ORDER BY CASE ts.evidence_role
                    WHEN 'primary' THEN 0 WHEN 'supporting' THEN 1 ELSE 2 END,
                    si.published_at DESC NULLS LAST
                """,
                (topic_id,),
            )
            sources = tuple(
                {"source": row[0], "title": row[1], "summary": row[2], "url": row[3]}
                for row in cursor.fetchall()
            )
        return TopicContext(
            id=topic[0],
            title=topic[1],
            summary=topic[2],
            rationale=topic[3],
            sources=sources,
            origin=topic[4] or "pubmed",
            rubric=topic[5],
            niche=topic[6],
            author_note=topic[7] or "",
        )

    def evidence_query(self, topic_id: str) -> str:
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT evidence_query FROM topics WHERE id = %s", (topic_id,))
            row = cursor.fetchone()
        return str(row[0]) if row and row[0] else ""

    def save_evidence_query(self, topic_id: str, query: str) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "UPDATE topics SET evidence_query = %s WHERE id = %s",
                (query.strip(), topic_id),
            )
        self.connection.commit()

    def link_sources(self, topic_id: str, external_ids: list[str]) -> int:
        """Связать тему с найденными публикациями.

        Первая по списку идёт основной опорой: PubMed отдаёт результаты по
        релевантности, и лучшее совпадение обычно первое.
        """
        if not external_ids:
            return 0
        linked = 0
        try:
            with self.connection.cursor() as cursor:
                for position, external_id in enumerate(external_ids):
                    cursor.execute(
                        """
                        INSERT INTO topic_sources (topic_id, source_item_id, evidence_role)
                        SELECT %s, si.id, %s
                          FROM source_items AS si
                         WHERE si.external_id = %s
                         LIMIT 1
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            topic_id,
                            "primary" if position == 0 else "supporting",
                            external_id,
                        ),
                    )
                    linked += cursor.rowcount
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return linked

    def save_author_note(self, topic_id: str, note: str) -> bool:
        """Записать фактуру автора, наговорённую под конкретную тему."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE topics
                   SET author_note = %s, author_note_at = now()
                 WHERE id = %s AND status <> 'archived'
                """,
                (note.strip(), topic_id),
            )
            updated = cursor.rowcount > 0
        self.connection.commit()
        return updated

    def save_material_version(
        self,
        *,
        topic_id: str,
        format_name: str,
        body: str,
        ai_result: AIResult,
        edit_mode: str = "generated",
        edit_instruction: str | None = None,
        check_note: str = "",
        rubric: str | None = None,
        niche: str | None = None,
    ) -> tuple[str, int, bool]:
        if format_name not in FORMATS:
            raise ValueError("Неизвестный формат материала")
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, current_version FROM materials
                    WHERE topic_id = %s AND format = %s
                    FOR UPDATE
                    """,
                    (topic_id, format_name),
                )
                existing = cursor.fetchone()
                if existing:
                    material_id, current_version = str(existing[0]), int(existing[1])
                else:
                    material_id, current_version = self.material_id_factory(), 0
                    cursor.execute(
                        """
                        INSERT INTO materials (
                            id, topic_id, format, status, current_version, rubric, niche
                        )
                        VALUES (%s, %s, %s, 'draft', 0, %s, %s)
                        """,
                        (material_id, topic_id, format_name, rubric, niche),
                    )
                version = current_version + 1
                cursor.execute(
                    """
                    INSERT INTO material_versions (
                        material_id, version, body, edit_instruction, edit_mode,
                        model, input_tokens, output_tokens, check_note
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        material_id,
                        version,
                        body,
                        edit_instruction,
                        edit_mode,
                        ai_result.model,
                        ai_result.input_tokens,
                        ai_result.output_tokens,
                        check_note or None,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE materials
                    SET current_version = %s, status = 'review', updated_at = now(),
                        approved_at = NULL
                    WHERE id = %s
                    """,
                    (version, material_id),
                )
                cursor.execute(
                    "UPDATE topics SET status = 'selected', selected_at = now() WHERE id = %s",
                    (topic_id,),
                )
                # Новая версия — это уже не тот текст, который одобряли.
                # Раньше строка в publications оставалась нетронутой, и в
                # назначенное время в канал уходила неодобренная правка.
                cursor.execute(
                    """
                    UPDATE publications SET status = 'cancelled', locked_at = NULL,
                        last_error = 'Снято с публикации: материал ушёл на правку'
                    WHERE material_id = %s AND status IN ('scheduled', 'failed', 'held')
                    RETURNING id
                    """,
                    (material_id,),
                )
                cancelled = cursor.fetchone() is not None
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return material_id, version, cancelled

    def current_material(self, material_id: str) -> tuple[str, str, str, int] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT m.topic_id, m.format, mv.body, mv.version
                FROM materials AS m
                JOIN material_versions AS mv
                  ON mv.material_id = m.id AND mv.version = m.current_version
                WHERE m.id = %s
                """,
                (material_id,),
            )
            row = cursor.fetchone()
        return (str(row[0]), str(row[1]), str(row[2]), int(row[3])) if row else None

    def approve_material(self, material_id: str, approved_by: int | None = None) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE materials SET status = 'approved', approved_at = now(),
                    updated_at = now(), approved_by = %s
                WHERE id = %s AND status = 'review'
                """,
                (approved_by, material_id),
            )
            changed = cursor.rowcount == 1
        self.connection.commit()
        return changed


def _interleave(first: list, second: list) -> list:
    """Перемешать два списка через один, сохранив порядок внутри каждого.

    Автор видит на первой странице и то и другое, а не пять тем из реестра
    подряд и свежее — на второй, куда доходят не всегда.
    """
    result = []
    for index in range(max(len(first), len(second))):
        if index < len(first):
            result.append(first[index])
        if index < len(second):
            result.append(second[index])
    return result


def prompt_sections(numbers: tuple[int, ...]) -> str:
    """Разделы core/system-prompt.md по номерам.

    Роль редактора живёт в одном экземпляре — в файле, а не строкой в
    коде. Целиком промпт в каждый запрос не тащим: разделы про площадки,
    режимы и машинный вывод генерации не нужны, а токены платные.
    """
    wanted = {f"## {number}." for number in numbers}
    chunks: list[str] = []
    current: list[str] | None = None
    for line in SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if current:
                chunks.append("\n".join(current).strip())
            heading = line.split()[0] + " " + line.split()[1]
            current = [line] if heading in wanted else None
            continue
        if current is not None:
            current.append(line)
    if current:
        chunks.append("\n".join(current).strip())
    return "\n\n".join(chunks)


def prompt_subsection(number: str) -> str:
    """Подраздел системного промпта вида «### 9.1».

    Форматы вывода описаны там с точностью до полей: хук, текст на экране,
    визуал по секундам, подпись, хештеги. Тащить весь раздел 9 незачем —
    каруселей и сторис мы пока не делаем, а токены платные.
    """
    lines = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").splitlines()
    chunk: list[str] = []
    collecting = False
    for line in lines:
        if line.startswith("### "):
            if collecting:
                break
            collecting = line.startswith(f"### {number}")
            if collecting:
                chunk.append(line)
            continue
        if collecting:
            chunk.append(line)
    return "\n".join(chunk).strip()


FORMAT_SPECS = {
    "telegram": {
        "subsection": "9.2",
        "task": (
            "Напиши пост в Telegram по формату 9.2 целиком: ЗАГОЛОВОК, ТЕКСТ, "
            "ВЫВОД, ВОВЛЕЧЕНИЕ, ДИСКЛЕЙМЕР. Тело — 1200–2500 знаков, абзацы "
            "по две-четыре строки. Держись в пределах 3500 знаков: длиннее "
            "телеграм не примет."
        ),
        "max_tokens": 3500,
    },
    "reels": {
        "subsection": "9.1",
        "task": (
            "Напиши сценарий Reels на 45–60 секунд по формату 9.1 целиком, со "
            "всеми полями: ХУК, ТЕКСТ НА ЭКРАНЕ В ХУКЕ, ОСНОВНАЯ ЧАСТЬ, "
            "ЗАКРЫТИЕ, ВИЗУАЛ, ТЕКСТ ПОДПИСИ, ХЕШТЕГИ, ДИСКЛЕЙМЕР. ВИЗУАЛ "
            "расписывай по секундам и указывай, нужна ли съёмка автора в "
            "кадре или хватит футажей с озвучкой — по этому полю материал "
            "идёт на съёмку, без него сценарий бесполезен."
        ),
        "max_tokens": 3000,
    },
    "tiktok": {
        "subsection": "9.1",
        "task": (
            "Напиши сценарий TikTok на 30–45 секунд по формату 9.1 целиком, со "
            "всеми полями: ХУК, ТЕКСТ НА ЭКРАНЕ В ХУКЕ, ОСНОВНАЯ ЧАСТЬ, "
            "ЗАКРЫТИЕ, ВИЗУАЛ, ТЕКСТ ПОДПИСИ, ХЕШТЕГИ, ДИСКЛЕЙМЕР. Темп выше, "
            "чем в Reels, фразы короче. ВИЗУАЛ — по секундам, с пометкой про "
            "съёмку в кадре или озвучку поверх футажей."
        ),
        "max_tokens": 2800,
    },
}


def rubric_brief(rubric: str | None, niche: str | None) -> str:
    """Одна строка задания: в какой рубрике и нише подаётся тема."""
    parts = []
    if rubric:
        name = RUBRIC_NAMES.get(rubric, "")
        parts.append(f"рубрика {rubric}" + (f" — {name}" if name else ""))
    if niche:
        parts.append(f"ниша «{niche.replace('_', ' ')}»")
    return ", ".join(parts)


def generation_prompt(context: TopicContext, format_name: str) -> tuple[str, str, int]:
    spec = FORMAT_SPECS[format_name]
    max_tokens = int(spec["max_tokens"])
    task = str(spec["task"])
    format_block = prompt_subsection(str(spec["subsection"]))

    brief = rubric_brief(context.rubric, context.niche)
    if brief:
        task += (
            f"\n\nПодача: {brief}. Рубрика задаёт угол: разбор мифа начинается "
            "с самого утверждения, «что означает показатель» — с того, что "
            "человек видит в бланке, «когда пора к эндокринологу» — с ситуации, "
            "а не с теории. Не подменяй рубрику пересказом исследования."
        )

    task += (
        "\n\nПоследней строкой, отдельным абзацем, добавь "
        f"«{CHECK_MARKER} …» — одно конкретное утверждение из материала, "
        "которое автор обязана сверить перед публикацией: цифру, "
        "формулировку механизма или границу нормы. Не «проверить факты», "
        "а именно что и почему. Эта строка в публикацию не пойдёт."
    )

    red_lines = RED_LINES_PATH.read_text(encoding="utf-8")
    # Разделы: 1 — кто автор и как о ней можно говорить, 4 — ниши,
    # 5 — портрет читателя, 6 — тон и запрещённые штампы, 8 — рубрикатор,
    # 10 — правила хука, 13 — самопроверка перед выдачей.
    role = prompt_sections((1, 4, 5, 6, 8, 10, 13))
    instructions = (
        "Ты медицинский редактор образовательного блога. "
        "Не додумывай факты. Пиши живо, точно, без юмора и без AI-штампов. "
        "Соблюдай все красные линии ниже.\n\n"
        f"{red_lines}\n\n{role}\n\n"
        f"ФОРМАТ ВЫВОДА:\n{format_block}\n\n"
        f"ЗАДАНИЕ:\n{task}"
    )

    lines = [f"Тема: {context.title}"]
    if brief:
        lines.append(f"Подача: {brief}")
    if context.summary and context.summary != context.title:
        lines.append(f"Кратко: {context.summary}")
    if context.rationale:
        lines.append(f"Почему эта тема: {context.rationale}")

    if context.author_note:
        # Слова автора идут выше источников: разбор на кафедре и то, что
        # путают пациенты, не лежит ни в одном абстракте. Ради этого блока
        # бот и спрашивает «есть что добавить от себя».
        lines.append(
            "\nЧТО СКАЗАЛА АВТОР — приоритетный материал, строй объяснение "
            "вокруг него, а источники используй как опору:\n"
            f"{context.author_note}"
        )

    if context.sources:
        lines.append("\nИсточники:")
        lines.extend(
            f"- {source['source']}: {source['title']}\n  {source['summary']}\n  {source['url']}"
            for source in context.sources
        )
    else:
        # Источников может не быть: тема из плана блога, а поиск по ней ничего
        # не дал. Материал всё равно нужен, но без единой цифры — иначе модель
        # придумает и процент, и год исследования.
        lines.append(
            "\nИСТОЧНИКОВ НЕТ. Объясняй механизм и общие принципы, ссылаясь на "
            "то, что написано в клинических рекомендациях, без конкретики. "
            "Категорически запрещено: проценты, числа пациентов, годы и "
            "названия исследований, названия организаций с цифрами. Ни одной "
            "цифры, которую нельзя подтвердить. В строке "
            f"«{CHECK_MARKER}» укажи, по какому документу автору сверить "
            "ключевое утверждение."
        )

    return instructions, "\n".join(lines), max_tokens


def generate_checked(
    *,
    instructions: str,
    content: str,
    max_tokens: int,
    label: str,
) -> tuple[AIResult, str, str, tuple[str, ...]]:
    """Генерация с детерминированной проверкой красных линий.

    Промпт с red-lines.md — не гарантия: модель ошибается, а цена ошибки
    здесь — материал с дозировкой или «как эндокринолог я» в канале. При
    попадании в неотключаемое правило текст автору не показываем вовсе,
    а просим модель переписать. Мягкие срабатывания (вес, штампы) идут
    автору пометкой: их иногда обязан цитировать разбор мифа.

    Возвращает результат модели, публикуемый текст, строку «что
    проверить» и мягкие замечания линтера.
    """
    gateway = AIGateway.from_environment()
    attempt_content = content
    last: list[str] = []
    for _ in range(GENERATION_ATTEMPTS):
        ai_result = gateway.generate(
            task="writer",
            instructions=instructions,
            content=attempt_content,
            max_tokens=max_tokens,
        )
        body, check_note = split_check_note(ai_result.text)
        violations = hard_violations(body, label)
        if not violations:
            hits, _ = check_text(body, label)
            return ai_result, body, check_note, tuple(hits)
        last = violations
        attempt_content = (
            f"{content}\n\nПредыдущая версия нарушила красные линии:\n"
            + "\n".join(last[:5])
            + "\nПерепиши так, чтобы этих формулировок не было вовсе."
        )
    raise RedLineViolation(
        "Черновик дважды нарушил красные линии — не показываю. "
        "Сообщил разработчику, тему лучше взять другую.\n"
        + "\n".join(item.split("] ", 1)[-1] for item in last[:3])
    )


def _store_from_environment() -> tuple[Any, ContentStore]:
    settings = DatabaseSettings.from_environment()
    connection = connect(settings)
    return connection, ContentStore(connection)


def create_digest_from_environment() -> DigestPage | None:
    connection, store = _store_from_environment()
    with connection:
        return store.create_digest()


def load_digest_page_from_environment(digest_id: str, page: int) -> DigestPage | None:
    connection, store = _store_from_environment()
    with connection:
        return store.digest_page(digest_id, page)


def load_latest_digest_from_environment(page: int = 1) -> DigestPage | None:
    connection, store = _store_from_environment()
    with connection:
        return store.latest_digest_page(page)


def mark_digest_sent_from_environment(digest_id: str) -> bool:
    connection, store = _store_from_environment()
    with connection:
        return store.mark_digest_sent(digest_id)


def archive_stale_topics_from_environment(retention_days: int = 7) -> int:
    connection, store = _store_from_environment()
    with connection:
        return store.archive_stale_topics(retention_days)


def load_topic_from_environment(topic_id: str) -> TopicContext | None:
    connection, store = _store_from_environment()
    with connection:
        return store.topic_context(topic_id)


def build_evidence_query(title: str, rubric: str | None = None) -> str:
    """Перевести тему блога в поисковый запрос к PubMed.

    Темы реестра сформулированы словами читателя и по-русски: «ТТГ в норме,
    а самочувствие плохое». Искать по ним нечего — нужен запрос терминами и
    по-английски. Работа копеечная, поэтому идёт на быструю модель.
    """
    gateway = AIGateway.from_environment()
    result = gateway.generate(
        task="fast",
        instructions=(
            "Ты медицинский библиограф. По теме русскоязычного блога составь "
            "поисковый запрос к PubMed на английском: два-четыре ключевых "
            "термина через AND и OR, без фильтров по типу публикации и без "
            "дат — их добавят отдельно. Верни только строку запроса, без "
            "пояснений и кавычек."
        ),
        content=f"Тема: {title}" + (f"\nРубрика: {rubric}" if rubric else ""),
        # Двести токенов модель тратила на рассуждение целиком, и до ответа
        # не доходило: лимит считает и то и другое. Сама строка запроса —
        # десяток слов, платим мы по факту, а не по лимиту.
        max_tokens=2000,
    )
    query = " ".join(result.text.split()).strip().strip('"')
    if not query:
        raise ValueError("Модель вернула пустой поисковый запрос")
    return query


def attach_evidence(store: "ContentStore", topic: TopicContext) -> TopicContext:
    """Найти и привязать источники к теме, у которой их ещё нет.

    Для темы из плана блога источников не существует по определению: она
    родилась из вопроса читателя, а не из публикации. Здесь конвейер идёт в
    обратную сторону — от темы к доказательной базе.
    """
    if topic.sources:
        return topic
    from collect_sources import (
        SourceConfigurationError,
        SourceUnavailable,
        search_evidence_from_environment,
    )
    from source_store import PUBMED_DEFINITION, SourceStore

    from ai_gateway import AIConfigurationError, AIResponseError

    try:
        query = store.evidence_query(topic.id) or build_evidence_query(
            topic.title, topic.rubric
        )
        store.save_evidence_query(topic.id, query)
        items = search_evidence_from_environment(query)
    except (
        SourceConfigurationError,
        SourceUnavailable,
        AIConfigurationError,
        AIResponseError,
        ValueError,
        OSError,
    ) as error:
        # Без источников материал всё равно соберётся — промпт в этом случае
        # запрещает любые цифры. Молчать об этом нельзя: в логе должно быть
        # видно, почему у темы пустая доказательная база.
        log.warning("источники под тему %s не найдены: %s", topic.id, error)
        return topic
    if not items:
        log.info("PubMed не дал источников под тему %s по запросу %r", topic.id, query)
        return topic
    SourceStore(store.connection).save_items(items, PUBMED_DEFINITION)
    # Связываем по внешнему идентификатору PubMed: сохранение отдаёт сводку,
    # а не строки, и часть публикаций может быть уже в базе с прошлого сбора.
    store.link_sources(topic.id, [item.external_id for item in items])
    refreshed = store.topic_context(topic.id)
    return refreshed or topic


def generate_material_from_environment(topic_id: str, format_name: str) -> MaterialResult:
    connection, store = _store_from_environment()
    with connection:
        topic = store.topic_context(topic_id)
        if topic is None:
            raise ValueError("Тема не найдена")
        topic = attach_evidence(store, topic)
    instructions, content, max_tokens = generation_prompt(topic, format_name)
    ai_result, body, check_note, warnings = generate_checked(
        instructions=instructions,
        content=content,
        max_tokens=max_tokens,
        label=f"{topic_id}/{format_name}",
    )
    connection, store = _store_from_environment()
    with connection:
        material_id, version, cancelled = store.save_material_version(
            topic_id=topic_id,
            format_name=format_name,
            body=body,
            ai_result=ai_result,
            check_note=check_note,
            rubric=topic.rubric,
            niche=topic.niche,
        )
    return MaterialResult(
        material_id=material_id,
        topic_id=topic_id,
        format=format_name,
        version=version,
        body=body,
        ai_result=ai_result,
        warnings=warnings,
        schedule_cancelled=cancelled,
        check_note=check_note,
        title=topic.title,
        sources=topic.sources,
    )


def adapt_material_from_environment(
    topic_id: str, format_name: str, source_body: str
) -> MaterialResult:
    """Короткий формат делается из уже готового поста, а не с нуля.

    Так автор проверяет фактуру один раз, а не трижды, и мы не платим за
    три независимых прохода по одним и тем же источникам.
    """
    connection, store = _store_from_environment()
    with connection:
        topic = store.topic_context(topic_id)
    if topic is None:
        raise ValueError("Тема не найдена")
    instructions, _, max_tokens = generation_prompt(topic, format_name)
    ai_result, body, check_note, warnings = generate_checked(
        instructions=instructions,
        content=(
            f"Готовый Telegram-пост по этой теме:\n{source_body}\n\n"
            "Сделай из него материал нужного формата. Новых фактов, цифр и "
            "исследований не добавляй — только то, что уже есть в тексте."
        ),
        max_tokens=max_tokens,
        label=f"{topic_id}/{format_name}",
    )
    connection, store = _store_from_environment()
    with connection:
        material_id, version, cancelled = store.save_material_version(
            topic_id=topic_id,
            format_name=format_name,
            body=body,
            ai_result=ai_result,
            check_note=check_note,
            rubric=topic.rubric,
            niche=topic.niche,
        )
    return MaterialResult(
        material_id=material_id,
        topic_id=topic_id,
        format=format_name,
        version=version,
        body=body,
        ai_result=ai_result,
        warnings=warnings,
        schedule_cancelled=cancelled,
        check_note=check_note,
        title=topic.title,
        sources=topic.sources,
    )


def generate_bundle_from_environment(topic_id: str) -> list[MaterialResult]:
    """Пост плюс два коротких сценария из него же."""
    post = generate_material_from_environment(topic_id, "telegram")
    results = [post]
    for format_name in ("reels", "tiktok"):
        results.append(
            adapt_material_from_environment(topic_id, format_name, post.body)
        )
    return results


def revise_material_from_environment(
    material_id: str, instruction: str, edit_mode: str = "text"
) -> MaterialResult:
    if edit_mode not in {"text", "voice"}:
        raise ValueError("Правка должна быть текстовой или голосовой")
    if not instruction.strip():
        raise ValueError("Правка не может быть пустой")
    connection, store = _store_from_environment()
    with connection:
        material = store.current_material(material_id)
        if material is None:
            raise ValueError("Материал не найден")
        topic_id, format_name, current_body, _ = material
        topic = store.topic_context(topic_id)
    if topic is None:
        raise ValueError("Тема материала не найдена")
    base_instructions, _, max_tokens = generation_prompt(topic, format_name)
    ai_result, body, check_note, warnings = generate_checked(
        instructions=base_instructions,
        content=(
            f"Текущая версия:\n{current_body}\n\n"
            f"Правка автора:\n{instruction.strip()}\n\n"
            "Верни материал целиком с учётом правки. Не комментируй изменения."
        ),
        max_tokens=max_tokens,
        label=material_id,
    )
    connection, store = _store_from_environment()
    with connection:
        _, version, cancelled = store.save_material_version(
            topic_id=topic_id,
            format_name=format_name,
            body=body,
            ai_result=ai_result,
            edit_mode=edit_mode,
            edit_instruction=instruction.strip(),
            check_note=check_note,
        )
    return MaterialResult(
        material_id=material_id,
        topic_id=topic_id,
        format=format_name,
        version=version,
        body=body,
        ai_result=ai_result,
        warnings=warnings,
        schedule_cancelled=cancelled,
        check_note=check_note,
        title=topic.title,
        sources=topic.sources,
    )


def set_topic_rubric_from_environment(topic_id: str, rubric: str) -> bool:
    """Сменить рубрику темы по решению автора."""
    connection, store = _store_from_environment()
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE topics SET rubric = %s WHERE id = %s AND status <> 'archived'",
                (rubric, topic_id),
            )
            changed = cursor.rowcount > 0
        connection.commit()
    return changed


def save_topic_note_from_environment(topic_id: str, note: str) -> bool:
    connection, store = _store_from_environment()
    with connection:
        return store.save_author_note(topic_id, note)


def approve_material_from_environment(material_id: str, approved_by: int | None = None) -> bool:
    connection, store = _store_from_environment()
    with connection:
        return store.approve_material(material_id, approved_by)
