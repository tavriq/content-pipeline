from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import material_export  # noqa: E402
from material_export import export, next_number, render  # noqa: E402

SOURCES = ({"source": "PubMed", "url": "https://example.test/paper"},)


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "reels" / "approved").mkdir(parents=True)
        (self.root / "reels" / "queue").mkdir(parents=True)
        self.original_root = material_export.ROOT
        material_export.ROOT = self.root

    def tearDown(self):
        material_export.ROOT = self.original_root
        self.directory.cleanup()

    def test_numbers_continue_across_status_folders(self):
        (self.root / "reels" / "queue" / "r-002.md").write_text("x", encoding="utf-8")
        (self.root / "reels" / "approved" / "r-007.md").write_text("x", encoding="utf-8")

        self.assertEqual(next_number("r", "reels"), "r-008")
        # У TikTok своя нумерация: это другая площадка, а не продолжение.
        self.assertEqual(next_number("k", "reels"), "k-001")

    def test_writes_script_with_fields_the_pipeline_reads(self):
        target = export(
            material_id="mat-1",
            format_name="reels",
            version=2,
            title="Почему щитовидная железа чаще у женщин",
            sources=SOURCES,
            body="ХУК: спокойный вопрос по существу.\n\nОЗВУЧКА: объяснение механизма.",
        )

        text = target.read_text(encoding="utf-8")
        self.assertEqual(target.name, "r-001.md")
        for field in ("id:", "track: reels", "статус: approved", "источник:", "создан:"):
            self.assertIn(field, text)
        self.assertIn("https://example.test/paper", text)
        self.assertIn("ОЗВУЧКА", text)

    def test_refuses_script_breaking_red_lines(self):
        with self.assertRaisesRegex(ValueError, "красные линии"):
            export(
                material_id="mat-2",
                format_name="reels",
                version=1,
                title="Тема",
                sources=SOURCES,
                body="ХУК: схема простая.\nПринимать по 50\nмкг утром натощак.",
            )
        self.assertEqual(list((self.root / "reels" / "approved").glob("*.md")), [])

    def test_published_post_lands_in_the_archive_with_message_id(self):
        # Telegram-пост выгружается уже опубликованным: база — рабочее
        # состояние, git — архив вышедшего.
        (self.root / "tg" / "published").mkdir(parents=True)
        target = export(
            material_id="mat-3",
            format_name="telegram",
            version=1,
            title="Тема",
            sources=SOURCES,
            body="Спокойный образовательный текст.",
            status="published",
            extra={"message_id": "451"},
        )

        self.assertEqual(target.parent.name, "published")
        self.assertEqual(target.name, "t-001.md")
        self.assertIn("message_id: 451", target.read_text(encoding="utf-8"))

    def test_registry_topic_is_marked_used_in_the_file(self):
        # Реестр читает человек, база — бот. Без обратной отметки одна тема
        # ушла бы в работу дважды: через бота и через topics.py take.
        import material_export
        import topics as registry

        original = registry.TOPICS.read_text(encoding="utf-8")
        target = self.root / "reels" / "approved" / "r-999.md"
        try:
            material_export.mark_registry_topic("registry-005", target)
            _, entries = registry.load(registry.TOPICS)
            entry = registry.find(entries, 5)
            self.assertEqual(entry.status, registry.USED)
            self.assertEqual(entry.used_in, "reels/approved/r-999.md")
        finally:
            registry.TOPICS.write_text(original, encoding="utf-8")

    def test_marking_ignores_topics_that_came_from_pubmed(self):
        import material_export
        import topics as registry

        original = registry.TOPICS.read_text(encoding="utf-8")
        material_export.mark_registry_topic(
            "topic-abc123", self.root / "tg" / "approved" / "t-1.md"
        )
        self.assertEqual(registry.TOPICS.read_text(encoding="utf-8"), original)

    def test_render_keeps_frontmatter_parseable(self):
        text = render(
            file_id="k-004",
            material_id="mat-4",
            format_name="tiktok",
            version=1,
            title="Тема",
            sources=SOURCES,
            body="ХУК: текст.",
        )
        head = text.split("---")[1]
        fields = dict(
            line.split(":", 1) for line in head.strip().splitlines() if ":" in line
        )
        self.assertEqual(fields["id"].strip(), "k-004")
        self.assertEqual(fields["track"].strip(), "tiktok")


if __name__ == "__main__":
    unittest.main()
