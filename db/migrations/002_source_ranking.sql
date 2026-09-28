ALTER TABLE source_items
    ADD COLUMN ranked_at TIMESTAMPTZ;

CREATE INDEX source_items_unranked_idx
    ON source_items (published_at DESC NULLS LAST, fetched_at DESC)
    WHERE is_preprint = FALSE AND ranked_at IS NULL;
