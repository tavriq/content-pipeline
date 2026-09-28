CREATE TABLE sources (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    language TEXT NOT NULL CHECK (language IN ('ru', 'en', 'multi')),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('rss', 'api', 'website')),
    primary_source BOOLEAN NOT NULL DEFAULT FALSE,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name, base_url)
);

CREATE TABLE source_items (
    id BIGSERIAL PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE RESTRICT,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL CHECK (language IN ('ru', 'en')),
    published_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash TEXT NOT NULL,
    is_preprint BOOLEAN NOT NULL DEFAULT FALSE,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source_id, external_id),
    UNIQUE (content_hash)
);

CREATE INDEX source_items_recent_idx
    ON source_items (published_at DESC NULLS LAST, fetched_at DESC);
CREATE INDEX source_items_eligible_idx
    ON source_items (is_preprint, published_at DESC NULLS LAST);

CREATE TABLE topics (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    rationale TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'ru' CHECK (language = 'ru'),
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'shown', 'selected', 'rejected', 'archived')),
    score NUMERIC(8, 4) NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    selected_at TIMESTAMPTZ,
    rejected_at TIMESTAMPTZ,
    rejection_reason TEXT
);

CREATE TABLE topic_sources (
    topic_id TEXT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    source_item_id BIGINT NOT NULL REFERENCES source_items(id) ON DELETE RESTRICT,
    evidence_role TEXT NOT NULL DEFAULT 'supporting'
        CHECK (evidence_role IN ('primary', 'supporting', 'context')),
    PRIMARY KEY (topic_id, source_item_id)
);

CREATE TABLE digest_batches (
    id TEXT PRIMARY KEY,
    scheduled_for TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'building'
        CHECK (status IN ('building', 'ready', 'sent', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at TIMESTAMPTZ,
    error TEXT
);

CREATE TABLE digest_topics (
    digest_id TEXT NOT NULL REFERENCES digest_batches(id) ON DELETE CASCADE,
    topic_id TEXT NOT NULL REFERENCES topics(id) ON DELETE RESTRICT,
    page INTEGER NOT NULL DEFAULT 1 CHECK (page > 0),
    rank INTEGER NOT NULL CHECK (rank > 0),
    shown_at TIMESTAMPTZ,
    PRIMARY KEY (digest_id, topic_id),
    UNIQUE (digest_id, page, rank)
);

CREATE TABLE materials (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL REFERENCES topics(id) ON DELETE RESTRICT,
    format TEXT NOT NULL CHECK (format IN ('telegram', 'reels', 'tiktok')),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'review', 'approved', 'scheduled', 'published', 'cancelled')),
    current_version INTEGER NOT NULL DEFAULT 0 CHECK (current_version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at TIMESTAMPTZ,
    UNIQUE (topic_id, format)
);

CREATE TABLE material_versions (
    id BIGSERIAL PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    body TEXT NOT NULL,
    edit_instruction TEXT,
    edit_mode TEXT NOT NULL DEFAULT 'generated'
        CHECK (edit_mode IN ('generated', 'text', 'voice', 'rollback')),
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (material_id, version)
);

CREATE TABLE publications (
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id) ON DELETE RESTRICT,
    platform TEXT NOT NULL CHECK (platform = 'telegram'),
    scheduled_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled', 'publishing', 'published', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error TEXT,
    external_message_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);

CREATE INDEX publications_due_idx
    ON publications (scheduled_at)
    WHERE status IN ('scheduled', 'failed');

CREATE TABLE ai_usage (
    id BIGSERIAL PRIMARY KEY,
    operation TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    estimated_cost_rub NUMERIC(12, 4),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ai_usage_created_idx ON ai_usage (created_at DESC);
