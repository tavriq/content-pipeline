#!/usr/bin/env python3
"""Выгрузка одобренного материала из базы в файл репозитория.

Telegram-пост публикуется сам и живёт в базе. Сценарий Reels или TikTok
дальше идёт руками: съёмка, озвучка, монтаж, загрузка. Пока он лежал
только в PostgreSQL, эта работа была не видна никому — ни автору в
списке дел, ни монтажёру, ни `status.py`. Reels при этом единственный
источник новой аудитории.

Поэтому одобренный сценарий выгружается файлом: база остаётся рабочим
состоянием, git — архивом и рабочим местом продакшна.
"""

from __future__ import annotations

import re
from datetime import date
import logging
from pathlib import Path

from lint import hard_violations

ROOT = Path(__file__).resolve().parent.parent

log = logging.getLogger("material_export")

# Куда кладём и с каким префиксом нумеруем. Telegram выгружается уже
# опубликованным: база держит рабочее состояние, git — архив.
TRACKS = {
    "reels": ("reels", "r"),
    "tiktok": ("reels", "k"),
    "telegram": ("tg", "t"),
}

NUMBER = re.compile(r"^([rkt])-(\d+)")


def next_number(prefix: str, track_dir: str) -> str:
    """Следующий свободный номер по всем папкам статуса трека."""
    used = [0]
    base = ROOT / track_dir
    for path in base.glob("*/*.md"):
        match = NUMBER.match(path.stem)
        if match and match.group(1) == prefix:
            used.append(int(match.group(2)))
    return f"{prefix}-{max(used) + 1:03d}"


def render(
    *,
    file_id: str,
    material_id: str,
    format_name: str,
    version: int,
    title: str,
    sources: tuple[dict[str, str], ...],
    body: str,
    status: str = "approved",
    extra: dict[str, str] | None = None,
    approved_on: date | None = None,
) -> str:
    approved = (approved_on or date.today()).isoformat()
    источник = "; ".join(
        f"{item.get('source', 'источник')}: {item.get('url', '')}".strip()
        for item in sources
    )
    header = [
        "---",
        f"id: {file_id}",
        f"track: {format_name}",
        f"материал: {material_id}",
        f"версия: {version}",
        f"статус: {status}",
        f"тема: {title}",
        f'источник: "{источник}"',
        "требует_проверки: false",
        'что_проверить: ""',
        f"проверено_автором: {approved}",
        "результат_проверки: ок",
        f"создан: {approved}",
    ]
    header += [f"{key}: {value}" for key, value in (extra or {}).items()]
    header += ["---", ""]
    return "\n".join(header) + body.strip() + "\n"


def export(
    *,
    material_id: str,
    format_name: str,
    version: int,
    title: str,
    sources: tuple[dict[str, str], ...],
    body: str,
    status: str = "approved",
    extra: dict[str, str] | None = None,
) -> Path:
    """Кладёт материал в `<трек>/<статус>/` и возвращает путь.

    Линтер прогоняется ещё раз: файл уезжает в репозиторий, а pre-commit
    в контейнере не установлен — коммит делает бот напрямую.
    """
    if format_name not in TRACKS:
        raise ValueError("Неизвестный формат материала")
    violations = hard_violations(body, material_id)
    if violations:
        raise ValueError(
            "Сценарий не проходит красные линии: "
            + "; ".join(item.split("] ", 1)[-1] for item in violations[:3])
        )

    track_dir, prefix = TRACKS[format_name]
    file_id = next_number(prefix, track_dir)
    target = ROOT / track_dir / status / f"{file_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        render(
            file_id=file_id,
            material_id=material_id,
            format_name=format_name,
            version=version,
            title=title,
            sources=sources,
            body=body,
            status=status,
            extra=extra,
        ),
        encoding="utf-8",
    )
    return target


def export_from_environment(
    material_id: str,
    *,
    status: str = "approved",
    extra: dict[str, str] | None = None,
) -> Path:
    """То же, но данные берутся из базы. Вызывается ботом после одобрения."""
    from content_workflow import (  # локально: бот тянет базу лениво
        _store_from_environment,
    )

    connection, store = _store_from_environment()
    with connection:
        material = store.current_material(material_id)
        if material is None:
            raise ValueError("Материал не найден")
        topic_id, format_name, body, version = material
        topic = store.topic_context(topic_id)
    if topic is None:
        raise ValueError("Тема материала не найдена")
    extras = dict(extra or {})
    if topic.rubric:
        extras.setdefault("рубрика", topic.rubric)
    if topic.niche:
        extras.setdefault("ниша", topic.niche)
    target = export(
        material_id=material_id,
        format_name=format_name,
        version=version,
        title=topic.title,
        sources=topic.sources,
        body=body,
        status=status,
        extra=extras,
    )
    mark_registry_topic(topic.id, target)
    return target


def mark_registry_topic(topic_id: str, target: Path) -> None:
    """Отметить тему реестра занятой в core/topics.yaml.

    Реестр остаётся файлом, который человек читает и правит; база знает про
    ту же тему своё. Без этой отметки одна тема ушла бы в работу дважды —
    один раз через бота, другой через `topics.py take`.
    """
    if not topic_id.startswith("registry-"):
        return
    try:
        registry_id = int(topic_id.split("-", 1)[1])
    except ValueError:
        return
    import topics as registry

    relative = target.relative_to(ROOT).as_posix()
    try:
        lines, entries = registry.load(registry.TOPICS)
        entry = registry.find(entries, registry_id)
        registry.set_field(lines, entry, "статус", registry.USED)
        registry.set_field(lines, entry, "где_использована", relative)
        registry.save(registry.TOPICS, lines)
    except Exception:  # noqa: BLE001 — материал уже выгружен, отметка вторична
        log.warning("тема %s не отмечена в реестре", topic_id, exc_info=True)
