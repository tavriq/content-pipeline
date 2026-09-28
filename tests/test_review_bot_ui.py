from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import review_bot  # noqa: E402
from review_bot import (  # noqa: E402
    callback_data,
    parse_author_chat_ids,
    read_user_state,
    split_telegram_text,
    write_user_state,
)


class TelegramUiHelpersTest(unittest.TestCase):
    def test_splits_long_text_without_losing_words(self):
        text = "Первый абзац.\n\n" + "слово " * 30
        chunks = split_telegram_text(text, limit=60)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 60 for chunk in chunks))
        self.assertEqual(" ".join(" ".join(chunks).split()), " ".join(text.split()))

    def test_callback_rejects_telegram_limit_overflow(self):
        self.assertEqual(callback_data("topic", "abc"), "topic:abc")
        material_id = "material-" + "a" * 32
        scheduled = callback_data("st", material_id, "2026-08-25", "1830")
        confirmed = callback_data("sc", material_id, 1787686200)
        self.assertLessEqual(len(scheduled.encode("utf-8")), 64)
        self.assertLessEqual(len(confirmed.encode("utf-8")), 64)
        with self.assertRaisesRegex(ValueError, "64"):
            callback_data("topic", "x" * 60)

    def test_parses_primary_and_additional_authors_without_duplicates(self):
        result = parse_author_chat_ids(
            {"AUTHOR_CHAT_ID": "101", "AUTHOR_CHAT_IDS": "202, 303;101"}
        )
        self.assertEqual(result, (101, 202, 303))
        with self.assertRaisesRegex(ValueError, "числовые"):
            parse_author_chat_ids(
                {"AUTHOR_CHAT_ID": "101", "AUTHOR_CHAT_IDS": "@username"}
            )

    def test_keeps_conversation_state_separate_for_each_author(self):
        original_state_file = review_bot.STATE_FILE
        try:
            with tempfile.TemporaryDirectory() as directory:
                review_bot.STATE_FILE = Path(directory) / "bot-state.json"
                write_user_state(101, {"awaiting_edit": {"file": "one"}})
                write_user_state(202, {"awaiting_edit": {"file": "two"}})

                self.assertEqual(read_user_state(101)["awaiting_edit"]["file"], "one")
                self.assertEqual(read_user_state(202)["awaiting_edit"]["file"], "two")
        finally:
            review_bot.STATE_FILE = original_state_file

    def test_button_answers_do_not_depend_on_query_message(self):
        # У кнопки под сообщением старше 48 часов query.message приходит
        # как InaccessibleMessage — без reply_text. Автор жала кнопку,
        # кнопки снимались, материал не одобрялся, в ответ приходило
        # «что-то пошло не так». Отвечаем по chat_id.
        source = (ROOT / "scripts" / "review_bot.py").read_text(encoding="utf-8")
        self.assertNotIn("query.message.reply_text", source)
        self.assertNotIn("query.message.chat.id", source)

    def test_group_id_is_not_accepted_as_author(self):
        # Отрицательный ID — это группа: права автора получил бы каждый
        # её участник, включая одобрение материалов и публикацию в канал.
        with self.assertRaisesRegex(ValueError, "личным чатом"):
            parse_author_chat_ids({"AUTHOR_CHAT_ID": "-1001234567890"})

    def test_bot_commits_only_its_own_paths(self):
        # «git add -A» дважды утаскивал в репозиторий лишнее: сначала
        # pip-кэш на 124 МБ, потом веса whisper.
        source = (ROOT / "scripts" / "review_bot.py").read_text(encoding="utf-8")
        self.assertNotIn('git("add", "-A")', source)
        self.assertIn("COMMIT_PATHS", source)


if __name__ == "__main__":
    unittest.main()
