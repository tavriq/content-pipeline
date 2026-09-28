#!/usr/bin/env python3
"""Публикация поста в Telegram-канал.

    python3 tg/publish.py tg/approved/t-001.md --dry-run   показать, что уйдёт
    python3 tg/publish.py tg/approved/t-001.md             опубликовать
    python3 tg/publish.py --next --dry-run                 самый старый готовый

Публикует только материал, который прошёл линтер и одобрен автором. Без
отметки проверки — отказ. После публикации файл переезжает в published/,
во фронтматтер пишутся дата и message_id.

Токен и канал берутся из .env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPROVED = ROOT / "tg" / "approved"
PUBLISHED = ROOT / "tg" / "published"
LINT = ROOT / "scripts" / "lint.py"

API = "https://api.telegram.org/bot{token}/sendMessage"

# Служебные строки формата материала: в канал уходит только текст поста.
SECTION_PREFIXES = (
    "ЗАГОЛОВОК:",
    "ТЕКСТ:",
    "ВЫВОД:",
    "ВОВЛЕЧЕНИЕ:",
    "ДИСКЛЕЙМЕР:",
)


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fields: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip('"').strip("'")
    return fields, text[end + 4 :].lstrip("\n")


def set_field(text: str, key: str, value: str) -> str:
    if not text.startswith("---"):
        return f"---\n{key}: {value}\n---\n\n{text}"
    end = text.find("\n---", 3)
    if end == -1:
        return text
    header = text[3:end].splitlines()
    for index, line in enumerate(header):
        if line.split(":", 1)[0].strip() == key:
            header[index] = f"{key}: {value}"
            break
    else:
        header.append(f"{key}: {value}")
    return "---" + "\n".join(header) + "\n---" + text[end + 4 :]


def to_post(body: str) -> str:
    """Служебная разметка материала → текст, который увидит читатель."""
    blocks: list[str] = []
    for raw in body.split("\n\n"):
        block = raw.strip()
        if not block:
            continue
        for prefix in SECTION_PREFIXES:
            if block.startswith(prefix):
                block = block[len(prefix) :].strip()
                break
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def lint_ok(path: Path) -> bool:
    result = subprocess.run(
        [sys.executable, str(LINT), str(path)], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(result.stdout.strip())
    return result.returncode == 0


def checks(path: Path, fields: dict[str, str]) -> list[str]:
    problems = []
    # Сценарий рилса проходил все остальные проверки и уезжал в канал
    # вместе с раскадровкой и хештегами, а файл переезжал в tg/published.
    track = fields.get("track", "")
    if track and track != "tg":
        problems.append(f"это материал трека «{track}», а не Telegram-пост")
    if "tg" not in path.resolve().parts:
        problems.append("файл лежит вне tg/: публикуются только посты канала")
    if fields.get("статус") != "approved":
        problems.append(f"статус «{fields.get('статус', 'нет')}», нужен approved")
    if not fields.get("проверено_автором"):
        problems.append("нет отметки проверки автором")
    if fields.get("результат_проверки") not in {"ок", "ok"}:
        problems.append(f"результат проверки «{fields.get('результат_проверки', 'нет')}»")
    if not lint_ok(path):
        problems.append("не прошёл линтер")
    return problems


def send(token: str, channel: str, text: str) -> int:
    payload = urllib.parse.urlencode(
        {
            "chat_id": channel,
            "text": text,
            # Без разметки: markdown в тексте поста даёт сюрпризы на
            # подчёркиваниях и звёздочках.
            "disable_web_page_preview": "true",
        }
    ).encode()

    request = urllib.request.Request(API.format(token=token), data=payload)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            answer = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        sys.exit(f"publish: телеграм отказал ({error.code}): {detail}")
    except urllib.error.URLError as error:
        sys.exit(f"publish: сеть недоступна — {error.reason}")

    if not answer.get("ok"):
        sys.exit(f"publish: телеграм отказал: {answer}")
    return answer["result"]["message_id"]


def pick_next() -> Path | None:
    items = sorted(APPROVED.glob("*.md")) if APPROVED.exists() else []
    return items[0] if items else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Публикация поста в Telegram.")
    parser.add_argument("path", nargs="?", help="файл из tg/approved/")
    parser.add_argument("--next", action="store_true", help="взять первый готовый")
    parser.add_argument("--dry-run", action="store_true", help="показать и не отправлять")
    args = parser.parse_args()

    if args.next:
        path = pick_next()
        if path is None:
            print("В tg/approved/ пусто.")
            return 0
    elif args.path:
        path = Path(args.path)
        if not path.is_file():
            sys.exit(f"publish: нет файла {path}")
    else:
        parser.error("укажите файл или ключ --next")

    fields, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    problems = checks(path, fields)
    text = to_post(body)

    if args.dry_run:
        print(f"── {path.name} ──\n")
        print(text)
        print(f"\n── {len(text)} знаков ──")
        if problems:
            print("\nПубликация будет отклонена:")
            for item in problems:
                print(f"  - {item}")
            return 1
        print("\nПроверки пройдены, готов к публикации.")
        return 0

    if problems:
        print(f"Отказ публиковать {path.name}:")
        for item in problems:
            print(f"  - {item}")
        return 1

    config = load_env()
    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID") if not config.get(k)]
    if missing:
        sys.exit(f"publish: в .env не хватает {', '.join(missing)}")

    message_id = send(config["TELEGRAM_BOT_TOKEN"], config["TELEGRAM_CHANNEL_ID"], text)

    updated = path.read_text(encoding="utf-8")
    updated = set_field(updated, "статус", "published")
    updated = set_field(updated, "опубликовано", date.today().isoformat())
    updated = set_field(updated, "message_id", str(message_id))
    path.write_text(updated, encoding="utf-8")

    PUBLISHED.mkdir(parents=True, exist_ok=True)
    target = PUBLISHED / path.name
    shutil.move(str(path), str(target))

    print(f"Опубликовано: message_id {message_id}")
    print(f"Файл: {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
