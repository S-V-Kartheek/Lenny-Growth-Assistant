-- Semantic retrieval support.
--
-- Kept in a separate migration because pgvector is an optional capability: the
-- system runs in lexical-only mode when the extension or an embedding model is
-- unavailable, and this migration is allowed to fail softly in that case.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding vector(768);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_model TEXT;

-- HNSW gives good recall without the "train after load" step ivfflat needs,
-- which matters because ingestion and querying can interleave.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON chunks USING hnsw (embedding vector_cosine_ops);
