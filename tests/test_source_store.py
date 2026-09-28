from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from collect_sources import SourceItem  # noqa: E402
from source_store import PUBMED_DEFINITION, SourceStore  # noqa: E402


class FakeCursor:
    def __init__(self, returned_rows):
        self.returned_rows = iter(returned_rows)
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query, params):
        self.executions.append((query, params))

    def fetchone(self):
        return next(self.returned_rows)


class FakeConnection:
    def __init__(self, returned_rows):
        self.cursor_instance = FakeCursor(returned_rows)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def item(external_id: str, *, preprint: bool = False, source: str = "PubMed") -> SourceItem:
    return SourceItem(
        source=source,
        external_id=external_id,
        url=f"https://pubmed.ncbi.nlm.nih.gov/{external_id}/",
        title=f"Title {external_id}",
        summary="Journal",
        language="en",
        published_at="2026-08-20T00:00:00+00:00",
        content_hash=f"hash-{external_id}",
        is_preprint=preprint,
        raw_payload={"publication_types": ["Guideline"]},
    )


class SourceStoreTest(unittest.TestCase):
    def test_inserts_new_items_and_counts_duplicates_and_preprints(self):
        # source id, новый item id, затем None для конфликта-дубля.
        connection = FakeConnection([(7,), (101,), None])
        result = SourceStore(connection).save_items(
            [item("101"), item("102"), item("103", preprint=True)],
            PUBMED_DEFINITION,
        )

        self.assertEqual(result.received, 3)
        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.preprints_excluded, 1)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        self.assertEqual(len(connection.cursor_instance.executions), 3)
        self.assertIn("ON CONFLICT DO NOTHING", connection.cursor_instance.executions[1][0])

    def test_rolls_back_mixed_source_batch(self):
        connection = FakeConnection([(7,)])

        with self.assertRaisesRegex(ValueError, "а не к PubMed"):
            SourceStore(connection).save_items(
                [item("101", source="Another source")],
                PUBMED_DEFINITION,
            )

        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)


if __name__ == "__main__":
    unittest.main()
