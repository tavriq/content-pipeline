from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from content_pipeline import run_content_pipeline  # noqa: E402


def connection() -> MagicMock:
    value = MagicMock()
    value.__enter__.return_value = value
    value.__exit__.return_value = False
    return value


class ContentPipelineTest(unittest.TestCase):
    def test_skips_ai_when_pubmed_has_no_unranked_items(self):
        digest = SimpleNamespace(digest_id="digest-1")
        with (
            patch("content_pipeline.read_config", return_value={"pubmed": {"enabled": True}}),
            patch("content_pipeline.PubMedSource") as source_class,
            patch("content_pipeline.DatabaseSettings.from_environment"),
            patch(
                "content_pipeline.connect",
                side_effect=[connection(), connection(), connection()],
            ),
            patch("content_pipeline.SourceStore") as source_store_class,
            patch("content_pipeline.TopicRankingStore") as ranking_store_class,
            patch("content_pipeline.ContentStore") as content_store_class,
            patch("content_pipeline.AIGateway") as gateway_class,
        ):
            source_class.return_value.fetch.return_value = []
            source_store_class.return_value.save_items.return_value = SimpleNamespace(
                received=0, inserted=0
            )
            ranking_store_class.return_value.acquire_lock.return_value = True
            ranking_store_class.return_value.pending_items.return_value = []
            content_store_class.return_value.archive_stale_topics.return_value = 2
            content_store_class.return_value.create_digest.return_value = digest

            result = run_content_pipeline()

        gateway_class.from_environment.assert_not_called()
        self.assertEqual(result.topics_archived, 2)
        self.assertEqual(result.digest.digest_id, "digest-1")
        self.assertEqual(result.estimated_cost_rub, Decimal("0"))

    def test_ranks_only_pending_sources_and_returns_cost(self):
        pending = [SimpleNamespace(id=11), SimpleNamespace(id=12)]
        ai_result = SimpleNamespace(
            estimated_cost_rub=Decimal("1.25"), budget_warning=True
        )
        with (
            patch("content_pipeline.read_config", return_value={"pubmed": {"enabled": True}}),
            patch("content_pipeline.PubMedSource") as source_class,
            patch("content_pipeline.DatabaseSettings.from_environment"),
            patch(
                "content_pipeline.connect",
                side_effect=[connection(), connection(), connection()],
            ),
            patch("content_pipeline.SourceStore") as source_store_class,
            patch("content_pipeline.TopicRankingStore") as ranking_store_class,
            patch("content_pipeline.ContentStore") as content_store_class,
            patch("content_pipeline.AIGateway"),
            patch("content_pipeline.rank_topics") as rank_topics,
        ):
            source_class.return_value.fetch.return_value = [SimpleNamespace()]
            source_store_class.return_value.save_items.return_value = SimpleNamespace(
                received=1, inserted=1
            )
            ranking_store = ranking_store_class.return_value
            ranking_store.acquire_lock.return_value = True
            ranking_store.pending_items.return_value = pending
            ranking_store.save.return_value = SimpleNamespace(
                sources_marked=2, topic_ids=("topic-1", "topic-2")
            )
            rank_topics.return_value = SimpleNamespace(
                candidates=(SimpleNamespace(), SimpleNamespace()), ai_result=ai_result
            )
            content_store_class.return_value.archive_stale_topics.return_value = 0
            content_store_class.return_value.create_digest.return_value = None

            result = run_content_pipeline()

        self.assertEqual(result.sources_ranked, 2)
        self.assertEqual(result.topics_created, 2)
        self.assertEqual(result.estimated_cost_rub, Decimal("1.25"))
        self.assertTrue(result.budget_warning)
        ranking_store.save.assert_called_once()

    def test_rejects_unsafe_schedule_values_before_network(self):
        with self.assertRaisesRegex(ValueError, "retention_days"):
            run_content_pipeline(retention_days=0)
        with self.assertRaisesRegex(ValueError, "source_limit"):
            run_content_pipeline(source_limit=201)
        with self.assertRaisesRegex(ValueError, "max_topics"):
            run_content_pipeline(max_topics=11)


if __name__ == "__main__":
    unittest.main()
