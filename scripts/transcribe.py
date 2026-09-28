#!/usr/bin/env python3
"""Расшифровка голосовых. Крутится на сервере рядом с ботом.

    python3 scripts/transcribe.py core/voice/2026-08-05.ogg
    python3 scripts/transcribe.py core/voice           вся папка
    python3 scripts/transcribe.py файл --timestamps    строки с таймкодами
    python3 scripts/transcribe.py файл --model medium  модель полегче

Вход — аудио из core/voice/, выход — текст в core/transcripts/ с тем же
именем и расширением .md. Уже расшифрованное пропускается, если не задан
--force.

Движок — faster-whisper на CPU: он одинаково работает на сервере и на
маке, в отличие от mlx, который есть только на Apple Silicon. Модель по
умолчанию medium в int8: она помещается в лимит памяти прод-контейнера и
не перезапускает бота на длинных записях.

Зависимость одна — faster-whisper. Скрипт сам перезапускается под .venv,
поэтому вызывать можно обычным python3.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICE = ROOT / "core" / "voice"
TRANSCRIPTS = ROOT / "core" / "transcripts"
VENV_PYTHON = ROOT / ".venv" / "bin" / "python3"

MODEL = os.environ.get("WHISPER_MODEL", "medium")
COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE", "int8")
LANGUAGE = "ru"
AUDIO_SUFFIXES = {".m4a", ".mp3", ".wav", ".ogg", ".oga", ".aiff", ".aac", ".flac", ".mp4", ".opus"}

# Пауза, после которой начинается новый абзац.
PARAGRAPH_GAP = 1.5

# Термины, которые модель стабильно портит без подсказки. Дописывать по
# факту ошибок в расшифровках, не превентивно.
PROMPT = (
    "Голосовая заметка по эндокринологии. Термины: ТТГ, Т4 свободный, "
    "антитела к ТПО, инсулин, лептин, грелин, кортизол, пролактин, СПКЯ, "
    "щитовидная железа, гипотиреоз, инсулинорезистентность."
)


def reexec_in_venv() -> None:
    """Перезапуск под .venv — faster-whisper стоит там, а не в системном python.

    Сравнение идёт по sys.prefix, а не по пути интерпретатора:
    .venv/bin/python3 — симлинк на системный, и resolve() схлопывает их
    в один путь.
    """
    if os.environ.get("TRANSCRIBE_REEXEC") == "1":
        return
    if Path(sys.prefix).resolve() == (ROOT / ".venv").resolve():
        return
    if not VENV_PYTHON.exists():
        return
    os.environ["TRANSCRIBE_REEXEC"] = "1"
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])


def collect_audio(targets: list[str]) -> list[Path]:
    paths: list[Path] = []
    for item in targets or [str(VOICE)]:
        path = Path(item)
        if path.is_dir():
            paths.extend(sorted(p for p in path.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES))
        elif path.is_file():
            paths.append(path)
        else:
            sys.exit(f"transcribe: нет такого пути: {item}")
    return paths


def as_paragraphs(segments: list[tuple[float, float, str]]) -> str:
    """Склейка сегментов в абзацы по паузам в речи."""
    paragraphs: list[list[str]] = []
    current: list[str] = []
    previous_end = 0.0

    for start, end, text in segments:
        text = text.strip()
        if not text:
            continue
        if current and start - previous_end >= PARAGRAPH_GAP:
            paragraphs.append(current)
            current = []
        current.append(text)
        previous_end = end

    if current:
        paragraphs.append(current)
    return "\n\n".join(" ".join(p) for p in paragraphs)


def as_timestamps(segments: list[tuple[float, float, str]]) -> str:
    rows = []
    for start, _end, text in segments:
        text = text.strip()
        if not text:
            continue
        moment = int(start)
        rows.append(f"[{moment // 60:02d}:{moment % 60:02d}] {text}")
    return "\n".join(rows)


def duration_label(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


# Whisper на тишине и шуме уверенно выдаёт фразы из титров: ими забита
# обучающая выборка. Прилетают они без единого признака ошибки, поэтому
# в расшифровку такое попадает молча — и уезжает в репозиторий.
HALLUCINATIONS = (
    "продолжение следует",
    "субтитры сделал",
    "субтитры создавал",
    "редактор субтитров",
    "корректор",
    "спасибо за просмотр",
    "подписывайтесь на канал",
    "dimatorzok",
)


def is_hallucination(text: str) -> bool:
    stripped = text.strip().strip(".…!?").casefold()
    return any(mark in stripped for mark in HALLUCINATIONS) and len(stripped) < 60


def normalize_audio(path: Path) -> tuple[Path, bool]:
    """Выровнять громкость перед расшифровкой.

    Автор диктует как придётся: лёжа, на ходу, вполголоса. Тихая запись
    для VAD выглядит как тишина, и он вырезает речь целиком. Приведение
    к вещательной громкости снимает это раз и навсегда.

    Возвращает путь к подготовленному файлу и признак, что он временный.
    """
    target = Path(tempfile.gettempdir()) / f"{path.stem}-16k.wav"
    result = subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y", "-i", str(path),
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar", "16000", "-ac", "1", str(target),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not target.exists():
        print(f"  громкость выровнять не вышло, беру как есть: {result.stderr.strip()[:120]}")
        return path, False
    return target, True


def transcribe(path: Path, model_name: str, timestamps: bool) -> str:
    from faster_whisper import WhisperModel

    source, temporary = normalize_audio(path)
    model = WhisperModel(model_name, device="cpu", compute_type=COMPUTE_TYPE)
    raw, info = model.transcribe(
        str(source),
        language=LANGUAGE,
        initial_prompt=PROMPT,
        vad_filter=True,
        beam_size=5,
    )

    segments = [(s.start, s.end, s.text) for s in raw if not is_hallucination(s.text)]
    if temporary:
        source.unlink(missing_ok=True)
    body = as_timestamps(segments) if timestamps else as_paragraphs(segments)

    header = [
        "---",
        f"источник_аудио: {path.name}",
        f"длительность: {duration_label(info.duration)}",
        f"модель: {model_name}",
        f"расшифровано: {date.today().isoformat()}",
        "статус: сырьё",
        "---",
        "",
    ]
    return "\n".join(header) + body + "\n"


def transcribe_file(path: Path, model_name: str, timestamps: bool, force: bool) -> Path | None:
    """Один файл: расшифровать и записать. Возвращает путь к тексту."""
    target = TRANSCRIPTS / f"{path.stem}.md"
    if target.exists() and not force:
        return target

    TRANSCRIPTS.mkdir(parents=True, exist_ok=True)
    target.write_text(transcribe(path, model_name, timestamps), encoding="utf-8")
    return target


def main() -> int:
    reexec_in_venv()

    parser = argparse.ArgumentParser(description="Расшифровка голосовых.")
    parser.add_argument("paths", nargs="*", help="файлы или папка, по умолчанию core/voice")
    parser.add_argument("--model", default=MODEL, help=f"модель, по умолчанию {MODEL}")
    parser.add_argument("--timestamps", action="store_true", help="строки с таймкодами вместо абзацев")
    parser.add_argument("--force", action="store_true", help="перезаписать существующую расшифровку")
    args = parser.parse_args()

    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        sys.exit(
            "transcribe: нет faster-whisper.\n"
            "  python3 -m venv .venv && .venv/bin/pip install faster-whisper"
        )

    files = collect_audio(args.paths)
    if not files:
        print("Аудио не найдено. Голосовые кладутся в core/voice/.")
        return 0

    done = 0
    for path in files:
        target = TRANSCRIPTS / f"{path.stem}.md"
        if target.exists() and not args.force:
            print(f"пропуск, уже есть: {target.relative_to(ROOT)}")
            continue

        print(f"расшифровываю {path.name} ...", flush=True)
        try:
            result = transcribe_file(path, args.model, args.timestamps, args.force)
        except Exception as error:  # noqa: BLE001 — причин много, все внешние
            print(f"transcribe: {path.name} не расшифрован — {error}", file=sys.stderr)
            continue

        print(f"  → {result.relative_to(ROOT)}")
        done += 1

    print(f"\nГотово: {done} из {len(files)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
