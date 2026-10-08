-- requires-extension: vector
--
-- 0005_evidence_embeddings: pgvector embeddings for CANDIDATE RETRIEVAL ONLY
-- (§44: "pgvector for candidate finding only, never authoritative evidence").
--
-- Kept in its own migration because it needs the pgvector extension. The
-- runner checks pg_available_extensions first and stops with a clear message
-- when it is missing, instead of recording this migration as applied. The
-- pgvector/pgvector:pg16 image, Amazon RDS for PostgreSQL 16 and the CI
-- service container all ship it.
--
-- A nearest-neighbour hit is a lead to look at, not a fact: nothing read from
-- this table may become a field value, a match or a closure. Every candidate
-- is re-checked against the original evidence and its verified fields.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE evidence_embeddings (
    tenant_id       tenant_key    NOT NULL,
    id              bigint        GENERATED ALWAYS AS IDENTITY,
    evidence_id     bo_id         NOT NULL,
    chunk_index     integer       NOT NULL DEFAULT 0 CHECK (chunk_index >= 0),
    model           nonblank_text NOT NULL,   -- embedding model and version
    content_sha256  sha256_hex    NOT NULL,   -- hash of the exact text that was embedded
    -- 1024 dimensions fits common multilingual models (Portuguese, Spanish,
    -- English). A model with another size needs a new column and index.
    embedding       vector(1024)  NOT NULL,
    created_at      timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, evidence_id, model, chunk_index),
    FOREIGN KEY (tenant_id, evidence_id) REFERENCES evidence (tenant_id, id) ON DELETE CASCADE
);

COMMENT ON TABLE evidence_embeddings IS
    'Candidate retrieval only (§44). Never authoritative evidence: every hit is re-verified against the original.';
COMMENT ON COLUMN evidence_embeddings.embedding IS
    'Similarity is a hint for finding candidates, never proof of a value, a match or a closure.';

-- Row-level security filters by tenant after the approximate search; with many
-- tenants raise hnsw.ef_search (or use iterative scans on pgvector >= 0.8) so
-- a tenant still gets enough candidates.
CREATE INDEX evidence_embeddings_hnsw_idx ON evidence_embeddings
    USING hnsw (embedding vector_cosine_ops);

CALL enable_tenant_isolation('evidence_embeddings');

GRANT SELECT, INSERT, DELETE ON evidence_embeddings TO backoffice_app;
GRANT SELECT ON evidence_embeddings TO backoffice_readonly;
