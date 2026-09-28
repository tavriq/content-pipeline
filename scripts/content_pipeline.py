#!/usr/bin/env python3
"""Ежедневное обновление источников, тем и авторского дайджеста."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from ai_gateway import AIGateway
from collect_sources import PubMedSource, read_config
from content_workflow import ContentStore, DigestPage
from database import DatabaseSettings, connect
from source_store import PUBMED_DEFINITION, SourceStore
from topic_ranking import TopicRankingStore, rank_topics


@dataclass(frozen=True)
class ContentPipelineResult:
    sources_received: int
    sources_inserted: int
    sources_ranked: int
    topics_created: int
    topics_archived: int
    digest: DigestPage | None
    estimated_cost_rub: Decimal
    budget_warning: bool
    ranking_skipped: bool = False


def run_content_pipeline(
    *,
    retention_days: int = 7,
    source_limit: int = 50,
    max_topics: int = 10,
) -> ContentPipelineResult:
    """Выполнить один идемпотентный проход контентного конвейера.

    PubMed можно опрашивать повторно: база отсекает дубли. AI вызывается только
    при наличии ещё не ранжированных публикаций. Старые темы архивируются, а не
    удаляются, поэтому материалы и история автора остаются целыми.
    """
    if retention_days <= 0:
        raise ValueError("retention_days должен быть больше нуля")
    if not 1 <= source_limit <= 200:
        raise ValueError("source_limit должен быть от 1 до 200")
    if not 1 <= max_topics <= 10:
        raise ValueError("max_topics должен быть от 1 до 10")

    config = read_config().get("pubmed", {})
    if not config.get("enabled", False):
        raise ValueError("PubMed отключён в config/sources.json")

    source = PubMedSource(
        config,
        email=os.environ.get("NCBI_EMAIL", ""),
        api_key=os.environ.get("NCBI_API_KEY"),
    )
    items = source.fetch()
    settings = DatabaseSettings.from_environment()
    with connect(settings) as connection:
        saved_sources = SourceStore(connection).save_items(items, PUBMED_DEFINITION)

    sources_ranked = 0
    topics_created = 0
    estimated_cost = Decimal("0")
    budget_warning = False
    ranking_skipped = False
    with connect(settings) as connection:
        ranking_store = TopicRankingStore(connection)
        if not ranking_store.acquire_lock():
            ranking_skipped = True
        else:
            pending = ranking_store.pending_items(source_limit)
            if pending:
                ranking = rank_topics(
                    AIGateway.from_environment(), pending, max_topics=max_topics
                )
                saved_ranking = ranking_store.save(
                    ranking.candidates,
                    considered_source_ids=(item.id for item in pending),
                )
                sources_ranked = saved_ranking.sources_marked
                topics_created = len(saved_ranking.topic_ids)
                estimated_cost = ranking.ai_result.estimated_cost_rub
                budget_warning = ranking.ai_result.budget_warning

    with connect(settings) as connection:
        content_store = ContentStore(connection)
        archived = content_store.archive_stale_topics(retention_days)
        digest = None if ranking_skipped else content_store.create_digest(max_topics)

    return ContentPipelineResult(
        sources_received=saved_sources.received,
        sources_inserted=saved_sources.inserted,
        sources_ranked=sources_ranked,
        topics_created=topics_created,
        topics_archived=archived,
        digest=digest,
        estimated_cost_rub=estimated_cost,
        budget_warning=budget_warning,
        ranking_skipped=ranking_skipped,
    )


def main() -> int:
    result = run_content_pipeline()
    print(
        f"Источники: получено {result.sources_received}, новых {result.sources_inserted}; "
        f"ранжировано {result.sources_ranked}; создано тем {result.topics_created}; "
        f"архивировано {result.topics_archived}; "
        f"дайджест {result.digest.digest_id if result.digest else 'без изменений'}; "
        f"расход {result.estimated_cost_rub:.4f} ₽"
    )
    if result.ranking_skipped:
        print("Ранжирование уже выполняется другим процессом")
    if result.budget_warning:
        from ai_budget import BudgetSettings

        warning = BudgetSettings.from_mapping(os.environ).warning_rub
        print(f"Внимание: расход AI за месяц превысил {warning:.0f} ₽")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
