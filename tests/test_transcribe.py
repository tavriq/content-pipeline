from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


def load_transcribe(environment: dict[str, str]):
    spec = importlib.util.spec_from_file_location("transcribe_under_test", ROOT / "scripts/transcribe.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    with patch.dict(os.environ, environment, clear=True):
        spec.loader.exec_module(module)
    return module


class TranscribeSettingsTest(unittest.TestCase):
    def test_uses_medium_by_default(self):
        self.assertEqual(load_transcribe({}).MODEL, "medium")

    def test_allows_environment_override(self):
        self.assertEqual(load_transcribe({"WHISPER_MODEL": "small"}).MODEL, "small")


if __name__ == "__main__":
    unittest.main()
