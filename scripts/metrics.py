#!/usr/bin/env python3
"""Метрики публикаций: дописать строку, посмотреть сводку.

    python3 scripts/metrics.py add r-001 --площадка ig --охват 4200 \
        --реакции 180 --сохранения 95 --подписки 40
    python3 scripts/metrics.py show
    python3 scripts/metrics.py show --ниша щитовидная_железа

Цифры вносятся руками раз в две недели, при разборе. Автоподбор тем по
метрикам не делается сознательно: он потянет в сторону веса и диет,
потому что там охваты.

Рубрика и ниша подтягиваются из фронтматтера материала, если он найден.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
METRICS = ROOT / "data" / "metrics.csv"
COLUMNS = [
    "дата", "площадка", "материал", "рубрика", "ниша",
    "охват", "реакции", "комментарии", "сохранения", "подписки", "переходы_в_tg",
]


def find_material(material_id: str) -> dict[str, str]:
    """Ищет материал по id во всех папках статусов, читает фронтматтер."""
    for track in ("reels", "tg"):
        for path in (ROOT / track).rglob(f"{material_id}.md"):
            text = path.read_text(encoding="utf-8")
            if not text.startswith("---"):
                continue
            end = text.find("\n---", 3)
            fields = {}
            for line in text[3 : end if end != -1 else len(text)].splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    fields[key.strip()] = value.strip().strip('"').strip("'")
            return fields
    return {}


def read_rows() -> list[dict[str, str]]:
    if not METRICS.exists():
        return []
    with METRICS.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def cmd_add(args: argparse.Namespace) -> int:
    fields = find_material(args.material)
    if not fields:
        print(f"Материал {args.material} не найден, рубрика и ниша останутся пустыми.")

    row = {
        "дата": args.дата or date.today().isoformat(),
        "площадка": args.площадка,
        "материал": args.material,
        "рубрика": args.рубрика or fields.get("рубрика", ""),
        "ниша": args.ниша or fields.get("ниша", ""),
        "охват": args.охват,
        "реакции": args.реакции,
        "комментарии": args.комментарии,
        "сохранения": args.сохранения,
        "подписки": args.подписки,
        "переходы_в_tg": args.переходы,
    }

    exists = METRICS.exists() and METRICS.stat().st_size > 0
    METRICS.parent.mkdir(parents=True, exist_ok=True)
    with METRICS.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerow(row)

    print(f"Записано: {row['материал']} · {row['площадка']} · охват {row['охват']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    rows = read_rows()
    if args.ниша:
        rows = [r for r in rows if r.get("ниша") == args.ниша]
    if args.площадка:
        rows = [r for r in rows if r.get("площадка") == args.площадка]

    if not rows:
        print("Данных нет. Цифры вносятся вручную: scripts/metrics.py add")
        return 0

    print(f"{'материал':<10} {'площадка':<9} {'ниша':<20} {'охват':>8} {'подписки':>9}")
    for row in rows[-20:]:
        print(
            f"{row['материал']:<10} {row['площадка']:<9} {row.get('ниша', ''):<20} "
            f"{row.get('охват', ''):>8} {row.get('подписки', ''):>9}"
        )

    def total(column: str) -> int:
        return sum(int(r[column]) for r in rows if r.get(column, "").isdigit())

    print()
    print(f"Записей: {len(rows)}. Суммарный охват: {total('охват')}, подписки: {total('подписки')}.")

    by_niche: dict[str, list[int]] = {}
    for row in rows:
        if row.get("охват", "").isdigit():
            by_niche.setdefault(row.get("ниша", "—"), []).append(int(row["охват"]))
    if len(by_niche) > 1:
        print("\nСредний охват по нишам:")
        for niche, values in sorted(by_niche.items(), key=lambda i: -sum(i[1]) / len(i[1])):
            print(f"  {niche:<22} {sum(values) // len(values):>7}  ({len(values)} шт)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Метрики публикаций.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="дописать строку")
    p_add.add_argument("material", help="id материала, например r-001")
    p_add.add_argument("--площадка", required=True, choices=["ig", "tg"])
    p_add.add_argument("--дата", default="")
    p_add.add_argument("--рубрика", default="")
    p_add.add_argument("--ниша", default="")
    p_add.add_argument("--охват", default="")
    p_add.add_argument("--реакции", default="")
    p_add.add_argument("--комментарии", default="")
    p_add.add_argument("--сохранения", default="")
    p_add.add_argument("--подписки", default="")
    p_add.add_argument("--переходы", default="", help="переходы в telegram")

    p_show = sub.add_parser("show", help="сводка")
    p_show.add_argument("--ниша", default="")
    p_show.add_argument("--площадка", default="", choices=["", "ig", "tg"])

    args = parser.parse_args()
    if args.command == "add":
        return cmd_add(args)
    return cmd_show(args)


if __name__ == "__main__":
    sys.exit(main())
