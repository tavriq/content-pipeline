#!/usr/bin/env python3
"""Фактчек-бот. Единственный интерфейс автора к конвейеру.

Автор не открывает репозиторий и не ставит git. Она открывает телеграм,
читает материал, жмёт «ок» или «правка». Всё остальное делает бот.

    python3 scripts/review_bot.py            обычный запуск
    python3 scripts/review_bot.py --check    проверка конфигурации без сети

Команды в телеграме:
    /review   прислать всё, что ждёт проверки
    /status   сколько материалов в очереди
    /cancel   выйти из режима ожидания правки, оно же словом «отмена»

Настройки берутся из .env: TELEGRAM_BOT_TOKEN, AUTHOR_CHAT_ID.
Логи — logs/bot.log. Состояние диалога — data/bot-state.json, переживает
перезапуск.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "bot-state.json"
LOG_FILE = ROOT / "logs" / "bot.log"
VOICE_DIR = ROOT / "core" / "voice"
TRACKS = ("tg", "reels")

# Bot API не отдаёт файлы больше двадцати мегабайт.
MAX_VOICE_BYTES = 20 * 1024 * 1024

# Во сколько раз расшифровка медленнее реального времени на CPU сервера.
# Нужно только для честной оценки ожидания в сообщении автору.
TRANSCRIBE_SLOWDOWN = 3

# Расшифровки идут по одной: две разом съедят память сервера.
transcribe_lock = asyncio.Lock()

# Git — единственное разделяемое состояние на диске. Раньше очередь
# обеспечивалась тем, что бот обрабатывал апдейты строго по одному; теперь
# долгие операции ушли в фоновые задачи, и порядок нужно держать явно.
git_lock = asyncio.Lock()
# Генерации также идут по одной: это не даёт двойному нажатию одновременно
# потратить бюджет и создавать конкурирующие версии одного материала.
generation_lock = asyncio.Lock()

# Раз в сутки, не чаще: напоминание не должно превращаться в долбёжку.
REMINDER_AFTER_DAYS = 3

# Всё расписание — по Москве, в одном поясе. Раньше пакет на фактчек и
# напоминание стояли в UTC: «по вторникам утром» на деле означало час дня.
REMINDER_HOUR = 11
DIGEST_HOUR = 10

# Дни недели считаем сами, через datetime.weekday() (0 — понедельник):
# нумерация days у run_daily менялась между версиями библиотеки, и
# проверить её на бою — значит узнать об ошибке через неделю.
REVIEW_DIGEST_WEEKDAY = 1  # вторник: пакет материалов на проверку фактов

# Темы автору — дважды в неделю. Ежедневный дайджест из десяти тем при
# плане четыре рилса и два поста забивает очередь быстрее, чем автор её
# разбирает; это первый риск проекта. Источники при этом
# собираются каждый день, чтобы темы оставались свежими.
TOPIC_DIGEST_WEEKDAYS = (0, 3)  # понедельник и четверг
DEFAULT_CONTENT_PIPELINE_HOUR = 8
DEFAULT_TOPIC_RETENTION_DAYS = 7
CONTENT_ERROR_COOLDOWN = timedelta(hours=1)
MOSCOW = ZoneInfo("Europe/Moscow")

TELEGRAM_LIMIT = 4096
CALLBACK_LIMIT_BYTES = 64

# Автор ничего не печатает: всё, что ей нужно, — кнопки под клавиатурой.
# Нажатие присылает боту ровно эту строку, поэтому тексты и есть команды.
BUTTON_REVIEW = "📋 Что на проверке"
BUTTON_TOPICS = "📰 Актуальные темы"
BUTTON_QUEUE = "🗓 Очередь публикаций"
BUTTON_CANCEL = "✖️ Не буду править"

# Telegram парсит команды только по [a-zA-Z0-9_], поэтому /отмена приходит
# обычным текстом. Без этого списка слово уехало бы в файл как текст правки.
CANCEL_WORDS = frozenset(
    {"отмена", "/отмена", "отменить", "cancel", "/cancel", BUTTON_CANCEL.casefold()}
)

# Коды рубрик — наши, внутренние. Автору показываем словами.
RUBRICS = {
    "Р1": "разбор мифа",
    "Р2": "что означает показатель",
    "Р3": "когда пора к эндокринологу",
    "Р4": "изнутри медвуза",
    "Р5": "механизм простыми словами",
    "Р6": "вопрос из комментариев",
    "Р7": "что нового",
}


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def callback_data(action: str, *parts: object) -> str:
    """Callback Telegram с явной проверкой ограничения в 64 байта."""
    value = ":".join((action, *(str(part) for part in parts)))
    if len(value.encode("utf-8")) > CALLBACK_LIMIT_BYTES:
        raise ValueError("callback_data Telegram длиннее 64 байт")
    return value


def parse_author_chat_ids(config: dict[str, str]) -> tuple[int, ...]:
    """Основной автор плюс явный список дополнительных Telegram chat ID."""
    raw_values = [config.get("AUTHOR_CHAT_ID", "")]
    raw_values.extend(re.split(r"[\s,;]+", config.get("AUTHOR_CHAT_IDS", "").strip()))
    result: list[int] = []
    for raw in raw_values:
        value = raw.strip()
        if not value:
            continue
        if not value.lstrip("-").isdigit():
            raise ValueError("AUTHOR_CHAT_IDS должен содержать только числовые ID")
        chat_id = int(value)
        # Отрицательный ID принадлежит группе или каналу. Пропустив его,
        # мы выдали бы права автора каждому участнику: одобрять материалы,
        # жечь бюджет генерации, ставить посты в публичный канал.
        if chat_id <= 0:
            raise ValueError(
                "ID автора должен быть личным чатом: отрицательный ID "
                "принадлежит группе, туда доступ выдавать нельзя"
            )
        if chat_id not in result:
            result.append(chat_id)
    if not result:
        raise ValueError("Нужен хотя бы один AUTHOR_CHAT_ID")
    return tuple(result)


def split_telegram_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Делит произвольный текст модели, сохраняя абзацы где возможно."""
    if limit <= 0:
        raise ValueError("limit должен быть больше нуля")
    remaining = text.strip()
    if not remaining:
        return []
    chunks: list[str] = []
    while len(remaining) > limit:
        boundary = remaining.rfind("\n\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = remaining.rfind("\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = remaining.rfind(" ", 0, limit + 1)
        if boundary <= 0:
            boundary = limit
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks

log = logging.getLogger("review_bot")


# ─────────────────────────── окружение ───────────────────────────


def load_env() -> dict[str, str]:
    """Читает .env без внешних зависимостей."""
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    # Переменные окружения важнее файла: так удобнее на VPS.
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "AUTHOR_CHAT_ID",
        "AUTHOR_CHAT_IDS",
        "DEV_CHAT_ID",
        "TELEGRAM_CHANNEL_ID",
        "TIMEWEB_AI_API_KEY",
        "TIMEWEB_AI_BASE_URL",
        "AI_MODEL_FAST",
        "AI_MODEL_WRITER",
        "AI_TIMEOUT_SECONDS",
        "AI_BUDGET_WARNING_RUB",
        "AI_BUDGET_HARD_LIMIT_RUB",
        "AI_PRICE_FAST_INPUT_RUB_PER_MILLION",
        "AI_PRICE_FAST_OUTPUT_RUB_PER_MILLION",
        "AI_PRICE_WRITER_INPUT_RUB_PER_MILLION",
        "AI_PRICE_WRITER_OUTPUT_RUB_PER_MILLION",
        "DATABASE_URL",
        "DATABASE_CONNECT_TIMEOUT_SECONDS",
        "TELEGRAM_PUBLISH_TARGET",
        "PUBLISH_AUTHOR_CHAT_ID",
        "CONTENT_PIPELINE_HOUR",
        "TOPIC_RETENTION_DAYS",
    ):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def apply_environment(values: dict[str, str]) -> None:
    """Передаёт прочитанный .env модулям, использующим from_environment()."""
    for key, value in values.items():
        os.environ.setdefault(key, value)


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ─────────────────────────── состояние ───────────────────────────


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        log.warning("состояние повреждено, начинаю с чистого: %s", error)
        return {}


def write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Через временный файл: обрыв на записи не должен оставить огрызок.
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def migrate_state(author_id: int) -> None:
    """Переносит состояние из корня файла в раздел users.

    До появления второго автора ключи лежали в корне. После обновления
    read_user_state их не видел: незакрытая правка терялась молча, а
    напоминание уходило в ближайший запуск, будто пакета не было.
    """
    state = read_state()
    if not state or "users" in state and not any(
        key in state for key in ("awaiting_edit", "last_digest", "last_reminder")
    ):
        return
    legacy = {
        key: state.pop(key)
        for key in ("awaiting_edit", "last_digest", "last_reminder")
        if key in state
    }
    if not legacy:
        return
    users = state.setdefault("users", {})
    current = dict(users.get(str(author_id), {}))
    for key, value in legacy.items():
        current.setdefault(key, value)
    users[str(author_id)] = current
    write_state(state)
    log.info("состояние перенесено в users для %s: %s", author_id, ", ".join(legacy))


def read_user_state(chat_id: int) -> dict:
    state = read_state()
    users = state.get("users")
    if not isinstance(users, dict):
        return {}
    value = users.get(str(chat_id))
    return dict(value) if isinstance(value, dict) else {}


def write_user_state(chat_id: int, user_state: dict) -> None:
    state = read_state()
    users = state.setdefault("users", {})
    if not isinstance(users, dict):
        users = {}
        state["users"] = users
    users[str(chat_id)] = user_state
    write_state(state)


# ─────────────────────────── материалы ───────────────────────────


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
    """Ставит поле во фронтматтер, добавляя его, если поля не было."""
    if not text.startswith("---"):
        return f"---\n{key}: {value}\n---\n\n{text}"
    end = text.find("\n---", 3)
    if end == -1:
        return text

    header = text[3:end].splitlines()
    replaced = False
    for index, line in enumerate(header):
        if line.split(":", 1)[0].strip() == key:
            indent = len(line) - len(line.lstrip())
            header[index] = f"{' ' * indent}{key}: {value}"
            replaced = True
            break
    if not replaced:
        header.append(f"{key}: {value}")
    return "---" + "\n".join(header) + "\n---" + text[end + 4 :]


def transcribe_audio(path: Path) -> Path:
    """Расшифровка одного файла. Синхронная, вызывается в отдельном потоке."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import transcribe as engine

    result = engine.transcribe_file(path, engine.MODEL, timestamps=False, force=True)
    if result is None:
        raise RuntimeError("расшифровка не вернула файл")
    return result


def waiting_materials() -> list[Path]:
    items: list[Path] = []
    for track in TRACKS:
        folder = ROOT / track / "review"
        if folder.exists():
            items.extend(sorted(folder.glob("*.md")))
    return items


def plural(count: int, one: str, few: str, many: str) -> str:
    """«1 материал», «2 материала», «5 материалов»."""
    if count % 100 in range(11, 20):
        return f"{count} {many}"
    last = count % 10
    if last == 1:
        return f"{count} {one}"
    if last in (2, 3, 4):
        return f"{count} {few}"
    return f"{count} {many}"


def headline(fields: dict[str, str], body: str, path: Path) -> str:
    """Заголовок материала человеческим языком.

    Автор видит не служебный t-001, а тему. У поста это строка
    ЗАГОЛОВОК, у рилса — ХУК: первое, что произносится в кадре.
    """
    for marker in ("ЗАГОЛОВОК:", "ТЕМА:", "ХУК:"):
        for line in body.splitlines():
            if line.strip().startswith(marker):
                value = line.split(":", 1)[1].strip()
                if value:
                    return value
    return fields.get("тема") or fields.get("id") or path.stem


def title_of(reference: str) -> str:
    """Заголовок материала по ссылке вида «tg/t-001.md».

    Файл мог уже уехать из review в approved или обратно в queue, поэтому
    ищем по всем папкам статуса — автору важна тема, а не где он лежит.
    """
    track, _, name = reference.partition("/")
    for folder in ("review", "approved", "queue", "published"):
        candidate = ROOT / track / folder / name
        if candidate.exists():
            fields, body = parse_frontmatter(candidate.read_text(encoding="utf-8"))
            return headline(fields, body.strip(), candidate)
    return Path(name).stem


def clean_source(value: str) -> str:
    """Убирает внутренние пути проекта: автору они ничего не говорят."""
    parts = [p.strip() for p in value.split(";")]
    kept = [p for p in parts if p and not re.search(r"\b\w+/[\w./-]+\.(md|ya?ml|py)\b", p)]
    return "; ".join(kept)


def describe(path: Path) -> str:
    """Сообщение автору: тема, что проверить, источник, текст.

    Разметка HTML, а не Markdown: текст материала произвольный, и одно
    подчёркивание в слове вроде «ТТГ_св» роняло отправку целиком — автор
    просто не получала материал и не могла узнать, почему.
    """
    fields, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    body = body.strip()
    kind = "Рилс" if fields.get("track") == "reels" else "Пост"
    rubric = RUBRICS.get(fields.get("рубрика", ""), "")
    title = headline(fields, body, path)

    # Заголовок уже вынесен наверх, а метка «ТЕКСТ:» после разделителя
    # ничего не сообщает — обе строки в теле только шумят.
    body = "\n".join(
        line
        for line in body.splitlines()
        if title not in line and line.strip() not in ("ТЕКСТ:", "ЗАГОЛОВОК:")
    ).strip()

    head = f"{kind} · {rubric}" if rubric else kind
    lines = [f"{esc(head)}\n<b>{esc(title)}</b>", ""]

    if fields.get("что_проверить"):
        lines += [f"❓ <b>Что проверить</b>\n{esc(fields['что_проверить'])}", ""]

    source = clean_source(fields.get("источник", ""))
    if source:
        lines += [f"📎 Источник: {esc(source)}", ""]

    lines += ["➖➖➖", esc(body)]
    text = "\n".join(lines)
    if len(text) > TELEGRAM_LIMIT - 100:
        text = text[: TELEGRAM_LIMIT - 140] + "\n\n<i>[текст обрезан, целиком — у разработчика]</i>"
    return text


# ─────────────────────────────── git ───────────────────────────────


# Порог, после которого index.lock считается осиротевшим. Своя git-команда
# живёт максимум две минуты (timeout в git()), а вот ручной `git pull` с
# хоста бот не видит вовсе: в контейнере pgrep показывает только свои
# процессы. Отсюда запас: лучше подождать десять минут, чем снести лок
# у живого пула, которым обновляют сервер.
STALE_LOCK_SECONDS = 600


def clear_stale_lock() -> None:
    """Снять index.lock, оставшийся от прерванной операции.

    Бот перезапускается посреди git-команды — например, при деплое, — и
    лок остаётся навсегда. Дальше не проходит ни один коммит: автор жмёт
    кнопки, ответы не записываются, а понять это можно только по логам.
    """
    lock = ROOT / ".git" / "index.lock"
    if not lock.exists():
        return
    if subprocess.run(["pgrep", "-x", "git"], capture_output=True).returncode == 0:
        return
    age = datetime.now().timestamp() - lock.stat().st_mtime
    if age < STALE_LOCK_SECONDS:
        return
    lock.unlink(missing_ok=True)
    log.warning("снят осиротевший index.lock, возраст %.0f с", age)


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=120
    )


def has_remote() -> bool:
    return bool(git("remote").stdout.strip())


def pull_rebase() -> bool:
    """Подтянуть чужие изменения. При конфликте — откатиться и сказать «нет».

    Без отката репозиторий остаётся в незавершённом rebase, и тогда
    ломается каждая следующая операция бота: коммит не проходит, кнопки
    отвечают ошибкой, чинить нужно руками по ssh. Лучше отступить и
    работать локально, пока человек не разберёт конфликт.
    """
    result = git("pull", "--rebase", "--autostash")
    if result.returncode == 0:
        return True

    log.error("pull не прошёл: %s", result.stderr.strip())
    if (ROOT / ".git" / "rebase-merge").exists() or (ROOT / ".git" / "rebase-apply").exists():
        abort = git("rebase", "--abort")
        log.error("rebase откачен, код %s. Конфликт разбирается руками", abort.returncode)
    return False


def sync_before_write() -> None:
    """Перед любой записью — подтянуть чужие изменения."""
    clear_stale_lock()
    if not has_remote():
        return
    pull_rebase()


# Что бот вправе класть в коммит. Раньше стояло «git add -A», и дважды
# это выстрелило: сначала pip-кэш на 124 МБ, потом веса whisper. Явный
# список дешевле любых .gitignore-заплаток.
COMMIT_PATHS = (
    "core/transcripts",
    "core/topics.yaml",
    "tg",
    "reels",
    "data/metrics.csv",
)


def commit_and_push(message: str) -> bool:
    clear_stale_lock()
    git("add", "--", *COMMIT_PATHS)
    status = git("diff", "--cached", "--name-only")
    if not status.stdout.strip():
        log.info("нечего коммитить")
        return True

    result = git("commit", "-m", message)
    if result.returncode != 0:
        log.error("commit не прошёл: %s", result.stderr.strip())
        return False

    if not has_remote():
        log.info("remote не настроен, push пропущен")
        return True

    for attempt in (1, 2):
        push = git("push")
        if push.returncode == 0:
            return True
        log.warning("push отклонён (попытка %s): %s", attempt, push.stderr.strip())
        if not pull_rebase():
            log.error("изменения записаны и закоммичены локально, но не отправлены")
            return False
    log.error("push не удался дважды, изменения остались локально")
    return False


async def git_approve(path: Path) -> tuple[Path, bool]:
    async with git_lock:
        return await asyncio.to_thread(approve, path)


async def git_request_edit(path: Path, comment: str) -> tuple[Path, bool]:
    async with git_lock:
        return await asyncio.to_thread(request_edit, path, comment)


async def git_pull() -> None:
    async with git_lock:
        await asyncio.to_thread(sync_before_write)


async def git_save(message: str) -> bool:
    async with git_lock:
        return await asyncio.to_thread(commit_and_push, message)


# ─────────────────────────── переходы ───────────────────────────


def approve(path: Path) -> tuple[Path, bool]:
    sync_before_write()
    text = path.read_text(encoding="utf-8")
    text = set_field(text, "статус", "approved")
    text = set_field(text, "проверено_автором", date.today().isoformat())
    text = set_field(text, "результат_проверки", "ок")
    path.write_text(text, encoding="utf-8")

    target = path.parent.parent / "approved" / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))
    saved = commit_and_push(f"review: {path.stem} одобрен автором")
    return target, saved


def request_edit(path: Path, comment: str) -> tuple[Path, bool]:
    sync_before_write()
    text = path.read_text(encoding="utf-8")
    text = set_field(text, "статус", "queue")
    text = set_field(text, "проверено_автором", date.today().isoformat())
    text = set_field(text, "результат_проверки", "правка")
    # Комментарий кладётся в тело: во фронтматтере он развалит YAML
    # на первом же переводе строки. Маркер отключает линтер ниже себя —
    # автор пишет разработчику «убери отсюда дозировку», и это законный
    # текст, который никогда не попадёт в публикацию.
    text = (
        text.rstrip()
        + "\n\n<!-- lint:ignore-below — правка автора, не публикуемый текст -->\n\n"
        + f"ПРАВКА АВТОРА ({date.today().isoformat()}):\n{comment.strip()}\n"
    )
    path.write_text(text, encoding="utf-8")

    target = path.parent.parent / "queue" / path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(target))
    saved = commit_and_push(f"review: {path.stem} с правкой автора")
    return target, saved


# ──────────────────────────── телеграм ────────────────────────────


def build_application(config: dict[str, str]):
    from ai_budget import AIBudgetExceeded, BudgetSettings
    from topic_registry import sync_from_environment as sync_registry_from_environment
    from content_workflow import (
        generate_bundle_from_environment,
        FORMAT_LABELS,
        RUBRIC_NAMES,
        rubric_brief,
        save_topic_note_from_environment,
        set_topic_rubric_from_environment,
        approve_material_from_environment,
        create_digest_from_environment,
        generate_material_from_environment,
        load_digest_page_from_environment,
        load_latest_digest_from_environment,
        load_topic_from_environment,
        mark_digest_sent_from_environment,
        revise_material_from_environment,
    )
    from content_pipeline import run_content_pipeline
    from lint import hard_violations
    from material_export import export_from_environment
    from publication_scheduler import (
        cancel_from_environment,
        claim_due_from_environment,
        future_slots,
        list_active_from_environment,
        mark_failed_from_environment,
        mark_published_from_environment,
        material_summary_from_environment,
        moscow_datetime,
        parse_author_date,
        parse_author_time,
        release_stuck_from_environment,
        schedule_from_environment,
    )
    from telegram import (
        BotCommand,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        KeyboardButton,
        ReplyKeyboardMarkup,
        Update,
    )
    from telegram.constants import ParseMode
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )

    author_ids = parse_author_chat_ids(config)
    # Пороги берём из настроек: раньше суммы были вписаны прямо в текст
    # сообщений и врали, стоило поменять переменные окружения.
    budget_settings = BudgetSettings.from_mapping(config)
    budget_warning_rub = budget_settings.warning_rub
    budget_hard_limit_rub = budget_settings.hard_limit_rub
    author_id = author_ids[0]
    dev_id = int(config["DEV_CHAT_ID"]) if config.get("DEV_CHAT_ID", "").lstrip("-").isdigit() else None
    publish_author_id = int(config.get("PUBLISH_AUTHOR_CHAT_ID") or author_id)
    content_pipeline_hour = int(
        config.get("CONTENT_PIPELINE_HOUR", str(DEFAULT_CONTENT_PIPELINE_HOUR))
    )
    topic_retention_days = int(
        config.get("TOPIC_RETENTION_DAYS", str(DEFAULT_TOPIC_RETENTION_DAYS))
    )

    def is_author(update: Update) -> bool:
        """Права даёт человек, а не чат: в группе чат общий на всех."""
        user = update.effective_user
        chat = update.effective_chat
        if user is None or user.id not in author_ids:
            return False
        return chat is not None and chat.id == user.id

    async def tell_dev(bot, what: str) -> None:
        """Разработчику — про сломанную синхронизацию.

        Иначе о конфликте узнают только когда заметят, что материалы
        перестали приезжать, а это неделя.
        """
        if dev_id is None:
            return
        pending_output = git("log", "--oneline", "origin/main..HEAD").stdout.strip()
        pending = len(pending_output.splitlines()) if pending_output else 0
        try:
            await bot.send_message(
                dev_id,
                f"Синхронизация не прошла: {what}\n"
                f"Локальных коммитов ждёт отправки: {pending}\n"
                f"Смотреть: git -C {ROOT} status и logs/bot.log",
            )
        except Exception:  # noqa: BLE001 — падать из-за уведомления нельзя
            log.exception("не смог предупредить разработчика")

    def keyboard(name: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Всё верно", callback_data=f"ok:{name}"),
                InlineKeyboardButton("✏️ Поправить", callback_data=f"edit:{name}"),
            ]]
        )

    def topic_keyboard(page) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(
                    f"{topic.rank}. {topic.title[:48]}",
                    callback_data=callback_data("topic", topic.id),
                )
            ]
            for topic in page.topics
        ]
        navigation = []
        if page.page > 1:
            navigation.append(
                InlineKeyboardButton(
                    "← Первые 5",
                    callback_data=callback_data("digest", page.digest_id, 1),
                )
            )
        if page.has_next:
            navigation.append(
                InlineKeyboardButton(
                    "Ещё 5 →",
                    callback_data=callback_data("digest", page.digest_id, page.page + 1),
                )
            )
        if navigation:
            rows.append(navigation)
        return InlineKeyboardMarkup(rows)

    def format_keyboard(topic_id: str) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(
                    FORMAT_LABELS[format_name],
                    callback_data=callback_data("format", topic_id, format_name),
                )
            ]
            for format_name in ("telegram", "reels", "tiktok")
        ]
        # Пакетом дешевле и для автора, и по токенам: сценарии делаются из
        # уже готового поста, поэтому фактура проверяется один раз, а не три.
        rows.append(
            [
                InlineKeyboardButton(
                    "📦 Всё сразу: пост, Reels и TikTok",
                    callback_data=callback_data("bundle", topic_id),
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "🎤 Сначала наговорю от себя",
                    callback_data=callback_data("note", topic_id),
                ),
                InlineKeyboardButton(
                    "🔁 Другая рубрика",
                    callback_data=callback_data("rubric", topic_id),
                ),
            ]
        )
        return InlineKeyboardMarkup(rows)

    def rubric_keyboard(topic_id: str) -> InlineKeyboardMarkup:
        """Семь рубрик раздела 8 — по две в ряд, чтобы влезло на экран."""
        buttons = [
            InlineKeyboardButton(
                f"{code} · {name}",
                callback_data=callback_data("setrubric", topic_id, code),
            )
            for code, name in RUBRIC_NAMES.items()
        ]
        rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
        rows.append(
            [
                InlineKeyboardButton(
                    "← Оставить как есть",
                    callback_data=callback_data("topic", topic_id),
                )
            ]
        )
        return InlineKeyboardMarkup(rows)

    def material_keyboard(material_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "✅ Одобрить",
                    callback_data=callback_data("material_ok", material_id),
                ),
                InlineKeyboardButton(
                    "✏️ Поправить",
                    callback_data=callback_data("material_edit", material_id),
                ),
            ]]
        )

    def date_keyboard(material_id: str) -> InlineKeyboardMarkup:
        today = datetime.now(MOSCOW).date()
        rows = []
        if future_slots(today):
            rows.append([
                InlineKeyboardButton(
                    "Сегодня",
                    callback_data=callback_data("sd", material_id, today.isoformat()),
                )
            ])
        tomorrow = today + timedelta(days=1)
        rows.append([
            InlineKeyboardButton(
                "Завтра",
                callback_data=callback_data("sd", material_id, tomorrow.isoformat()),
            )
        ])
        rows.append([
            InlineKeyboardButton(
                "📅 Выбрать дату",
                callback_data=callback_data("sdc", material_id),
            )
        ])
        return InlineKeyboardMarkup(rows)

    def time_keyboard(material_id: str, day: date) -> InlineKeyboardMarkup:
        slots = future_slots(day)
        rows = [
            [
                InlineKeyboardButton(
                    slot.strftime("%H:%M"),
                    callback_data=callback_data(
                        "st", material_id, day.isoformat(), slot.strftime("%H%M")
                    ),
                )
                for slot in slots[index : index + 2]
            ]
            for index in range(0, len(slots), 2)
        ]
        rows.append([
            InlineKeyboardButton(
                "⏰ Другое время",
                callback_data=callback_data("stc", material_id, day.isoformat()),
            )
        ])
        return InlineKeyboardMarkup(rows)

    def confirmation_keyboard(material_id: str, scheduled_at: datetime) -> InlineKeyboardMarkup:
        epoch = int(scheduled_at.timestamp())
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "✅ Запланировать",
                    callback_data=callback_data("sc", material_id, epoch),
                ),
                InlineKeyboardButton(
                    "← Изменить",
                    callback_data=callback_data("sb", material_id),
                ),
            ]]
        )

    def menu(waiting_edit: bool = False) -> ReplyKeyboardMarkup:
        """Кнопки вместо команд: автор ничего не печатает и не запоминает.

        Держится под полем ввода постоянно, поэтому забыть про бота или
        не найти нужное действие невозможно.
        """
        rows = (
            [[KeyboardButton(BUTTON_CANCEL)]]
            if waiting_edit
            else [
                [KeyboardButton(BUTTON_TOPICS)],
                [KeyboardButton(BUTTON_QUEUE), KeyboardButton(BUTTON_REVIEW)],
            ]
        )
        return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

    async def send_topic_page(bot, chat_id: int, page) -> None:
        lines = [f"📰 Актуальные темы · страница {page.page}", ""]
        for topic in page.topics:
            lines.append(f"{topic.rank}. {topic.title}")
            label = getattr(topic, "rubric_label", "")
            if label:
                lines.append(label)
            if topic.summary and topic.summary != topic.title:
                lines.append(topic.summary)
            lines.extend([f"Почему сейчас: {topic.rationale}", ""])
        lines.append("Выберите тему кнопкой ниже.")
        rendered = "\n".join(lines).strip()
        if len(rendered) > TELEGRAM_LIMIT:
            rendered = rendered[: TELEGRAM_LIMIT - 40].rstrip() + "\n\n[описания сокращены]"
        await bot.send_message(chat_id, rendered, reply_markup=topic_keyboard(page))

    async def send_generated_material(bot, chat_id: int, result) -> None:
        # Сначала — то, ради чего автор здесь: тезис, источник и одно
        # конкретное утверждение на проверку. Текст идёт следом. В файловом
        # конвейере это были поля «источник» и «что_проверить», и по ним
        # автор сверяла факты; при переезде в базу они потерялись.
        check_note = getattr(result, "check_note", "")
        sources = getattr(result, "sources", ())
        if check_note or sources:
            lines = [f"🔍 {FORMAT_LABELS[result.format]} · на проверку"]
            if getattr(result, "title", ""):
                lines.append(f"<b>{esc(result.title)}</b>")
            if check_note:
                lines.append(f"\n❓ <b>Что сверить</b>\n{esc(check_note)}")
            if sources:
                links = "\n".join(
                    f"· {esc(item.get('source', 'источник'))}: {esc(item.get('url', ''))}"
                    for item in sources[:3]
                )
                lines.append(f"\n📎 <b>Источники</b>\n{links}")
            await bot.send_message(
                chat_id, "\n".join(lines), parse_mode=ParseMode.HTML
            )

        heading = (
            f"✍️ {FORMAT_LABELS[result.format]} · версия {result.version}\n\n"
        )
        chunks = split_telegram_text(heading + result.body)
        for index, chunk in enumerate(chunks):
            markup = material_keyboard(result.material_id) if index == len(chunks) - 1 else None
            await bot.send_message(chat_id, chunk, reply_markup=markup)
        if getattr(result, "schedule_cancelled", False):
            await bot.send_message(
                chat_id,
                "Прежняя публикация снята: это уже другой текст. "
                "Одобрите и выберите дату заново.",
                reply_markup=menu(),
            )
        # Мягкие срабатывания линтера — не запрет, а повод посмотреть:
        # разбор мифа иногда обязан миф процитировать.
        warnings = getattr(result, "warnings", ())
        if warnings:
            notes = "\n".join(item.split("] ", 1)[-1] for item in warnings[:5])
            await bot.send_message(
                chat_id,
                f"⚠️ Линтер отметил формулировки — посмотрите, уместны ли они:\n{notes}",
                reply_markup=menu(),
            )

    def publication_buttons(publication) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "🔄 Перенести",
                    callback_data=callback_data("pr", publication.id),
                ),
                InlineKeyboardButton(
                    "✖️ Отменить",
                    callback_data=callback_data("pc", publication.id),
                ),
            ]]
        )

    async def show_queue(bot, chat_id: int) -> None:
        publications = await asyncio.to_thread(list_active_from_environment)
        if not publications:
            await bot.send_message(chat_id, "Очередь пуста.", reply_markup=menu())
            return
        await bot.send_message(
            chat_id,
            f"В очереди: {plural(len(publications), 'публикация', 'публикации', 'публикаций')}.",
            reply_markup=menu(),
        )
        for publication in publications:
            local = publication.scheduled_at.astimezone(MOSCOW)
            status = {
                "scheduled": "запланировано",
                "publishing": "отправляется",
                "failed": (
                    "остановлено после 3 ошибок"
                    if publication.attempts >= 3
                    else "ошибка, будет повтор"
                ),
                "held": "отправка прервалась, нужна проверка канала",
            }.get(publication.status, publication.status)
            await bot.send_message(
                chat_id,
                f"{publication.title}\n"
                f"📅 {local:%d.%m.%Y в %H:%M} МСК\n"
                f"Статус: {status}\n"
                f"Получатель: {'тестовая личка' if publication.destination == 'author' else 'канал'}",
                reply_markup=(
                    publication_buttons(publication)
                    if publication.status in {"scheduled", "failed", "held"}
                    else None
                ),
            )

    async def show_schedule_confirmation(
        bot, chat_id: int, material_id: str, scheduled_at: datetime
    ) -> None:
        material = await asyncio.to_thread(material_summary_from_environment, material_id)
        if material is None:
            raise ValueError("Материал не найден")
        state = read_user_state(chat_id)
        state["schedule_candidate"] = {
            "material_id": material_id,
            "scheduled_at": scheduled_at.isoformat(),
        }
        write_user_state(chat_id, state)
        destination = config.get("TELEGRAM_PUBLISH_TARGET", "author").strip() or "author"
        destination_label = (
            "канал" if destination == "channel" else "тестовая личка автора"
        )
        await bot.send_message(
            chat_id,
            f"Проверьте перед постановкой в очередь:\n\n"
            f"{material[1]}\n"
            f"📅 {scheduled_at:%d.%m.%Y в %H:%M} МСК\n"
            f"Получатель: {destination_label}",
            reply_markup=confirmation_keyboard(material_id, scheduled_at),
        )

    async def generate_and_send(bot, chat_id: int, topic_id: str, format_name: str) -> None:
        """Генерация в фоне: минуты работы не должны блокировать бота."""
        try:
            async with generation_lock:
                result = await asyncio.to_thread(
                    generate_material_from_environment, topic_id, format_name
                )
        except (AIBudgetExceeded, ValueError) as error:
            await bot.send_message(
                chat_id, str(error), reply_markup=format_keyboard(topic_id)
            )
            return
        except Exception:  # noqa: BLE001 — фоновая задача не должна падать молча
            log.exception("генерация %s/%s не удалась", topic_id, format_name)
            await bot.send_message(
                chat_id,
                "Не смог подготовить материал. Разработчику сообщил, "
                "попробуйте другую тему.",
                reply_markup=menu(),
            )
            await tell_dev(bot, f"генерация {topic_id}/{format_name} не удалась")
            return
        await send_generated_material(bot, chat_id, result)

    async def generate_bundle_and_send(bot, chat_id: int, topic_id: str) -> None:
        try:
            async with generation_lock:
                results = await asyncio.to_thread(
                    generate_bundle_from_environment, topic_id
                )
        except (AIBudgetExceeded, ValueError) as error:
            await bot.send_message(
                chat_id, str(error), reply_markup=format_keyboard(topic_id)
            )
            return
        except Exception:  # noqa: BLE001 — фоновая задача не должна падать молча
            log.exception("пакет материалов по теме %s не собран", topic_id)
            await bot.send_message(
                chat_id,
                "Не смог собрать пакет. Разработчику сообщил, "
                "попробуйте один формат отдельно.",
                reply_markup=menu(),
            )
            await tell_dev(bot, f"пакет по теме {topic_id} не собран")
            return
        for result in results:
            await send_generated_material(bot, chat_id, result)

    async def revise_and_send(bot, chat_id: int, material_id: str, instruction: str) -> None:
        try:
            async with generation_lock:
                result = await asyncio.to_thread(
                    revise_material_from_environment, material_id, instruction
                )
        except (AIBudgetExceeded, ValueError) as error:
            await bot.send_message(
                chat_id, str(error), reply_markup=menu(waiting_edit=True)
            )
            return
        except Exception:  # noqa: BLE001
            log.exception("правка %s не применена", material_id)
            await bot.send_message(
                chat_id,
                "Не смог применить правку. Разработчику сообщил.",
                reply_markup=menu(),
            )
            await tell_dev(bot, f"правка {material_id} не применена")
            return
        state = read_user_state(chat_id)
        state.pop("awaiting_material_edit", None)
        write_user_state(chat_id, state)
        await send_generated_material(bot, chat_id, result)
        log.info("правка к %s применена", result.material_id)

    async def deliver_script(bot, chat_id: int, material_id: str, format_name: str) -> None:
        """Одобренный сценарий уезжает файлом в репозиторий."""
        try:
            target = await asyncio.to_thread(export_from_environment, material_id)
        except ValueError as error:
            log.error("сценарий %s не выгружен: %s", material_id, error)
            await bot.send_message(
                chat_id,
                f"Одобрено, но сохранить сценарий в работу не смог: {error}\n"
                "Разработчику сообщил.",
                reply_markup=menu(),
            )
            await tell_dev(bot, f"сценарий {material_id} не выгружен: {error}")
            return

        saved = await git_save(f"script: {target.stem} одобрен автором")
        label = FORMAT_LABELS.get(format_name, "сценарий")
        answer = (
            f"Одобрено. {label.capitalize()} сохранён под номером {target.stem} — "
            "он в списке на съёмку.\n\n"
            "Дальше: снять или начитать озвучку. Готовое видео — в облачную "
            "папку, ссылку сюда."
        )
        if not saved:
            answer += "\n\nСинхронизация не прошла — разработчик поправит."
            await tell_dev(bot, f"сценарий {target.name} не отправлен в репозиторий")
        await bot.send_message(chat_id, answer, reply_markup=menu())

    async def send_digest(bot, chat_id: int, quiet_if_empty: bool = False) -> int:
        items = waiting_materials()
        if not items:
            if not quiet_if_empty:
                await bot.send_message(
                    chat_id, "Сейчас проверять нечего. Пришлю, когда появится.", reply_markup=menu()
                )
            return 0

        word = plural(len(items), "материал", "материала", "материалов")
        await bot.send_message(
            chat_id,
            f"Жду вашей проверки: {word}.\n"
            "Под каждым — две кнопки. Всё верно — жмите зелёную, "
            "есть что поправить — вторую, и напишите словами.",
            reply_markup=menu(),
        )
        delivered = 0
        for path in items:
            # По одному материалу в своём try: раньше первое же падение
            # (битый HTML, лимит) обрывало пакет целиком, а из плановой
            # рассылки — и очередь остальных авторов.
            try:
                await bot.send_message(
                    chat_id,
                    describe(path),
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard(f"{path.parent.parent.name}/{path.name}"),
                )
                delivered += 1
            except Exception:  # noqa: BLE001 — остальные материалы должны дойти
                log.exception("материал %s не отправлен автору %s", path.name, chat_id)

        state = read_user_state(chat_id)
        state["last_digest"] = datetime.now(timezone.utc).isoformat()
        state["last_reminder"] = None
        write_user_state(chat_id, state)
        if delivered < len(items):
            await bot.send_message(
                chat_id,
                f"Часть материалов ({len(items) - delivered}) показать не смог — "
                "разработчику сообщил, он поправит.",
                reply_markup=menu(),
            )
            await tell_dev(bot, f"не отправлено материалов: {len(items) - delivered}")
        return delivered

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_author(update):
            chat = update.effective_chat
            user = update.effective_user
            if chat is None or update.message is None:
                return
            username = f"@{user.username}" if user and user.username else "без username"
            display_name = user.full_name if user else "Неизвестный пользователь"
            await update.message.reply_text(
                f"Доступ к рабочему боту пока не выдан. Ваш Telegram ID: {chat.id}. "
                "Отправьте этот номер Сане."
            )
            await context.bot.send_message(
                author_id,
                f"Запрос доступа к боту: {display_name} ({username}), ID {chat.id}. "
                "Доступ автоматически не выдавал.",
                reply_markup=menu(),
            )
            log.info("запрос доступа: chat_id=%s username=%s", chat.id, username)
            return
        await update.message.reply_text(
            "Здравствуйте. Это рабочий бот блога — печатать команды не нужно, "
            "всё делается кнопками.\n\n"
            "🎤 Наговорите голосовое — расшифрую и сохраню. Диктовать можно "
            "как удобно, со сбивками и паузами, разберу.\n\n"
            "📰 Кнопка «Актуальные темы» покажет свежий дайджест. "
            "Выберите тему и формат — я подготовлю черновик.\n\n"
            "📋 По вторникам утром пришлю материалы, которые ждут вашей проверки. "
            "Нужны раньше — кнопка внизу.",
            reply_markup=menu(),
        )

    async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_author(update):
            return
        await git_pull()
        await send_digest(context.bot, update.effective_chat.id)

    async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_author(update):
            return
        await show_queue(context.bot, update.effective_chat.id)

    async def cmd_topics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_author(update):
            return
        await update.message.reply_text("Смотрю актуальные темы…", reply_markup=menu())
        # Сначала показываем уже собранный дайджест. Раньше кнопка сразу
        # звала create_digest, а он забирает до десяти тем-кандидатов и
        # переводит их в «показаны»: второе нажатие подряд отдавало другой,
        # куцый список, а пагинация первого разъезжалась.
        page = await asyncio.to_thread(load_latest_digest_from_environment)
        if page is None:
            page = await asyncio.to_thread(create_digest_from_environment)
        if page is None:
            await update.message.reply_text(
                "Дайджест ещё не собран. Ближайшее обновление — по расписанию.",
                reply_markup=menu(),
            )
            return
        await send_topic_page(context.bot, update.effective_chat.id, page)
        await asyncio.to_thread(mark_digest_sent_from_environment, page.digest_id)

    async def notify_content_failure(bot, message: str) -> None:
        state = read_state()
        now = datetime.now(timezone.utc)
        previous = state.get("last_content_error_notice")
        if previous:
            try:
                if now - datetime.fromisoformat(previous) < CONTENT_ERROR_COOLDOWN:
                    log.warning("уведомление об обновлении подавлено на час: %s", message)
                    return
            except ValueError:
                log.warning("повреждена дата last_content_error_notice")
        await bot.send_message(author_id, message, reply_markup=menu())
        state["last_content_error_notice"] = now.isoformat()
        write_state(state)

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_author(update):
            return
        items = waiting_materials()
        state = read_user_state(update.effective_chat.id)
        pending = state.get("awaiting_edit")
        pending_material = state.get("awaiting_material_edit")
        count = plural(len(items), "материал", "материала", "материалов")
        lines = [f"Ждёт проверки: {count}." if items else "Проверять пока нечего."]
        if pending:
            lines.append(
                f"Ещё жду от вас правку к «{title_of(pending['file'])}» — "
                "напишите словами или нажмите кнопку внизу."
            )
        if pending_material:
            lines.append(
                "Ещё жду правку к сгенерированному материалу — "
                "пришлите текст или голосовое."
            )
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=menu(waiting_edit=bool(pending or pending_material)),
        )

    async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_author(update):
            return
        chat_id = update.effective_chat.id
        state = read_user_state(chat_id)
        cancelled_file = state.pop("awaiting_edit", None)
        cancelled_material = state.pop("awaiting_material_edit", None)
        cancelled_date = state.pop("awaiting_schedule_date", None)
        cancelled_time = state.pop("awaiting_schedule_time", None)
        cancelled_note = state.pop("awaiting_topic_note", None)
        state.pop("schedule_candidate", None)
        state.pop("scheduling_publication_id", None)
        cancelled = bool(
            cancelled_file
            or cancelled_material
            or cancelled_date
            or cancelled_time
            or cancelled_note
        )
        if cancelled:
            write_user_state(chat_id, state)
            if cancelled_note and not (cancelled_file or cancelled_material):
                await update.message.reply_text(
                    "Хорошо, обойдёмся без заметки. Тему выберите заново — "
                    "кнопка «Актуальные темы».",
                    reply_markup=menu(),
                )
                return
            await update.message.reply_text(
                "Хорошо, правку не жду. Материал остался на проверке — вернётесь, когда удобно.",
                reply_markup=menu(),
            )
        else:
            await update.message.reply_text("Сейчас я ничего не жду.", reply_markup=menu())

    async def safe_answer(query) -> None:
        """Ответ на нажатие. Протухший запрос — не повод падать.

        Кнопка могла быть нажата, пока бот перезапускался или пока связь
        лежала: апдейты копятся, приезжают пачкой, и к этому моменту
        телеграм считает запрос устаревшим.
        """
        try:
            await query.answer()
        except Exception as error:  # noqa: BLE001
            log.info("нажатие устарело, продолжаю: %s", error)

    async def hide_buttons(query) -> None:
        """Снять кнопки. Их могло уже не быть — это не ошибка."""
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as error:  # noqa: BLE001
            log.debug("кнопки уже сняты: %s", error)

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await safe_answer(query)
        if not is_author(update):
            return
        chat_id = update.effective_chat.id

        action, _, payload = (query.data or "").partition(":")

        if action == "digest":
            digest_id, separator, raw_page = payload.rpartition(":")
            if not separator or not raw_page.isdigit():
                raise ValueError("Неверная кнопка дайджеста")
            page = await asyncio.to_thread(
                load_digest_page_from_environment, digest_id, int(raw_page)
            )
            await hide_buttons(query)
            if page is None:
                await context.bot.send_message(chat_id,
                    "Этот дайджест уже недоступен. Запросите новый.", reply_markup=menu()
                )
                return
            await send_topic_page(context.bot, chat_id, page)
            return

        if action == "topic":
            topic = await asyncio.to_thread(load_topic_from_environment, payload)
            await hide_buttons(query)
            if topic is None:
                await context.bot.send_message(chat_id,
                    "Тема больше недоступна. Выберите другую из нового дайджеста.",
                    reply_markup=menu(),
                )
                return
            lines = [f"Выбрана тема:\n{topic.title}"]
            brief = rubric_brief(topic.rubric, topic.niche)
            if brief:
                lines.append(f"Подача: {brief}")
            if topic.author_note:
                lines.append("Ваша заметка по теме уже сохранена и пойдёт в материал.")
            lines.append("\nКакой материал подготовить?")
            await context.bot.send_message(chat_id,
                "\n".join(lines),
                reply_markup=format_keyboard(topic.id),
            )
            return

        if action == "rubric":
            await hide_buttons(query)
            await context.bot.send_message(
                chat_id,
                "В какой рубрике подать тему? Рубрика задаёт угол: разбор мифа "
                "начинается с самого утверждения, «что означает показатель» — "
                "с того, что человек видит в бланке.",
                reply_markup=rubric_keyboard(payload),
            )
            return

        if action == "setrubric":
            topic_id, separator, code = payload.rpartition(":")
            if not separator or code not in RUBRIC_NAMES:
                raise ValueError("Неверная кнопка рубрики")
            await hide_buttons(query)
            changed = await asyncio.to_thread(set_topic_rubric_from_environment, topic_id, code)
            if not changed:
                await context.bot.send_message(
                    chat_id, "Тема больше недоступна.", reply_markup=menu()
                )
                return
            await context.bot.send_message(
                chat_id,
                f"Рубрика: {code} · {RUBRIC_NAMES[code]}.\n\nКакой материал подготовить?",
                reply_markup=format_keyboard(topic_id),
            )
            return

        if action == "note":
            await hide_buttons(query)
            state = read_user_state(chat_id)
            state["awaiting_topic_note"] = {
                "topic_id": payload,
                "since": datetime.now(timezone.utc).isoformat(),
            }
            write_user_state(chat_id, state)
            await context.bot.send_message(
                chat_id,
                "Наговорите или напишите, что знаете по теме: что разбирали на "
                "цикле, что обычно понимают неправильно, откуда это известно. "
                "Ваши слова пойдут в материал выше источников — этого нет ни в "
                "одной статье.",
                reply_markup=menu(waiting_edit=True),
            )
            return

        if action == "format":
            topic_id, separator, format_name = payload.rpartition(":")
            if not separator or format_name not in FORMAT_LABELS:
                raise ValueError("Неверная кнопка формата")
            await hide_buttons(query)
            await context.bot.send_message(chat_id,
                f"Готовлю {FORMAT_LABELS[format_name]}. Это может занять пару минут…",
                reply_markup=menu(),
            )
            context.application.create_task(
                generate_and_send(context.bot, chat_id, topic_id, format_name)
            )
            return

        if action == "bundle":
            await hide_buttons(query)
            await context.bot.send_message(
                chat_id,
                "Готовлю пост, а из него — сценарии Reels и TikTok. "
                "Это займёт несколько минут, пришлю всё разом.",
                reply_markup=menu(),
            )
            context.application.create_task(
                generate_bundle_and_send(context.bot, chat_id, payload)
            )
            return

        if action == "material_ok":
            await hide_buttons(query)
            approved = await asyncio.to_thread(
                approve_material_from_environment, payload, chat_id
            )
            material = await asyncio.to_thread(material_summary_from_environment, payload)
            if material and material[2] == "telegram" and material[3] == "approved":
                state = read_user_state(chat_id)
                state.pop("scheduling_publication_id", None)
                write_user_state(chat_id, state)
                await context.bot.send_message(chat_id,
                    "Одобрено. Когда отправить Telegram-пост?",
                    reply_markup=date_keyboard(payload),
                )
            elif material and material[3] == "scheduled":
                await context.bot.send_message(chat_id,
                    "Этот пост уже стоит в очереди. Перенести или отменить его можно "
                    "через «Очередь публикаций».",
                    reply_markup=menu(),
                )
            elif approved and material:
                # Сценарий дальше идёт руками: съёмка, озвучка, монтаж.
                # Пока он лежал только в базе, эта работа была не видна
                # ни автору, ни продакшну, ни status.py.
                await deliver_script(context.bot, chat_id, payload, material[2])
            else:
                await context.bot.send_message(
                    chat_id, "Этот материал уже был обработан.", reply_markup=menu()
                )
            return

        if action == "material_edit":
            await hide_buttons(query)
            state = read_user_state(chat_id)
            state["awaiting_material_edit"] = {
                "material_id": payload,
                "since": datetime.now(timezone.utc).isoformat(),
            }
            write_user_state(chat_id, state)
            await context.bot.send_message(chat_id,
                "Пришлите правку текстом или голосовым. "
                "Я верну материал целиком с новой версией.",
                reply_markup=menu(waiting_edit=True),
            )
            return

        if action == "sd":
            material_id, separator, raw_day = payload.rpartition(":")
            if not separator:
                raise ValueError("Неверная дата")
            day = date.fromisoformat(raw_day)
            await hide_buttons(query)
            await context.bot.send_message(chat_id,
                f"Дата: {day:%d.%m.%Y}. Выберите время по Москве:",
                reply_markup=time_keyboard(material_id, day),
            )
            return

        if action == "sdc":
            await hide_buttons(query)
            state = read_user_state(chat_id)
            state["awaiting_schedule_date"] = {"material_id": payload}
            write_user_state(chat_id, state)
            await context.bot.send_message(chat_id,
                "Напишите дату: ДД.ММ или ДД.ММ.ГГГГ.\n"
                "Например: 25.08",
                reply_markup=menu(waiting_edit=True),
            )
            return

        if action == "st":
            parts = payload.split(":")
            if len(parts) != 3 or len(parts[2]) != 4:
                raise ValueError("Неверное время")
            material_id, raw_day, raw_clock = parts
            scheduled_at = moscow_datetime(
                date.fromisoformat(raw_day),
                time(int(raw_clock[:2]), int(raw_clock[2:])),
            )
            await hide_buttons(query)
            await show_schedule_confirmation(
                context.bot, chat_id, material_id, scheduled_at
            )
            return

        if action == "stc":
            material_id, separator, raw_day = payload.rpartition(":")
            if not separator:
                raise ValueError("Неверная дата")
            await hide_buttons(query)
            state = read_user_state(chat_id)
            state["awaiting_schedule_time"] = {
                "material_id": material_id,
                "day": raw_day,
            }
            write_user_state(chat_id, state)
            await context.bot.send_message(chat_id,
                "Напишите время по Москве в формате ЧЧ:ММ.\n"
                "Например: 18:30",
                reply_markup=menu(waiting_edit=True),
            )
            return

        if action == "sb":
            await hide_buttons(query)
            await context.bot.send_message(chat_id,
                "Выберите новую дату:", reply_markup=date_keyboard(payload)
            )
            return

        if action == "sc":
            material_id, separator, raw_epoch = payload.rpartition(":")
            if not separator or not raw_epoch.isdigit():
                raise ValueError("Неверное время публикации")
            scheduled_at = datetime.fromtimestamp(int(raw_epoch), MOSCOW)
            state = read_user_state(chat_id)
            candidate = state.get("schedule_candidate") or {}
            if (
                candidate.get("material_id") != material_id
                or candidate.get("scheduled_at") != scheduled_at.isoformat()
            ):
                raise ValueError("Подтверждение устарело, выберите дату заново")
            publication_id = state.get("scheduling_publication_id")
            destination = config.get("TELEGRAM_PUBLISH_TARGET", "author").strip() or "author"
            await hide_buttons(query)
            try:
                await asyncio.to_thread(
                    schedule_from_environment,
                    material_id=material_id,
                    scheduled_at=scheduled_at,
                    destination=destination,
                    publication_id=publication_id,
                    scheduled_by=chat_id,
                )
            except ValueError as error:
                await context.bot.send_message(chat_id,
                    f"{error}. Выберите дату заново.",
                    reply_markup=date_keyboard(material_id),
                )
                return
            for key in (
                "schedule_candidate",
                "scheduling_publication_id",
                "awaiting_schedule_date",
                "awaiting_schedule_time",
            ):
                state.pop(key, None)
            write_user_state(chat_id, state)
            await context.bot.send_message(chat_id,
                f"Готово: {scheduled_at:%d.%m.%Y в %H:%M} МСК. "
                + (
                    "Пост уйдёт в канал."
                    if destination == "channel"
                    else "Тестовый пост уйдёт в личку автора."
                ),
                reply_markup=menu(),
            )
            return

        if action == "pc":
            cancelled = await asyncio.to_thread(cancel_from_environment, payload)
            await hide_buttons(query)
            await context.bot.send_message(chat_id,
                "Публикация отменена."
                if cancelled
                else "Публикацию уже нельзя отменить.",
                reply_markup=menu(),
            )
            return

        if action == "pr":
            publications = await asyncio.to_thread(list_active_from_environment)
            publication = next((item for item in publications if item.id == payload), None)
            await hide_buttons(query)
            if publication is None or publication.status not in {"scheduled", "failed", "held"}:
                await context.bot.send_message(chat_id, "Публикацию уже нельзя перенести.")
                return
            state = read_user_state(chat_id)
            state["scheduling_publication_id"] = publication.id
            write_user_state(chat_id, state)
            await context.bot.send_message(chat_id,
                "Выберите новую дату:",
                reply_markup=date_keyboard(publication.material_id),
            )
            return

        if action not in {"ok", "edit"}:
            raise ValueError("Неизвестная кнопка")

        name = payload
        track, separator, filename = name.partition("/")
        if not separator or track not in TRACKS or Path(filename).name != filename:
            raise ValueError("Неверная ссылка на материал")
        path = ROOT / track / "review" / filename

        # Кнопки снимаются до git-операций: те занимают секунды, и всё это
        # время автор видит живые кнопки. Второе нажатие приходило бы на
        # уже обработанный материал.
        await hide_buttons(query)

        if not path.exists():
            await context.bot.send_message(chat_id,
                "Этот материал уже обработан — видимо, вы ответили раньше.", reply_markup=menu()
            )
            return

        if action == "ok":
            target, saved = await git_approve(path)
            answer = "Приняла — записал. Этот материал уходит в публикацию."
            if not saved:
                answer += "\n\nВаш ответ сохранён, но синхронизация не прошла — разработчик поправит."
                await tell_dev(context.bot, f"одобрение {target.stem} не отправлено")
            await context.bot.send_message(chat_id, answer, reply_markup=menu())
            log.info("одобрен %s, синхронизация: %s", target, saved)
            return

        if action == "edit":
            state = read_user_state(chat_id)
            state["awaiting_edit"] = {
                "file": name,
                "since": datetime.now(timezone.utc).isoformat(),
                "message_id": getattr(query.message, "message_id", None),
            }
            write_user_state(chat_id, state)
            await context.bot.send_message(chat_id,
                "Напишите, что поправить — своими словами, одним сообщением.\n"
                "Передумали — кнопка внизу.",
                reply_markup=menu(waiting_edit=True),
            )

    async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Приём голосового. Расшифровка уходит в фон.

        Апдейты обрабатываются по одному, поэтому пятнадцатиминутная
        расшифровка держала очередь целиком: автор жала кнопки, слала
        второе голосовое — бот не отвечал никому, включая второго автора.
        Здесь остаётся только быстрая часть: скачать и подтвердить приём.
        """
        if not is_author(update):
            return

        message = update.message
        chat_id = message.chat.id
        state = read_user_state(chat_id)
        pending_material = state.get("awaiting_material_edit")
        source = message.voice or message.audio or message.document
        if source is None:
            return

        if source.file_size and source.file_size > MAX_VOICE_BYTES:
            await message.reply_text(
                "Файл больше двадцати мегабайт — телеграм не отдаёт такие ботам.\n"
                "Разбейте на части или пришлите обычным голосовым."
            )
            return

        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        suffix = Path(getattr(source, "file_name", "") or "").suffix or ".ogg"
        audio_path = VOICE_DIR / f"{stamp}{suffix}"
        VOICE_DIR.mkdir(parents=True, exist_ok=True)

        telegram_file = await context.bot.get_file(source.file_id)
        await telegram_file.download_to_drive(str(audio_path))
        log.info("принято голосовое %s (%s байт)", audio_path.name, source.file_size)

        duration = getattr(source, "duration", None) or 60
        minutes = max(1, round(duration * TRANSCRIBE_SLOWDOWN / 60))
        busy = transcribe_lock.locked()
        await message.reply_text(
            f"Принял {'голосовую правку' if pending_material else 'запись'}. "
            f"Расшифрую примерно за {minutes} мин, пришлю, когда будет готово."
            + ("\nСейчас в работе другая запись, эта пойдёт следом." if busy else ""),
            reply_markup=menu(waiting_edit=bool(pending_material)),
        )

        context.application.create_task(
            process_voice(context.bot, chat_id, audio_path)
        )

    async def store_topic_note(bot, chat_id: int, topic_id: str, note: str) -> None:
        """Сохранить сказанное автором и вернуть её к выбору формата."""
        state = read_user_state(chat_id)
        state.pop("awaiting_topic_note", None)
        write_user_state(chat_id, state)
        try:
            saved = await asyncio.to_thread(
                save_topic_note_from_environment, topic_id, note
            )
        except Exception:  # noqa: BLE001 — падать боту нельзя
            log.exception("заметка к теме %s не сохранена", topic_id)
            await bot.send_message(
                chat_id,
                "Не смог сохранить заметку. Разработчику сообщил.",
                reply_markup=menu(),
            )
            await tell_dev(bot, f"заметка к теме {topic_id} не сохранена")
            return
        if not saved:
            await bot.send_message(
                chat_id, "Тема больше недоступна.", reply_markup=menu()
            )
            return
        await bot.send_message(
            chat_id,
            "Записал. Ваши слова пойдут в материал выше источников.\n\n"
            "Какой материал подготовить?",
            reply_markup=format_keyboard(topic_id),
        )

    async def process_voice(bot, chat_id: int, audio_path: Path) -> None:
        """Расшифровка и всё, что за ней. Живёт в фоне, ошибок наружу не пускает."""
        # По одной за раз: две расшифровки разом съедят память сервера.
        async with transcribe_lock:
            try:
                target = await asyncio.to_thread(transcribe_audio, audio_path)
            except Exception:  # noqa: BLE001 — падать боту нельзя
                log.exception("расшифровка %s не удалась", audio_path.name)
                await bot.send_message(
                    chat_id,
                    "Не получилось разобрать эту запись — она сохранена, "
                    "разработчик посмотрит, что пошло не так.",
                    reply_markup=menu(),
                )
                return

        text = target.read_text(encoding="utf-8")
        letters = len(text.split("---", 2)[-1].strip())

        # Пустая расшифровка — это тихая запись или шум вместо речи.
        # Молчать нельзя: автор уверена, что наговорила материал, и узнает
        # об обратном через неделю, когда он не появится.
        if letters < 20:
            target.unlink(missing_ok=True)
            await bot.send_message(
                chat_id,
                "Речь в записи не разобрал — похоже, было слишком тихо "
                "или микрофон не поймал голос.\n\n"
                "Наговорите ещё раз, держа телефон ближе. Запись я сохранил, "
                "она не потерялась.",
                reply_markup=menu(),
            )
            log.warning("расшифровка %s пустая: %s знаков", audio_path.name, letters)
            return

        # Состояние перечитываем здесь: пока шла расшифровка, автор могла
        # отменить правку или ответить кнопкой.
        current_state = read_user_state(chat_id)
        pending_note = current_state.get("awaiting_topic_note")
        if pending_note:
            _, spoken = parse_frontmatter(text)
            await store_topic_note(bot, chat_id, pending_note["topic_id"], spoken.strip())
            return

        pending_material = current_state.get("awaiting_material_edit")
        if pending_material:
            _, instruction = parse_frontmatter(text)
            try:
                async with generation_lock:
                    result = await asyncio.to_thread(
                        revise_material_from_environment,
                        pending_material["material_id"],
                        instruction.strip(),
                        "voice",
                    )
            except (AIBudgetExceeded, ValueError) as error:
                target.unlink(missing_ok=True)
                await bot.send_message(
                    chat_id, str(error), reply_markup=menu(waiting_edit=True)
                )
                return
            target.unlink(missing_ok=True)
            state = read_user_state(chat_id)
            state.pop("awaiting_material_edit", None)
            write_user_state(chat_id, state)
            await send_generated_material(bot, chat_id, result)
            log.info("голосовая правка к %s применена", result.material_id)
            return

        await git_pull()
        saved = await git_save(f"transcript: {target.stem}")

        answer = (
            f"Готово, расшифровал — {plural(letters, 'знак', 'знака', 'знаков')}. "
            "Дальше соберу из этого материалы и принесу вам на проверку."
        )
        if not saved:
            answer += "\n\nТекст сохранён, но синхронизация не прошла — разработчик поправит."
            await tell_dev(bot, f"расшифровка {target.name} не отправлена")
        await bot.send_message(chat_id, answer, reply_markup=menu())
        log.info("расшифровано %s, синхронизация: %s", target.name, saved)

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_author(update):
            return
        chat_id = update.effective_chat.id

        # Раньше сюда попадала «отмена» и уезжала в файл как текст правки.
        if (update.message.text or "").strip().casefold() in CANCEL_WORDS:
            await cmd_cancel(update, context)
            return

        # Кнопки присылают текст, а не команду: без перехвата надпись
        # с кнопки уехала бы в файл как правка автора.
        if (update.message.text or "").strip() == BUTTON_REVIEW:
            await git_pull()
            await send_digest(context.bot, update.effective_chat.id)
            return

        if (update.message.text or "").strip() == BUTTON_TOPICS:
            await cmd_topics(update, context)
            return

        if (update.message.text or "").strip() == BUTTON_QUEUE:
            await cmd_queue(update, context)
            return

        state = read_user_state(chat_id)
        pending_date = state.get("awaiting_schedule_date")
        if pending_date:
            try:
                day = parse_author_date(update.message.text)
            except ValueError as error:
                await update.message.reply_text(
                    str(error), reply_markup=menu(waiting_edit=True)
                )
                return
            state.pop("awaiting_schedule_date", None)
            write_user_state(chat_id, state)
            await update.message.reply_text(
                f"Дата: {day:%d.%m.%Y}. Выберите время по Москве:",
                reply_markup=time_keyboard(pending_date["material_id"], day),
            )
            return

        pending_time = state.get("awaiting_schedule_time")
        if pending_time:
            try:
                clock = parse_author_time(update.message.text)
                scheduled_at = moscow_datetime(
                    date.fromisoformat(pending_time["day"]), clock
                )
                if scheduled_at <= datetime.now(MOSCOW):
                    raise ValueError("Это время уже прошло")
            except ValueError as error:
                await update.message.reply_text(
                    str(error), reply_markup=menu(waiting_edit=True)
                )
                return
            state.pop("awaiting_schedule_time", None)
            write_user_state(chat_id, state)
            await show_schedule_confirmation(
                context.bot,
                update.effective_chat.id,
                pending_time["material_id"],
                scheduled_at,
            )
            return

        pending_note = state.get("awaiting_topic_note")
        if pending_note:
            await store_topic_note(
                context.bot, chat_id, pending_note["topic_id"], update.message.text
            )
            return

        pending_material = state.get("awaiting_material_edit")
        if pending_material:
            await update.message.reply_text(
                "Правку принял, переписываю. Это займёт пару минут.",
                reply_markup=menu(),
            )
            context.application.create_task(
                revise_and_send(
                    context.bot,
                    chat_id,
                    pending_material["material_id"],
                    update.message.text,
                )
            )
            return

        pending = state.get("awaiting_edit")

        if not pending:
            items = waiting_materials()
            hint = (
                "Материалы ниже — ответьте кнопкой под любым из них."
                if items
                else "Проверять пока нечего. Можете наговорить голосовое — расшифрую."
            )
            await update.message.reply_text(hint, reply_markup=menu())
            return

        track, _, filename = pending["file"].partition("/")
        path = ROOT / track / "review" / filename
        if not path.exists():
            state.pop("awaiting_edit", None)
            write_user_state(chat_id, state)
            await update.message.reply_text(
                "Этот материал уже обработан, правка не записана.", reply_markup=menu()
            )
            return

        target, saved = await git_request_edit(path, update.message.text)
        state.pop("awaiting_edit", None)
        write_user_state(chat_id, state)
        answer = "Записал вашу правку. Материал вернулся в работу, принесу исправленным."
        if not saved:
            answer += "\n\nПравка сохранена, но синхронизация не прошла — разработчик поправит."
            await tell_dev(context.bot, f"правка к {target.stem} не отправлена")
        await update.message.reply_text(answer, reply_markup=menu())
        log.info("правка к %s, синхронизация: %s", target, saved)

    async def job_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
        await git_pull()
        for recipient_id in author_ids:
            try:
                count = await send_digest(context.bot, recipient_id, quiet_if_empty=True)
            except Exception:  # noqa: BLE001 — остальные авторы должны получить пакет
                log.exception("плановый пакет для %s не ушёл", recipient_id)
                continue
            log.info("плановый пакет для %s: %s материалов", recipient_id, count)

    async def job_content_pipeline(context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            result = await asyncio.to_thread(
                run_content_pipeline,
                retention_days=topic_retention_days,
                source_limit=50,
                max_topics=10,
            )
        except AIBudgetExceeded:
            log.exception("дневное обновление остановлено лимитом AI")
            await notify_content_failure(
                context.bot,
                "Не смог обновить актуальные темы: закончился месячный лимит AI. "
                "Нужно пополнить баланс — напиши Сане.",
            )
            return
        except Exception as error:  # noqa: BLE001 — следующий запуск должен состояться
            log.exception("дневное обновление тем не выполнено")
            await notify_content_failure(
                context.bot,
                f"Не смог обновить актуальные темы ({type(error).__name__}). "
                "Повторю завтра; если темы нужны сейчас — напиши Сане.",
            )
            return

        state = read_state()
        state.pop("last_content_error_notice", None)
        write_state(state)
        log.info(
            "дневное обновление: получено=%s новых=%s ранжировано=%s "
            "тем=%s архив=%s расход=%.4f",
            result.sources_received,
            result.sources_inserted,
            result.sources_ranked,
            result.topics_created,
            result.topics_archived,
            result.estimated_cost_rub,
        )
        if result.ranking_skipped:
            log.info("дневное ранжирование уже выполнялось другим процессом")
            return
        if result.digest is None:
            return
        # Источники собираются каждый день, а темы автору уходят дважды в
        # неделю: очередь на проверку не должна расти быстрее, чем автор
        # её разбирает. В остальные дни дайджест ждёт своей кнопки.
        if datetime.now(MOSCOW).weekday() not in TOPIC_DIGEST_WEEKDAYS:
            log.info("дайджест собран, рассылка не сегодня")
            return
        for recipient_id in author_ids:
            try:
                await send_topic_page(context.bot, recipient_id, result.digest)
            except Exception:  # noqa: BLE001 — темы должны дойти до остальных
                log.exception("темы не отправлены автору %s", recipient_id)
        await asyncio.to_thread(mark_digest_sent_from_environment, result.digest.digest_id)
        if result.budget_warning:
            await context.bot.send_message(
                author_id,
                f"Расход AI за этот месяц превысил {budget_warning_rub:.0f} ₽. "
                f"Генерация продолжит работать до жёсткого лимита "
                f"{budget_hard_limit_rub:.0f} ₽.",
                reply_markup=menu(),
            )

    async def job_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
        items = waiting_materials()
        if not items:
            return
        now = datetime.now(timezone.utc)
        word = plural(len(items), "материал", "материала", "материалов")
        verb = "ждёт" if len(items) % 10 == 1 and len(items) % 100 != 11 else "ждут"
        for recipient_id in author_ids:
            state = read_user_state(recipient_id)
            last_reminder = state.get("last_reminder")
            if last_reminder and now - datetime.fromisoformat(last_reminder) < timedelta(
                hours=20
            ):
                continue
            anchor = state.get("last_digest")
            if anchor and now - datetime.fromisoformat(anchor) < timedelta(
                days=REMINDER_AFTER_DAYS
            ):
                continue
            await context.bot.send_message(
                recipient_id,
                f"Тихое напоминание: {word} {verb} вашей проверки. "
                "Откроете, когда будет минута — кнопка внизу.",
                reply_markup=menu(),
            )
            state["last_reminder"] = now.isoformat()
            write_user_state(recipient_id, state)
            log.info("напоминание отправлено: %s", recipient_id)

    async def job_publish_due(context: ContextTypes.DEFAULT_TYPE) -> None:
        # Отправки, оборвавшиеся между send_message и записью message_id,
        # не повторяем: пост мог уже уйти. Их разбирает человек.
        stuck = await asyncio.to_thread(release_stuck_from_environment)
        for publication_id in stuck:
            log.error("отправка %s прервалась, нужна проверка канала", publication_id)
            await tell_dev(
                context.bot,
                f"публикация {publication_id} прервалась на полпути — "
                "проверь канал, автоматически не повторяю",
            )

        publication = await asyncio.to_thread(claim_due_from_environment)
        if publication is None:
            return
        try:
            if len(publication.body) > TELEGRAM_LIMIT:
                raise ValueError(
                    f"текст длиннее {TELEGRAM_LIMIT} знаков; нужна правка"
                )
            # Последний рубеж перед каналом. Стоит дёшево — регулярки по
            # готовому тексту, — а цена пропуска здесь максимальная.
            violations = hard_violations(publication.body, publication.material_id)
            if violations:
                raise ValueError(
                    "текст не проходит красные линии: "
                    + "; ".join(item.split("] ", 1)[-1] for item in violations[:3])
                )
            if publication.destination == "channel":
                raw_target = config.get("TELEGRAM_CHANNEL_ID", "")
                if not raw_target:
                    raise ValueError("не настроен TELEGRAM_CHANNEL_ID")
                target = int(raw_target) if raw_target.lstrip("-").isdigit() else raw_target
            else:
                target = publish_author_id
            message = await context.bot.send_message(target, publication.body)
        except Exception as error:  # noqa: BLE001 — очередь должна пережить сбой
            await asyncio.to_thread(
                mark_failed_from_environment, publication.id, str(error)
            )
            log.exception("публикация %s не отправлена", publication.id)
            if publication.attempts + 1 >= 3:
                await context.bot.send_message(
                    author_id,
                    f"Не смог отправить «{publication.title}» после трёх попыток. "
                    "Напиши Сане — он проверит бота.",
                    reply_markup=menu(),
                )
            return
        await asyncio.to_thread(
            mark_published_from_environment, publication.id, message.message_id
        )
        log.info("публикация %s отправлена: %s", publication.id, message.message_id)

        # Опубликованное уезжает в репозиторий: база держит рабочее
        # состояние, git — архив. Иначе вышедший в канал текст не найти
        # нигде, кроме самого канала.
        try:
            target = await asyncio.to_thread(
                export_from_environment,
                publication.material_id,
                status="published",
                extra={
                    "message_id": str(message.message_id),
                    "опубликовано": datetime.now(MOSCOW).date().isoformat(),
                },
            )
        except Exception:  # noqa: BLE001 — пост уже вышел, архив не критичен
            log.exception("публикация %s не выгружена в репозиторий", publication.id)
            return
        if not await git_save(f"published: {target.stem}"):
            await tell_dev(context.bot, f"архив публикации {target.name} не отправлен")

    async def post_init(app) -> None:
        # Кнопка «Меню» в телеграме: подписи по-русски, чтобы список команд
        # не выглядел набором английских слов.
        await app.bot.set_my_commands([
            BotCommand("topics", "выбрать актуальную тему"),
            BotCommand("queue", "очередь публикаций"),
            BotCommand("review", "показать материалы на проверке"),
            BotCommand("status", "что сейчас в работе"),
            BotCommand("cancel", "не буду править"),
        ])

        # Реестр тем приезжает вместе с кодом: после git pull он может быть
        # другим, а автор не должна знать про отдельную команду импорта.
        try:
            available, closed = await asyncio.to_thread(sync_registry_from_environment)
            log.info("реестр тем: %s доступно, %s закрыто", available, closed)
        except Exception:  # noqa: BLE001 — без реестра бот работает на потоке PubMed
            log.exception("реестр тем не синхронизирован")

    application = (
        Application.builder().token(config["TELEGRAM_BOT_TOKEN"]).post_init(post_init).build()
    )
    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Ни одна ошибка не должна выглядеть для автора как тишина.

        Без этого обработчика телеграм-библиотека просто пишет трейсбек в
        лог: автор жмёт кнопку, ничего не происходит, и понять, дошло или
        нет, невозможно.
        """
        log.error("сбой при обработке", exc_info=context.error)

        chat = getattr(getattr(update, "effective_chat", None), "id", None)
        if chat is None:
            return
        try:
            await context.bot.send_message(
                chat,
                "Что-то пошло не так на моей стороне. Ваш ответ мог не записаться — "
                "загляните сюда чуть позже, я покажу, что осталось на проверке.",
                reply_markup=menu(),
            )
        except Exception:  # noqa: BLE001 — из обработчика ошибок падать нельзя
            log.exception("не смог сообщить автору о сбое")
        await tell_dev(context.bot, f"сбой в боте: {type(context.error).__name__}")

    application.add_error_handler(on_error)
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("topics", cmd_topics))
    application.add_handler(CommandHandler("queue", cmd_queue))
    application.add_handler(CommandHandler("review", cmd_review))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE
            & (filters.VOICE | filters.AUDIO | filters.Document.AUDIO),
            on_voice,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, on_text
        )
    )

    queue = application.job_queue
    queue.run_daily(
        job_content_pipeline,
        time(hour=content_pipeline_hour, tzinfo=MOSCOW),
        name="daily-content-pipeline",
    )
    queue.run_daily(job_digest, time(hour=DIGEST_HOUR, tzinfo=MOSCOW))
    queue.run_daily(job_reminder, time(hour=REMINDER_HOUR, tzinfo=MOSCOW))
    queue.run_repeating(job_publish_due, interval=30, first=10)
    return application


def check_config(config: dict[str, str], strict: bool = False) -> int:
    problems = []
    if not config.get("TELEGRAM_BOT_TOKEN"):
        problems.append("нет TELEGRAM_BOT_TOKEN — токен выдаёт @BotFather")
    if not config.get("AUTHOR_CHAT_ID"):
        problems.append("нет AUTHOR_CHAT_ID — id автора, узнаётся у @userinfobot")
    elif not config["AUTHOR_CHAT_ID"].lstrip("-").isdigit():
        problems.append("AUTHOR_CHAT_ID должен быть числом")
    try:
        configured_author_ids = parse_author_chat_ids(config)
    except ValueError as error:
        configured_author_ids = ()
        problems.append(str(error))
    raw_publish_author = config.get("PUBLISH_AUTHOR_CHAT_ID", "").strip()
    if raw_publish_author and not raw_publish_author.lstrip("-").isdigit():
        problems.append("PUBLISH_AUTHOR_CHAT_ID должен быть числом")
    elif raw_publish_author and int(raw_publish_author) not in configured_author_ids:
        problems.append("PUBLISH_AUTHOR_CHAT_ID должен входить в список авторов")
    publish_target = config.get("TELEGRAM_PUBLISH_TARGET", "author").strip() or "author"
    if publish_target not in {"author", "channel"}:
        problems.append("TELEGRAM_PUBLISH_TARGET должен быть author или channel")
    if publish_target == "channel" and not config.get("TELEGRAM_CHANNEL_ID"):
        problems.append("для режима channel нужен TELEGRAM_CHANNEL_ID")
    try:
        content_hour = int(
            config.get("CONTENT_PIPELINE_HOUR", str(DEFAULT_CONTENT_PIPELINE_HOUR))
        )
        if not 0 <= content_hour <= 23:
            raise ValueError
    except ValueError:
        problems.append("CONTENT_PIPELINE_HOUR должен быть целым числом от 0 до 23")
    try:
        retention_days = int(
            config.get("TOPIC_RETENTION_DAYS", str(DEFAULT_TOPIC_RETENTION_DAYS))
        )
        if retention_days <= 0:
            raise ValueError
    except ValueError:
        problems.append("TOPIC_RETENTION_DAYS должен быть целым числом больше нуля")

    # Контентный конвейер живёт на базе и внешней модели. Без --strict это
    # предупреждения: в CI смоук-запуск идёт с одним токеном. С --strict это
    # ошибки, и на нём стоит healthcheck контейнера — иначе бот считался
    # здоровым при недоступном Postgres, а вскрывалось это в 08:00 утра.
    infrastructure = [
        ("DATABASE_URL", "нет DATABASE_URL — контентный конвейер без базы не работает"),
        ("TIMEWEB_AI_API_KEY", "нет TIMEWEB_AI_API_KEY — генерация недоступна"),
        ("AI_MODEL_FAST", "нет AI_MODEL_FAST — модель для ранжирования тем"),
        ("AI_MODEL_WRITER", "нет AI_MODEL_WRITER — модель для текста материала"),
        ("NCBI_EMAIL", "нет NCBI_EMAIL — PubMed требует контакт в запросе"),
    ]
    missing_infrastructure = [note for key, note in infrastructure if not config.get(key)]

    database_note = "не проверял"
    if config.get("DATABASE_URL"):
        try:
            from database import DatabaseSettings, check_connection, connect

            os.environ.setdefault("DATABASE_URL", config["DATABASE_URL"])
            with connect(DatabaseSettings.from_mapping(config)) as connection:
                _, database_name = check_connection(connection)
            database_note = f"доступна ({database_name})"
        except Exception as error:  # noqa: BLE001 — сообщение важнее типа
            database_note = f"недоступна: {type(error).__name__}"
            if strict:
                problems.append(f"база данных недоступна: {error}")

    print(f"Материалов на проверке: {len(waiting_materials())}")
    print(f"Remote настроен: {'да' if has_remote() else 'нет'}")
    print(f"Состояние: {STATE_FILE.relative_to(ROOT)}")
    print(f"База: {database_note}")

    if missing_infrastructure:
        if strict:
            problems.extend(missing_infrastructure)
        else:
            print("\nПредупреждения:")
            for item in missing_infrastructure:
                print(f"  ~ {item}")

    if problems:
        print("\nНе хватает:")
        for item in problems:
            print(f"  - {item}")
        print("\nЗаполняется в .env, образец — .env.example")
        return 1

    print("\nКонфигурация на месте, бот готов к запуску.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Фактчек-бот проверки материалов.")
    parser.add_argument("--check", action="store_true", help="проверить конфигурацию и выйти")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="при проверке считать ошибкой недоступную базу и пустые AI-ключи",
    )
    args = parser.parse_args()

    setup_logging()
    config = load_env()
    apply_environment(config)

    if args.check:
        return check_config(config, strict=args.strict)

    missing = [k for k in ("TELEGRAM_BOT_TOKEN", "AUTHOR_CHAT_ID") if not config.get(k)]
    if missing:
        sys.exit(f"review_bot: в .env не хватает {', '.join(missing)}. Проверка: --check")

    log.info("бот запускается")
    clear_stale_lock()
    migrate_state(int(config["AUTHOR_CHAT_ID"]))
    application = build_application(config)
    application.run_polling(drop_pending_updates=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
