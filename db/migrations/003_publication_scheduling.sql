ALTER TABLE publications
    ADD COLUMN destination TEXT NOT NULL DEFAULT 'author'
        CHECK (destination IN ('author', 'channel')),
    ADD COLUMN locked_at TIMESTAMPTZ;

CREATE UNIQUE INDEX publications_one_active_per_material_idx
    ON publications (material_id)
    WHERE status IN ('scheduled', 'publishing', 'failed');
