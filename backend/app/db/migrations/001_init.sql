-- Lenny Growth Assistant :: core schema
-- Applied idempotently at startup by app.db.migrate.
--
-- Design notes
--  * Knowledge (episodes, chunks) and conversation (sessions, messages) are
--    separate concerns that meet only through message_sources, which records
--    exactly which transcript chunk backed which answer. That join table is
--    what makes grounding auditable after the fact.
--  * Chunks keep start/end second offsets so a citation can deep-link into the
--    YouTube episode at the moment the claim was made.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ---------------------------------------------------------------- knowledge --

CREATE TABLE IF NOT EXISTS episodes (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug             TEXT NOT NULL UNIQUE,
    guest            TEXT,
    title            TEXT NOT NULL,
    youtube_url      TEXT,
    video_id         TEXT,
    publish_date     DATE,
    duration_seconds INTEGER,
    view_count       BIGINT,
    keywords         TEXT[] NOT NULL DEFAULT '{}',
    description      TEXT,
    -- Provenance: which upstream revision this text came from.
    source_repo      TEXT NOT NULL,
    source_commit    TEXT NOT NULL,
    source_path      TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    word_count       INTEGER NOT NULL DEFAULT 0,
    ingested_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_episodes_publish_date ON episodes (publish_date DESC);

CREATE TABLE IF NOT EXISTS chunks (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    episode_id     UUID NOT NULL REFERENCES episodes (id) ON DELETE CASCADE,
    chunk_index    INTEGER NOT NULL,
    content        TEXT NOT NULL,
    speakers       TEXT[] NOT NULL DEFAULT '{}',
    start_seconds  INTEGER,
    end_seconds    INTEGER,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    -- Lexical index. Generated column so it can never drift from content.
    tsv            TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (episode_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_chunks_episode ON chunks (episode_id);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_commit     TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'running',
    episodes_ingested INTEGER NOT NULL DEFAULT 0,
    episodes_skipped  INTEGER NOT NULL DEFAULT 0,
    chunks_created    INTEGER NOT NULL DEFAULT 0,
    chunks_embedded   INTEGER NOT NULL DEFAULT 0,
    embedding_model   TEXT,
    error             TEXT,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    CONSTRAINT ingestion_runs_status_chk
        CHECK (status IN ('running', 'succeeded', 'failed'))
);

-- ------------------------------------------------------------- conversation --

CREATE TABLE IF NOT EXISTS users (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id  TEXT NOT NULL UNIQUE,
    display_name TEXT,
    metadata     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title      TEXT NOT NULL DEFAULT 'New chat',
    metadata   JSONB NOT NULL DEFAULT '{}'::jsonb,
    archived   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_updated
    ON sessions (user_id, updated_at DESC) WHERE archived = FALSE;

CREATE TABLE IF NOT EXISTS messages (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    UUID NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL,
    -- Which skill handled this turn, and on what model. Recorded per message so
    -- routing behaviour is inspectable in production, not just in logs.
    intent        TEXT,
    provider      TEXT,
    model         TEXT,
    latency_ms    INTEGER,
    token_usage   JSONB,
    grounding     JSONB,
    error         JSONB,
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT messages_role_chk CHECK (role IN ('user', 'assistant', 'system'))
);

CREATE INDEX IF NOT EXISTS idx_messages_session_created
    ON messages (session_id, created_at);

CREATE TABLE IF NOT EXISTS message_sources (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id       UUID NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    chunk_id         UUID REFERENCES chunks (id) ON DELETE SET NULL,
    rank             INTEGER NOT NULL,
    score            DOUBLE PRECISION NOT NULL DEFAULT 0,
    retrieval_method TEXT NOT NULL DEFAULT 'hybrid',
    cited            BOOLEAN NOT NULL DEFAULT FALSE,
    snapshot         JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (message_id, rank)
);

CREATE INDEX IF NOT EXISTS idx_message_sources_message ON message_sources (message_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    UUID NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    message_id    UUID REFERENCES messages (id) ON DELETE SET NULL,
    kind          TEXT NOT NULL,
    title         TEXT NOT NULL,
    -- Raw model output is retained for debugging; `content` is the sanitized
    -- form and is the ONLY field the viewer is ever given.
    content       TEXT NOT NULL,
    raw_content   TEXT,
    version       INTEGER NOT NULL DEFAULT 1,
    sanitization  JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT artifacts_kind_chk CHECK (kind IN ('markdown', 'html'))
);

CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts (session_id, created_at DESC);

CREATE OR REPLACE FUNCTION touch_session_updated_at() RETURNS TRIGGER AS $$
BEGIN
    UPDATE sessions SET updated_at = now() WHERE id = NEW.session_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_messages_touch_session ON messages;
CREATE TRIGGER trg_messages_touch_session
    AFTER INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION touch_session_updated_at();
