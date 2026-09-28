#!/usr/bin/env python3
"""Ранжирование новых публикаций в темы для двух страниц дайджеста."""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable

from ai_gateway import AIGateway, AIResult
from database import DatabaseSettings, connect

RANKING_LOCK_NAME = "blog-content-topic-ranking"
RED_LINES_PATH = Path(__file__).resolve().parent.parent / "core" / "red-lines.md"

# Сколько раз публикация может не попасть в темы, прежде чем перестанет
# предлагаться модели. Без счётчика она либо исчезала после первого
# захода, либо возвращалась бы бесконечно.
MAX_RANKING_ATTEMPTS = 3


# Рубрики раздела 8 системного промпта, доступные потоковым темам. Р4
# «изнутри медвуза» и Р6 «вопрос из комментариев» из публикации не рождаются:
# первая — из её учёбы, вторая — из комментариев читателей.
KNOWN_RUBRICS = ("Р1", "Р2", "Р3", "Р5", "Р7")
KNOWN_NICHES = ("питание", "женские_гормоны", "чекапы", "щитовидка", "личное")


class TopicRankingError(ValueError):
    """Ответ модели не соответствует контракту или ссылается не на те источники."""


@dataclass(frozen=True)
class RankingSourceItem:
    id: int
    source_name: str
    title: str
    summary: str
    url: str
    published_at: datetime | None


@dataclass(frozen=True)
class TopicCandidate:
    title: str
    summary: str
    rationale: str
    score: float
    source_item_ids: tuple[int, ...]
    rubric: str | None = None
    niche: str | None = None


@dataclass(frozen=True)
class RankingResult:
    candidates: tuple[TopicCandidate, ...]
    ai_result: AIResult


@dataclass(frozen=True)
class SavedRanking:
    topic_ids: tuple[str, ...]
    sources_marked: int


def _clean_json(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].strip().casefold() in ("```", "```json"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def parse_topic_candidates(
    text: str,
    *,
    allowed_source_ids: set[int],
    max_topics: int = 10,
) -> tuple[TopicCandidate, ...]:
    try:
        payload = json.loads(_clean_json(text))
    except json.JSONDecodeError as error:
        raise TopicRankingError("Luna вернула невалидный JSON") from error
    topics = payload.get("topics") if isinstance(payload, dict) else None
    if not isinstance(topics, list) or not topics:
        raise TopicRankingError("В ответе Luna нужен непустой массив topics")
    if len(topics) > max_topics:
        raise TopicRankingError(f"Luna вернула больше {max_topics} тем")

    candidates: list[TopicCandidate] = []
    normalized_titles: set[str] = set()
    for index, raw in enumerate(topics, start=1):
        if not isinstance(raw, dict):
            raise TopicRankingError(f"Тема {index} должна быть объектом")
        title = " ".join(str(raw.get("title", "")).split())
        summary = " ".join(str(raw.get("summary", "")).split())
        rationale = " ".join(str(raw.get("rationale", "")).split())
        if not title or not summary or not rationale:
            raise TopicRankingError(f"У темы {index} не заполнены title/summary/rationale")
        normalized_title = title.casefold()
        if normalized_title in normalized_titles:
            raise TopicRankingError(f"Luna продублировала тему: {title}")
        normalized_titles.add(normalized_title)

        try:
            score = float(raw["score"])
        except (KeyError, TypeError, ValueError) as error:
            raise TopicRankingError(f"У темы {index} некорректный score") from error
        if not 0 <= score <= 100:
            raise TopicRankingError(f"У темы {index} score должен быть от 0 до 100")

        raw_ids = raw.get("source_item_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise TopicRankingError(f"У темы {index} нет source_item_ids")
        try:
            source_ids = tuple(dict.fromkeys(int(value) for value in raw_ids))
        except (TypeError, ValueError) as error:
            raise TopicRankingError(f"У темы {index} некорректные source_item_ids") from error
        unknown = set(source_ids) - allowed_source_ids
        if unknown:
            raise TopicRankingError(
                f"Тема {index} ссылается на неизвестные источники: {sorted(unknown)}"
            )
        rubric = str(raw.get("rubric", "")).strip().upper().replace("P", "Р")
        niche = str(raw.get("niche", "")).strip().lower().replace(" ", "_")
        candidates.append(
            TopicCandidate(
                title=title,
                summary=summary,
                rationale=rationale,
                score=score,
                source_item_ids=source_ids,
                rubric=rubric if rubric in KNOWN_RUBRICS else None,
                niche=niche if niche in KNOWN_NICHES else None,
            )
        )
    return tuple(candidates)


def rank_topics(
    gateway: AIGateway,
    items: Iterable[RankingSourceItem],
    *,
    max_topics: int = 10,
) -> RankingResult:
    batch = list(items)
    if not batch:
        raise TopicRankingError("Нет новых публикаций для ранжирования")
    if not 1 <= max_topics <= 10:
        raise ValueError("max_topics должен быть от 1 до 10")

    source_payload = [
        {
            "id": item.id,
            "source": item.source_name,
            "title": item.title,
            "summary": item.summary,
            "url": item.url,
            "published_at": item.published_at.isoformat() if item.published_at else None,
        }
        for item in batch
    ]
    # Красные линии подмешиваются и сюда. Запрос к PubMed целенаправленно
    # тянет ожирение и метаболизм — самую опасную нишу блога, — а сортировку
    # придумывает модель. Без правил она предлагала темы, которые проекту
    # писать нельзя, и это выяснялось уже на генерации.
    red_lines = RED_LINES_PATH.read_text(encoding="utf-8")
    instructions = (
        "Ты редактор образовательного блога по эндокринологии. Читатель — "
        "женщина 20–40 лет без медицинского образования, которая ночью вбила "
        "в поиск свой результат анализа, прочитала десять форумов и "
        "напугана. Она не врач и никогда им не будет.\n\n"
        "Сгруппируй публикации в темы, которые отвечают на её вопросы. Не "
        "ставь диагнозы, не давай схем лечения, не додумывай факты.\n\n"
        "score от 0 до 100 складывается из трёх вещей, в этом порядке:\n"
        "1. Отвечает ли тема на вопрос, который читательница уже задавала "
        "поиску. «Почему при норме анализов плохо», «что означает эта "
        "строчка в бланке», «когда это повод обследоваться» — да. "
        "Маршрутизация пациентов, выбор антикоагулянта, протеомные "
        "биомаркеры, дизайн исследования — нет: это разговор для коллег, "
        "и такие темы оценивай ниже 30 или не предлагай вовсе.\n"
        "2. Раскрывается ли тема без нарушения правил ниже. Препараты для "
        "снижения веса, схемы приёма, дозировки — не предлагай совсем, "
        "даже если публикация весомая: об этом блог писать не может.\n"
        "3. Есть ли под тему источник, по которому автор сможет сверить "
        "ключевое утверждение. Уровень доказательности важен, но он "
        "определяет, можно ли тему брать, а не насколько она нужна.\n\n"
        "Оценивать по ожидаемому охвату и кликабельности запрещено: по этой "
        "шкале наверх всплывают вес и диеты, где блог обязан быть особенно "
        "осторожным.\n\n"
        "Заголовок темы пиши словами читателя, а не аннотации: «ТТГ в норме, "
        "а самочувствие плохое», а не «Субклинический гипотиреоз: "
        "современные подходы к ведению».\n\n"
        f"{red_lines}\n\n"
        "Верни только JSON-объект с массивом topics. Для каждой темы "
        "обязательны title, summary, rationale, score от 0 до 100 и "
        "source_item_ids из переданного списка. Поле rubric — код рубрики из "
        "списка: Р1 разбор мифа, Р2 что означает показатель, Р3 когда пора к "
        "эндокринологу, Р5 механизм простыми словами, Р7 что нового. Поле "
        "niche — одно из: питание, женские_гормоны, чекапы, щитовидка."
    )
    content = json.dumps(
        {"max_topics": max_topics, "sources": source_payload},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    ai_result = gateway.generate(
        task="fast",
        instructions=instructions,
        content=content,
        max_tokens=6000,
    )
    candidates = parse_topic_candidates(
        ai_result.text,
        allowed_source_ids={item.id for item in batch},
        max_topics=max_topics,
    )
    return RankingResult(candidates=candidates, ai_result=ai_result)


class TopicRankingStore:
    def __init__(self, connection: Any, id_factory: Callable[[], str] | None = None):
        self.connection = connection
        self.id_factory = id_factory or (lambda: f"topic-{uuid.uuid4().hex}")

    def acquire_lock(self) -> bool:
        """Разрешить только один ранжировщик на PostgreSQL-сессию."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))",
                (RANKING_LOCK_NAME,),
            )
            row = cursor.fetchone()
        return bool(row and row[0])

    def pending_items(self, limit: int = 50) -> list[RankingSourceItem]:
        if not 1 <= limit <= 200:
            raise ValueError("limit должен быть от 1 до 200")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT si.id, s.name, si.title, si.summary, si.url, si.published_at
                FROM source_items AS si
                JOIN sources AS s ON s.id = si.source_id
                WHERE si.is_preprint = FALSE AND si.ranked_at IS NULL
                  AND si.ranking_attempts < %s
                ORDER BY si.published_at DESC NULLS LAST, si.fetched_at DESC
                LIMIT %s
                """,
                (MAX_RANKING_ATTEMPTS, limit),
            )
            rows = cursor.fetchall()
        return [RankingSourceItem(*row) for row in rows]

    def save(
        self,
        candidates: Iterable[TopicCandidate],
        *,
        considered_source_ids: Iterable[int],
    ) -> SavedRanking:
        topics = list(candidates)
        considered = tuple(dict.fromkeys(int(value) for value in considered_source_ids))
        if not topics or not considered:
            raise ValueError("Для сохранения нужны темы и рассмотренные источники")
        referenced = {
            source_id for candidate in topics for source_id in candidate.source_item_ids
        }
        unknown = referenced - set(considered)
        if unknown:
            raise ValueError(f"Темы ссылаются на нерассмотренные источники: {sorted(unknown)}")
        topic_ids: list[str] = []
        try:
            with self.connection.cursor() as cursor:
                for candidate in topics:
                    topic_id = self.id_factory()
                    topic_ids.append(topic_id)
                    cursor.execute(
                        """
                        INSERT INTO topics (
                            id, title, summary, rationale, score,
                            origin, rubric, niche
                        )
                        VALUES (%s, %s, %s, %s, %s, 'pubmed', %s, %s)
                        """,
                        (
                            topic_id,
                            candidate.title,
                            candidate.summary,
                            candidate.rationale,
                            candidate.score,
                            candidate.rubric,
                            candidate.niche,
                        ),
                    )
                    for position, source_item_id in enumerate(candidate.source_item_ids):
                        cursor.execute(
                            """
                            INSERT INTO topic_sources (
                                topic_id, source_item_id, evidence_role
                            ) VALUES (%s, %s, %s)
                            """,
                            (
                                topic_id,
                                source_item_id,
                                "primary" if position == 0 else "supporting",
                            ),
                        )
                # Закрываем только то, что реально вошло в темы. Остальным
                # добавляем попытку: у публикации есть ещё два захода в
                # другом наборе кандидатов, а потом она уходит из очереди.
                used = sorted(
                    {
                        int(source_item_id)
                        for candidate in topics
                        for source_item_id in candidate.source_item_ids
                    }
                )
                cursor.execute(
                    """
                    UPDATE source_items
                    SET ranked_at = now()
                    WHERE id = ANY(%s) AND ranked_at IS NULL
                    """,
                    (used,),
                )
                sources_marked = int(cursor.rowcount)
                skipped = [item for item in considered if item not in set(used)]
                if skipped:
                    cursor.execute(
                        """
                        UPDATE source_items
                        SET ranking_attempts = ranking_attempts + 1
                        WHERE id = ANY(%s) AND ranked_at IS NULL
                        """,
                        (skipped,),
                    )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return SavedRanking(topic_ids=tuple(topic_ids), sources_marked=sources_marked)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-limit", type=int, default=50)
    parser.add_argument("--topics", type=int, default=10)
    args = parser.parse_args()

    settings = DatabaseSettings.from_environment()
    with connect(settings) as connection:
        store = TopicRankingStore(connection)
        if not store.acquire_lock():
            print("Ранжирование уже выполняется другим процессом")
            return 0
        items = store.pending_items(args.source_limit)
        if not items:
            print("Новых публикаций для ранжирования нет")
            return 0
        result = rank_topics(AIGateway.from_environment(), items, max_topics=args.topics)
        saved = store.save(result.candidates, considered_source_ids=[item.id for item in items])
    print(
        f"Создано тем: {len(saved.topic_ids)}; "
        f"обработано источников: {saved.sources_marked}; "
        f"расход: {result.ai_result.estimated_cost_rub:.4f} ₽"
    )
    if result.ai_result.budget_warning:
        from ai_budget import BudgetSettings

        warning = BudgetSettings.from_mapping(os.environ).warning_rub
        print(f"Внимание: расход AI за месяц превысил {warning:.0f} ₽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
