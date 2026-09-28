#!/usr/bin/env python3
"""Собирает состояние проекта из структуры репозитория.

Запуск:  python scripts/status.py
Пишет STATUS.md в корень и печатает сводку в терминал.
Ничего не требует, кроме стандартной библиотеки.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Темп публикации из плана: фаза 1 — 4 рилс и 2 поста в неделю.
RATE = {"reels": 4, "tg": 2}

# Норма запаса в неделях. Перед длинным перерывом автора поднять до 10.
BUFFER_TARGET = 4
BUFFER_ALARM = 2

# Сколько дней без голосового считаем тревогой.
VOICE_ALARM_DAYS = 10

# Сколько дней материал может ждать проверки, пока это норма.
REVIEW_ALARM_DAYS = 5


@dataclass
class Item:
    path: Path
    track: str
    status: str
    created: date | None
    what_to_check: str


def parse_frontmatter(text: str) -> dict[str, str]:
    """Минимальный разбор YAML-фронтматтера без внешних зависимостей."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fields: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields


def parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def collect(track: str) -> list[Item]:
    items: list[Item] = []
    base = ROOT / track
    if not base.exists():
        return items
    for status_dir in base.iterdir():
        if not status_dir.is_dir():
            continue
        for path in sorted(status_dir.glob("*.md")):
            fm = parse_frontmatter(path.read_text(encoding="utf-8"))
            items.append(
                Item(
                    path=path,
                    track=track,
                    status=status_dir.name,
                    created=parse_date(fm.get("создан", "")),
                    what_to_check=fm.get("что_проверить", ""),
                )
            )
    return items


def ready_count(items: list[Item], track: str) -> int:
    """Материалы, готовые к публикации.

    Для рилс готовым считается только смонтированное: approved — это
    проверенный сценарий, а не готовый ролик.
    """
    ready_statuses = {"edited"} if track == "reels" else {"approved"}
    return sum(1 for i in items if i.status in ready_statuses)


def days_since_last_voice() -> int | None:
    voice_dir = ROOT / "core" / "voice"
    if not voice_dir.exists():
        return None
    files = [
        p for p in voice_dir.iterdir()
        if p.is_file() and not p.name.startswith(".")
    ]
    if not files:
        return None
    newest = max(p.stat().st_mtime for p in files)
    return (datetime.now() - datetime.fromtimestamp(newest)).days


def open_decisions() -> list[str]:
    """Строки из таблицы решений CONTEXT.md, которые ещё не закрыты."""
    context = ROOT / "CONTEXT.md"
    if not context.exists():
        return []
    rows = []
    for line in context.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or "закрыто" in line or "принято" in line:
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 3 and cells[2] and cells[2] not in {"Статус", "---"}:
            rows.append(f"{cells[1]} — {cells[2]}")
    return rows


def flag(ok: bool, warn: bool = False) -> str:
    if warn:
        return "!"
    return " " if ok else "!"


def pipeline_snapshot() -> list[str]:
    """Состояние контентного конвейера из PostgreSQL.

    Материалы, темы и очередь публикаций живут в базе, а отчёт собирался
    только по папкам — и показывал пустую очередь при полной базе. Если
    базы под рукой нет (обычный запуск на маке), блок молча пропускается:
    ставить psycopg ради отчёта не нужно.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return []
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from database import DatabaseSettings, connect
    except Exception:  # noqa: BLE001 — модуль psycopg может быть не установлен
        return ["- база: драйвер psycopg не установлен"]

    queries = (
        ("материалы", "SELECT status, count(*) FROM materials GROUP BY status"),
        (
            "публикации",
            "SELECT status, count(*) FROM publications "
            "WHERE status <> 'published' GROUP BY status",
        ),
        ("темы", "SELECT status, count(*) FROM topics GROUP BY status"),
    )
    lines: list[str] = []
    try:
        with connect(DatabaseSettings.from_environment()) as connection:
            for label, query in queries:
                with connection.cursor() as cursor:
                    cursor.execute(query)
                    rows = cursor.fetchall()
                summary = ", ".join(f"{status}: {count}" for status, count in sorted(rows))
                lines.append(f"- {label} — {summary or 'пусто'}")
    except Exception as error:  # noqa: BLE001 — отчёт не должен падать из-за базы
        return [f"- база недоступна: {type(error).__name__}"]
    return lines


def build_report() -> str:
    today = date.today()
    lines = [f"# STATUS — {today.isoformat()}", ""]

    tracks = {t: collect(t) for t in ("reels", "tg")}
    all_items = [i for items in tracks.values() for i in items]

    # --- Запас ---
    weeks = []
    lines.append("## Запас")
    lines.append("")
    for track, items in tracks.items():
        ready = ready_count(items, track)
        w = ready / RATE[track] if RATE[track] else 0
        weeks.append(w)
        lines.append(f"- {track}: {ready} шт, это {w:.1f} нед при темпе {RATE[track]}/нед")
    buffer_weeks = min(weeks) if weeks else 0
    lines.append("")
    if buffer_weeks < BUFFER_ALARM:
        lines.append(f"**Запас {buffer_weeks:.1f} нед — ниже критического порога {BUFFER_ALARM}.** Канал встанет при первом же выпадении автора.")
    elif buffer_weeks < BUFFER_TARGET:
        lines.append(f"Запас {buffer_weeks:.1f} нед при норме {BUFFER_TARGET}. Нужен батч.")
    else:
        lines.append(f"Запас {buffer_weeks:.1f} нед — в норме.")
    lines.append("")

    # --- Очередь на проверку ---
    waiting = [i for i in all_items if i.status == "review"]
    lines.append("## Ждёт проверки автором")
    lines.append("")
    if not waiting:
        lines.append("Очередь пуста.")
    else:
        oldest_days = 0
        for item in waiting:
            age = (today - item.created).days if item.created else 0
            oldest_days = max(oldest_days, age)
            note = f" — {item.what_to_check}" if item.what_to_check else ""
            lines.append(f"- {item.path.name} ({age} дн){note}")
        lines.append("")
        if oldest_days >= REVIEW_ALARM_DAYS:
            lines.append(f"**Самый старый ждёт {oldest_days} дн.** Конвейер стоит на проверке, а не на производстве.")
    lines.append("")

    # --- Фактура ---
    lines.append("## Фактура")
    lines.append("")
    days = days_since_last_voice()
    if days is None:
        lines.append("Голосовых нет вообще.")
    elif days >= VOICE_ALARM_DAYS:
        lines.append(f"**Последнее голосовое {days} дн назад.** Если при этом материалы выходят — автор ушла в продакшн, проверьте, не съел ли монтаж её часы.")
    else:
        lines.append(f"Последнее голосовое {days} дн назад.")
    lines.append("")

    # --- Открытые решения ---
    decisions = open_decisions()
    lines.append("## Открытые решения")
    lines.append("")
    if decisions:
        lines.extend(f"- {d}" for d in decisions)
    else:
        lines.append("Нет.")
    lines.append("")

    # --- По статусам ---
    lines.append("## Материалы по статусам")
    lines.append("")
    for track, items in tracks.items():
        counts: dict[str, int] = {}
        for i in items:
            counts[i.status] = counts.get(i.status, 0) + 1
        summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "пусто"
        lines.append(f"- {track} — {summary}")

    snapshot = pipeline_snapshot()
    if snapshot:
        lines.append("")
        lines.append("## Конвейер в базе")
        lines.append("")
        lines.extend(snapshot)

    return "\n".join(lines) + "\n"


def main() -> None:
    report = build_report()
    (ROOT / "STATUS.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
