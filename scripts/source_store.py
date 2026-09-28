#!/usr/bin/env python3
"""Сохранение собранных публикаций в PostgreSQL без дублей."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from collect_sources import SourceItem


@dataclass(frozen=True)
class SourceDefinition:
    name: str
    base_url: str
    language: str
    source_kind: str
    primary_source: bool
    config: dict[str, Any]


PUBMED_DEFINITION = SourceDefinition(
    name="PubMed",
    base_url="https://pubmed.ncbi.nlm.nih.gov/",
    language="en",
    source_kind="api",
    primary_source=True,
    config={"provider": "NCBI E-utilities"},
)


@dataclass(frozen=True)
class SaveResult:
    received: int
    inserted: int
    duplicates: int
    preprints_excluded: int


class SourceStore:
    def __init__(self, connection: Any):
        self.connection = connection

    def save_items(
        self,
        items: Iterable[SourceItem],
        definition: SourceDefinition,
    ) -> SaveResult:
        batch = list(items)
        inserted = 0
        preprints_excluded = 0
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO sources (
                        name, base_url, language, source_kind, primary_source, config
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (name, base_url) DO UPDATE SET
                        language = EXCLUDED.language,
                        source_kind = EXCLUDED.source_kind,
                        primary_source = EXCLUDED.primary_source,
                        config = sources.config || EXCLUDED.config,
                        enabled = TRUE
                    RETURNING id
                    """,
                    (
                        definition.name,
                        definition.base_url,
                        definition.language,
                        definition.source_kind,
                        definition.primary_source,
                        json.dumps(definition.config, ensure_ascii=False),
                    ),
                )
                source_id = int(cursor.fetchone()[0])

                for item in batch:
                    if item.source != definition.name:
                        raise ValueError(
                            f"Элемент {item.external_id} относится к {item.source}, "
                            f"а не к {definition.name}"
                        )
                    if item.is_preprint:
                        preprints_excluded += 1
                        continue
                    cursor.execute(
                        """
                        INSERT INTO source_items (
                            source_id, external_id, url, title, summary, language,
                            published_at, content_hash, is_preprint, raw_payload
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s::jsonb)
                        ON CONFLICT DO NOTHING
                        RETURNING id
                        """,
                        (
                            source_id,
                            item.external_id,
                            item.url,
                            item.title,
                            item.summary,
                            item.language,
                            item.published_at,
                            item.content_hash,
                            json.dumps(item.raw_payload, ensure_ascii=False),
                        ),
                    )
                    if cursor.fetchone() is not None:
                        inserted += 1
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

        eligible = len(batch) - preprints_excluded
        return SaveResult(
            received=len(batch),
            inserted=inserted,
            duplicates=eligible - inserted,
            preprints_excluded=preprints_excluded,
        )
