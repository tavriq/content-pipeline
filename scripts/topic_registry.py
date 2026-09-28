#!/usr/bin/env python3
"""Реестр тем блога как источник повестки наравне с потоком публикаций.

core/topics.yaml написан под аудиторию: «ТТГ в норме, а самочувствие
плохое», «Почему диагноза гормональный сбой не существует». Это вопросы,
которые человек уже задавал поиску. PubMed отдаёт другое — то, что вышло на
этой неделе, отсортированное по весомости для практикующего врача. Обе
повестки нужны, но первая в конвейере не участвовала вовсе.

Файл остаётся реестром: что писать и в каком статусе. База — рабочее
состояние: что взято в дайджест, что сгенерировано. Синхронизация
односторонняя, из файла в базу; обратно в файл статус возвращает
material_export при выгрузке одобренного материала.

    python3 scripts/topic_registry.py import     завести реестр в базу
    python3 scripts/topics.py list               свободные темы файла

Внешних зависимостей нет: pyyaml в проекте не стоит, реестр разбирается
построчно — тем же способом, что и в topics.py.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TOPICS_FILE = ROOT / "core" / "topics.yaml"

FREE = "свободна"
IN_WORK = "в_работе"
USED = "использована"

# Блоки реестра ложатся на ниши раздела 4 системного промпта. Регулирование
# отдельного веса в разделе 4 не имеет и относится к отстройке — это тот же
# разговор про «кто такие нутрициологи», что и в нише чекапов.
BLOCK_TO_NICHE = {
    "регулирование": "чекапы",
    "питание_и_вес": "питание",
    "чекапы": "чекапы",
    "женские_гормоны": "женские_гормоны",
    "щитовидная_железа": "щитовидка",
    "личное": "личное",
}

# Доли раздела 4. Используются, чтобы дайджест не уводило в одну нишу:
# при прочих равных наверх поднимается та, что недобрана за последние
# материалы.
NICHE_SHARE = {
    "питание": 0.30,
    "женские_гормоны": 0.25,
    "чекапы": 0.20,
    "щитовидка": 0.15,
    "личное": 0.10,
}

RUBRIC_NAMES = {
    "Р1": "разбор мифа",
    "Р2": "что означает показатель",
    "Р3": "когда пора к эндокринологу",
    "Р4": "изнутри медвуза",
    "Р5": "механизм простыми словами",
    "Р6": "вопрос из комментариев",
    "Р7": "что нового",
}


@dataclass(frozen=True)
class RegistryTopic:
    registry_id: int
    block: str
    rubric: str
    title: str
    status: str
    used_in: str

    @property
    def niche(self) -> str:
        return BLOCK_TO_NICHE.get(self.block, "чекапы")

    @property
    def rubric_name(self) -> str:
        return RUBRIC_NAMES.get(self.rubric, "разбор")

    @property
    def is_free(self) -> bool:
        return self.status == FREE


def parse_registry(text: str) -> list[RegistryTopic]:
    """Разобрать реестр построчно.

    Формат реестра плоский и стабильный: запись начинается с «- id:», поля
    идут «ключ: значение». Полноценный YAML тут не нужен, а зависимость от
    pyyaml стоила бы установки на машине, где локальные скрипты обходятся
    стандартной библиотекой.
    """
    topics: list[RegistryTopic] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if not current.get("id"):
            return
        topics.append(
            RegistryTopic(
                registry_id=int(current["id"]),
                block=current.get("блок", ""),
                rubric=current.get("рубрика", ""),
                title=current.get("тема", ""),
                status=current.get("статус", FREE),
                used_in=current.get("где_использована", ""),
            )
        )

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- id:"):
            flush()
            current = {"id": line.split(":", 1)[1].strip()}
            continue
        if ":" not in line or not current:
            continue
        key, value = line.split(":", 1)
        current[key.strip()] = value.strip().strip('"')
    flush()
    return topics


def load_registry(path: Path | None = None) -> list[RegistryTopic]:
    source = path or TOPICS_FILE
    return parse_registry(source.read_text(encoding="utf-8"))


class RegistryStore:
    """Перенос реестра в таблицу topics и обратная отметка занятости."""

    def __init__(self, connection: Any):
        self.connection = connection

    def sync(self, topics: list[RegistryTopic]) -> tuple[int, int]:
        """Завести свободные темы реестра в базу, занятые — закрыть.

        Возвращает пару: сколько тем доступно для дайджеста и сколько
        закрыто как уже использованные.
        """
        available = closed = 0
        try:
            with self.connection.cursor() as cursor:
                for topic in topics:
                    topic_id = f"registry-{topic.registry_id:03d}"
                    if topic.status == USED:
                        cursor.execute(
                            """
                            UPDATE topics
                               SET status = 'archived', selected_at = COALESCE(selected_at, now())
                             WHERE registry_id = %s AND status <> 'archived'
                            """,
                            (topic.registry_id,),
                        )
                        closed += 1
                        continue
                    # Тема в работе остаётся в базе, но в дайджест не идёт:
                    # материал по ней уже пишется.
                    status = "shown" if topic.status == IN_WORK else "candidate"
                    cursor.execute(
                        """
                        INSERT INTO topics (
                            id, title, summary, rationale, language, status, score,
                            origin, registry_id, rubric, niche
                        )
                        VALUES (%s, %s, %s, %s, 'ru', %s, %s, 'registry', %s, %s, %s)
                        ON CONFLICT (registry_id) WHERE registry_id IS NOT NULL
                        DO UPDATE SET
                            title = EXCLUDED.title,
                            rubric = EXCLUDED.rubric,
                            niche = EXCLUDED.niche,
                            last_seen_at = now()
                        """,
                        (
                            topic_id,
                            topic.title,
                            topic.title,
                            f"Тема плана блога, блок «{topic.block}», рубрика "
                            f"{topic.rubric} — {topic.rubric_name}.",
                            status,
                            registry_score(topic),
                            topic.registry_id,
                            topic.rubric,
                            topic.niche,
                        ),
                    )
                    if status == "candidate":
                        available += 1
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return available, closed

    def niche_counts(self, limit: int = 10) -> dict[str, int]:
        """Сколько материалов по каждой нише среди последних выпущенных."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT niche, count(*)
                  FROM (
                        SELECT niche FROM materials
                         WHERE niche IS NOT NULL
                         ORDER BY created_at DESC
                         LIMIT %s
                       ) AS recent
                 GROUP BY niche
                """,
                (limit,),
            )
            return {row[0]: int(row[1]) for row in cursor.fetchall()}


def registry_score(topic: RegistryTopic) -> float:
    """Базовый вес темы реестра.

    Реестровые темы не соревнуются со свежими публикациями по научной
    новизне — они отобраны людьми под конкретную аудиторию. Вес нужен
    только чтобы упорядочить их между собой: сначала ниши с большей долей
    в плане выпуска.
    """
    return round(50 + NICHE_SHARE.get(topic.niche, 0.1) * 100, 4)


def underweighted_niches(counts: dict[str, int], total: int) -> list[str]:
    """Ниши, недобранные против долей раздела 4, от самой отстающей.

    Пустая история — не повод считать все ниши равными: порядок всё равно
    задаётся планом, поэтому первым идёт питание, последним личное.
    """
    gaps: list[tuple[float, str]] = []
    for niche, share in NICHE_SHARE.items():
        expected = share * total if total else share * 10
        gaps.append((counts.get(niche, 0) - expected, niche))
    gaps.sort()
    return [niche for _, niche in gaps]


def sync_from_environment() -> tuple[int, int]:
    from database import DatabaseSettings, connect  # локально: файл сам по себе базы не требует

    connection = connect(DatabaseSettings.from_environment())
    try:
        return RegistryStore(connection).sync(load_registry())
    finally:
        connection.close()


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "import":
        print(__doc__.strip())
        return 2
    available, closed = sync_from_environment()
    print(f"Реестр в базе: {available} тем доступно, {closed} закрыто как использованные")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
