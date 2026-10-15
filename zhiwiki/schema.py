"""SQLite schema, kept separate from lifecycle operations for readability."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    title TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    text TEXT NOT NULL,
    active INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_key, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_current_document
    ON documents(source_key) WHERE active = 1;
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id),
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    UNIQUE(document_id, ordinal)
);
CREATE INDEX IF NOT EXISTS chunks_by_document ON chunks(document_id);
CREATE TABLE IF NOT EXISTS wikis (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    stale INTEGER NOT NULL DEFAULT 0,
    claims_json TEXT NOT NULL,
    links_json TEXT NOT NULL,
    body TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(slug, version)
);
CREATE TABLE IF NOT EXISTS wiki_sources (
    wiki_id TEXT NOT NULL REFERENCES wikis(id),
    chunk_id TEXT NOT NULL REFERENCES chunks(id),
    PRIMARY KEY(wiki_id, chunk_id)
);
CREATE INDEX IF NOT EXISTS dependents_by_chunk ON wiki_sources(chunk_id);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retrieval_state (
    id INTEGER PRIMARY KEY CHECK(id=1),
    epoch INTEGER NOT NULL
);
INSERT OR IGNORE INTO retrieval_state(id, epoch) VALUES (1, 0);
"""

# Persistent triggers cover other KnowledgeStore instances and SQL writers too.
# Epoch increments participate in their source transaction (including rollback).
# Run history is deliberately absent: queries do not invalidate their own index.
for _table in ("documents", "chunks", "wikis", "wiki_sources"):
    for _operation in ("INSERT", "UPDATE", "DELETE"):
        SCHEMA += """
CREATE TRIGGER IF NOT EXISTS retrieval_{table}_{suffix}
AFTER {operation} ON {table}
BEGIN
    UPDATE retrieval_state SET epoch=epoch+1 WHERE id=1;
END;
""".format(table=_table, operation=_operation, suffix=_operation.lower())
