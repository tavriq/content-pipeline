#!/usr/bin/env python3
"""Детерминированный линтер красных линий.

Проверяет тексты по стоп-листу core/stop-words.txt. Не «умная проверка»,
а тупой фильтр, который срабатывает всегда.

Запуск:
    python3 scripts/lint.py tg/queue/post.md      один файл
    python3 scripts/lint.py reels/queue           папку целиком
    python3 scripts/lint.py --staged              staged-файлы, для pre-commit

Материал может точечно отключить правило — во фронтматтере, по id
правила из стоп-листа:

    линтер_исключения: [гормональный-сбой, сила-воли]

Правила с «!» в начале строки не отключаются никогда.

Код возврата: 0 — чисто, 1 — есть срабатывания, 2 — ошибка запуска.
Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STOP_WORDS = ROOT / "core" / "stop-words.txt"

# Материалы, и только они. Правила, промпт и доки сами полны стоп-слов —
# при обходе папки и при --staged они не проверяются, иначе линтер
# заблокирует коммит собственного стоп-листа.
MATERIAL_DIRS = ("tg", "reels")
MATERIAL_SUFFIX = ".md"

# Хвост строки стоп-листа: два пробела, решётка, id, вертикальная черта,
# пояснение.
COMMENT = re.compile(r"\s{2,}#\s*([^|]+?)\s*\|\s*(.*)$")

EXCLUSIONS_KEY = "линтер_исключения"


def fail(message: str) -> "NoReturn":  # noqa: F821
    """Ошибка запуска — код 2. Срабатывание правила — код 1, это разные вещи."""
    print(message, file=sys.stderr)
    raise SystemExit(2)

# Всё ниже этого маркера не проверяется. Нужен для правок автора: она
# пишет разработчику «убери отсюда дозировку», и это законный текст,
# который никогда не попадёт в публикацию.
IGNORE_BELOW = "lint:ignore-below"


class Rule:
    __slots__ = ("id", "pattern", "note", "hard")

    def __init__(self, rule_id: str, pattern: re.Pattern[str], note: str, hard: bool) -> None:
        self.id = rule_id
        self.pattern = pattern
        self.note = note
        self.hard = hard


def load_rules(path: Path) -> list[Rule]:
    if not path.exists():
        fail(f"lint: нет файла со стоп-листом: {path}")

    rules: list[Rule] = []
    seen: dict[str, int] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        match = COMMENT.search(raw)
        if not match:
            fail(f"lint: {path}:{number}: нет хвоста «# id | пояснение»")
        expression = raw[: match.start()].strip()
        rule_id, note = match.group(1), match.group(2)

        if rule_id in seen:
            fail(f"lint: {path}:{number}: id «{rule_id}» уже занят строкой {seen[rule_id]}")
        seen[rule_id] = number

        hard = expression.startswith("!")
        if hard:
            expression = expression[1:]

        try:
            rules.append(Rule(rule_id, re.compile(expression, re.IGNORECASE), note, hard))
        except re.error as error:
            fail(f"lint: {path}:{number}: битая регулярка — {error}")

    if not rules:
        fail(f"lint: стоп-лист пуст: {path}")
    return rules


def parse_exclusions(text: str) -> list[str]:
    """Читает «линтер_исключения» из фронтматтера. Оба вида YAML-списка."""
    if not text.startswith("---"):
        return []
    end = text.find("\n---", 3)
    if end == -1:
        return []

    values: list[str] = []
    collecting = False
    for line in text[3:end].splitlines():
        stripped = line.strip()
        if collecting:
            if stripped.startswith("- "):
                values.append(stripped[2:].strip().strip("\"'"))
                continue
            if stripped and not line.startswith((" ", "\t")):
                collecting = False
            if collecting and not stripped:
                continue
        if stripped.startswith(f"{EXCLUSIONS_KEY}:"):
            inline = stripped.split(":", 1)[1].strip()
            if inline.startswith("[") and inline.endswith("]"):
                values.extend(
                    part.strip().strip("\"'")
                    for part in inline[1:-1].split(",")
                    if part.strip()
                )
            elif inline:
                values.append(inline.strip("\"'"))
            else:
                collecting = True
    return [v for v in values if v]


def is_material(path: Path) -> bool:
    """Материал ли это — по расположению в дереве репозитория."""
    try:
        parts = path.resolve().relative_to(ROOT).parts
    except ValueError:
        return False
    return len(parts) > 1 and parts[0] in MATERIAL_DIRS and path.suffix == MATERIAL_SUFFIX


def _paragraphs(text: str) -> list[tuple[str, list[tuple[int, int]]]]:
    """Абзацы, склеенные в одну строку, и карта «позиция → номер строки».

    Стоп-лист написан словосочетаниями, а материалы жёстко перенесены по
    ширине колонки. При построчной проверке «как\nэндокринолог я» не ловит
    ни одно правило — мимо проходит половина неотключаемых, включая
    дозировки и статус автора. Границу абзаца не склеиваем: конец одной
    мысли и начало другой дали бы срабатывание на ровном месте.
    """
    blocks: list[tuple[str, list[tuple[int, int]]]] = []
    current: list[str] = []
    anchors: list[tuple[int, int]] = []
    length = 0

    def flush() -> None:
        nonlocal current, anchors, length
        if current:
            blocks.append((" ".join(current), anchors))
        current, anchors, length = [], [], 0

    for number, line in enumerate(text.splitlines(), 1):
        if IGNORE_BELOW in line:
            break
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        anchors.append((length, number))
        length += len(stripped) + 1
        current.append(stripped)
    flush()
    return blocks


def _line_of(position: int, anchors: list[tuple[int, int]]) -> int:
    number = anchors[0][1] if anchors else 1
    for start, line_number in anchors:
        if start > position:
            break
        number = line_number
    return number


def check(text: str, label: str, rules: list[Rule]) -> tuple[list[str], list[str]]:
    """Отчёт по одному тексту: срабатывания и заметки об исключениях."""
    by_id = {rule.id: rule for rule in rules}
    silenced: set[str] = set()
    notes: list[str] = []

    for item in parse_exclusions(text):
        rule = by_id.get(item)
        if rule is None:
            notes.append(f"{label}: нет правила с id «{item}», см. core/stop-words.txt")
            continue
        if rule.hard:
            notes.append(f"{label}: правило «{item}» неотключаемое, исключение проигнорировано")
            continue
        silenced.add(item)
        notes.append(f"{label}: отключено правило «{item}» — {rule.note}")

    found_hits: list[tuple[int, str]] = []
    for block, anchors in _paragraphs(text):
        for rule in rules:
            if rule.id in silenced:
                continue
            for found in rule.pattern.finditer(block):
                number = _line_of(found.start(), anchors)
                fragment = " ".join(found.group(0).split())
                found_hits.append(
                    (number, f"{label}:{number}: [{rule.id}: {rule.note}] «{fragment}»")
                )
    found_hits.sort(key=lambda item: item[0])
    return [line for _, line in found_hits], notes


_RULES: list[Rule] | None = None


def default_rules() -> list[Rule]:
    """Правила из стоп-листа, разобранные один раз на процесс."""
    global _RULES
    if _RULES is None:
        _RULES = load_rules(STOP_WORDS)
    return _RULES


def check_text(text: str, label: str = "текст") -> tuple[list[str], list[str]]:
    """Проверка текста, которого нет на диске. Точка входа для кода, не CLI.

    Нужна конвейеру: материал живёт в PostgreSQL и файла у него нет, а
    проверять красные линии всё равно обязаны — и до показа автору, и
    перед отправкой в канал.
    """
    return check(text, label, default_rules())


def hard_violations(text: str, label: str = "текст") -> list[str]:
    """Только неотключаемые правила: разделы 1 и 2 красных линий.

    Отключаемые правила (вес, штампы) требуют фронтматтера с исключениями,
    а у материала из базы его нет — разбор мифа обязан миф процитировать.
    Поэтому машина жёстко блокирует по разделам 1–2, остальное показывает
    человеку.
    """
    hits, _ = check(text, label, [rule for rule in default_rules() if rule.hard])
    return hits


def read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"lint: пропущен {path}: {error}", file=sys.stderr)
        return None


def collect_targets(paths: list[str]) -> list[Path]:
    """Явный файл проверяется всегда, папка — только по материалам внутри."""
    targets: list[Path] = []
    for item in paths:
        path = Path(item)
        if path.is_dir():
            targets.extend(sorted(p for p in path.rglob(f"*{MATERIAL_SUFFIX}") if p.is_file()))
        elif path.is_file():
            targets.append(path)
        else:
            fail(f"lint: нет такого пути: {item}")
    return targets


def staged_files() -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(f"lint: git вернул ошибку — {result.stderr.strip()}")
    names = [n for n in result.stdout.split("\0") if n]
    return [ROOT / n for n in names]


def staged_content(path: Path) -> str | None:
    """Содержимое из индекса, а не с диска: коммитится именно оно."""
    relative = path.resolve().relative_to(ROOT).as_posix()
    result = subprocess.run(
        ["git", "show", f":{relative}"],
        cwd=ROOT,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Проверка текстов по стоп-листу красных линий.",
        epilog="Обход хука — git commit --no-verify, но тогда объясните себе, зачем.",
    )
    parser.add_argument("paths", nargs="*", help="файлы или папки")
    parser.add_argument("--staged", action="store_true", help="проверить staged-материалы")
    args = parser.parse_args()

    if not args.paths and not args.staged:
        parser.error("укажите путь или ключ --staged")

    rules = load_rules(STOP_WORDS)
    hits: list[str] = []
    notes: list[str] = []
    checked = 0

    if args.staged:
        for path in staged_files():
            if not is_material(path):
                continue
            text = staged_content(path)
            if text is None:
                continue
            checked += 1
            file_hits, file_notes = check(text, path.resolve().relative_to(ROOT).as_posix(), rules)
            hits.extend(file_hits)
            notes.extend(file_notes)

    for path in collect_targets(args.paths):
        text = read_file(path)
        if text is None:
            continue
        checked += 1
        file_hits, file_notes = check(text, str(path), rules)
        hits.extend(file_hits)
        notes.extend(file_notes)

    # Отключённые правила показываются всегда: заглушка должна быть
    # заметна и в ревью, и в выводе хука.
    for note in notes:
        print(note)
    if notes:
        print()

    if hits:
        print(f"Красные линии — {len(hits)} срабатываний:\n")
        for line in hits:
            print(line)
        print("\nМатериал не публикуется. Правила: core/red-lines.md")
        return 1

    print(f"Чисто. Проверено файлов: {checked}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
