#!/usr/bin/env python3
"""Реестр тем: посмотреть свободные, занять, закрыть.

Правит core/topics.yaml построчно, сохраняя форматирование и комментарии.
Внешних зависимостей нет — pyyaml в системе не стоит и не нужен.

    python3 scripts/topics.py list                    свободные темы
    python3 scripts/topics.py list --all              все, со статусами
    python3 scripts/topics.py list --block чекапы     фильтр по блоку
    python3 scripts/topics.py take 8 reels/queue/r-008.md    занять тему
    python3 scripts/topics.py done 8                  тема использована
    python3 scripts/topics.py free 8                  вернуть в свободные

Код возврата: 0 — успех, 1 — тема не найдена или занята, 2 — ошибка файла.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOPICS = ROOT / "core" / "topics.yaml"

FREE = "свободна"
TAKEN = "в_работе"
USED = "использована"
STATUSES = (FREE, TAKEN, USED)


class Topic:
    __slots__ = ("id", "block", "title", "status", "used_in", "lines")

    def __init__(self, topic_id: int, start: int) -> None:
        self.id = topic_id
        self.block = ""
        self.title = ""
        self.status = ""
        self.used_in = ""
        # Номера строк полей в файле, чтобы править на месте.
        self.lines: dict[str, int] = {}


def load(path: Path) -> tuple[list[str], list[Topic]]:
    if not path.exists():
        sys.exit(f"topics: нет файла реестра: {path}")

    lines = path.read_text(encoding="utf-8").splitlines()
    topics: list[Topic] = []
    current: Topic | None = None

    for number, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue

        if stripped.startswith("- id:"):
            try:
                current = Topic(int(stripped.split(":", 1)[1].strip()), number)
            except ValueError:
                sys.exit(f"topics: {path}:{number + 1}: id не число")
            topics.append(current)
            continue

        if current is None or ":" not in stripped:
            continue

        key, _, value = stripped.partition(":")
        key, value = key.strip(), value.strip()
        current.lines[key] = number
        if key == "блок":
            current.block = value
        elif key == "тема":
            current.title = value
        elif key == "статус":
            current.status = value
        elif key == "где_использована":
            current.used_in = value

    if not topics:
        sys.exit(f"topics: реестр пуст: {path}")
    return lines, topics


def save(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def find(topics: list[Topic], topic_id: int) -> Topic:
    for topic in topics:
        if topic.id == topic_id:
            return topic
    sys.exit(f"topics: темы {topic_id} в реестре нет")


def set_field(lines: list[str], topic: Topic, key: str, value: str) -> None:
    number = topic.lines.get(key)
    if number is None:
        sys.exit(f"topics: у темы {topic.id} нет поля «{key}»")
    indent = len(lines[number]) - len(lines[number].lstrip())
    lines[number] = f"{' ' * indent}{key}: {value}".rstrip()


def cmd_list(topics: list[Topic], show_all: bool, block: str | None) -> int:
    rows = [t for t in topics if show_all or t.status == FREE]
    if block:
        rows = [t for t in rows if t.block == block]

    if not rows:
        print("Подходящих тем нет.")
        return 0

    for topic in rows:
        mark = {FREE: " ", TAKEN: "~", USED: "x"}.get(topic.status, "?")
        tail = f"  → {topic.used_in}" if topic.used_in else ""
        print(f"[{mark}] {topic.id:>2}  {topic.block:<20} {topic.title}{tail}")

    if show_all:
        counts = {s: sum(1 for t in topics if t.status == s) for s in STATUSES}
        print()
        print(", ".join(f"{s}: {n}" for s, n in counts.items()))
    return 0


def cmd_take(path: Path, lines: list[str], topics: list[Topic], topic_id: int, target: str) -> int:
    topic = find(topics, topic_id)
    if topic.status != FREE:
        print(f"Тема {topic_id} не свободна: {topic.status} {topic.used_in}".rstrip())
        return 1
    set_field(lines, topic, "статус", TAKEN)
    set_field(lines, topic, "где_использована", target)
    save(path, lines)
    print(f"Тема {topic_id} занята: {topic.title} → {target}")
    return 0


def cmd_done(path: Path, lines: list[str], topics: list[Topic], topic_id: int) -> int:
    topic = find(topics, topic_id)
    if topic.status == USED:
        print(f"Тема {topic_id} уже использована.")
        return 0
    set_field(lines, topic, "статус", USED)
    save(path, lines)
    print(f"Тема {topic_id} закрыта: {topic.title}")
    return 0


def cmd_free(path: Path, lines: list[str], topics: list[Topic], topic_id: int) -> int:
    topic = find(topics, topic_id)
    set_field(lines, topic, "статус", FREE)
    set_field(lines, topic, "где_использована", "")
    save(path, lines)
    print(f"Тема {topic_id} снова свободна: {topic.title}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Реестр тем блога.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="показать темы")
    p_list.add_argument("--all", action="store_true", help="включая занятые и использованные")
    p_list.add_argument("--block", help="фильтр по блоку")

    p_take = sub.add_parser("take", help="занять тему под материал")
    p_take.add_argument("id", type=int)
    p_take.add_argument("path", help="путь к материалу")

    p_done = sub.add_parser("done", help="пометить тему использованной")
    p_done.add_argument("id", type=int)

    p_free = sub.add_parser("free", help="вернуть тему в свободные")
    p_free.add_argument("id", type=int)

    args = parser.parse_args()
    lines, topics = load(TOPICS)

    if args.command == "list":
        return cmd_list(topics, args.all, args.block)
    if args.command == "take":
        return cmd_take(TOPICS, lines, topics, args.id, args.path)
    if args.command == "done":
        return cmd_done(TOPICS, lines, topics, args.id)
    if args.command == "free":
        return cmd_free(TOPICS, lines, topics, args.id)
    return 2


if __name__ == "__main__":
    sys.exit(main())
