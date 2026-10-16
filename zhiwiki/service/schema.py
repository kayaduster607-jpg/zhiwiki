"""Additive team schema: original documents/chunks remain the content source."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS team_schema(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO team_schema VALUES(1);
CREATE TABLE IF NOT EXISTS team_users(
 id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
 admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS team_tokens(
 hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES team_users(id), kind TEXT NOT NULL,
 scopes TEXT NOT NULL, kb_ids TEXT NOT NULL, expires REAL NOT NULL, csrf_hash TEXT,
 revoked INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS team_tokens_user ON team_tokens(user_id);
CREATE TABLE IF NOT EXISTS team_kbs(
 id TEXT PRIMARY KEY, title TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
 artifact TEXT, acl_epoch INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS team_grants(
 user_id TEXT NOT NULL REFERENCES team_users(id), kb_id TEXT NOT NULL REFERENCES team_kbs(id),
 role TEXT NOT NULL, PRIMARY KEY(user_id,kb_id));
CREATE TABLE IF NOT EXISTS team_books(
 id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES team_kbs(id), source_key TEXT NOT NULL,
 title TEXT NOT NULL, current_revision TEXT, UNIQUE(kb_id,source_key));
CREATE TABLE IF NOT EXISTS team_revisions(
 id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES team_books(id), number INTEGER NOT NULL,
 status TEXT NOT NULL, expected_revision TEXT, file_hash TEXT NOT NULL, filename TEXT NOT NULL,
 pipeline TEXT NOT NULL, quality TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL,
 reviewed INTEGER NOT NULL DEFAULT 0, UNIQUE(book_id,number));
CREATE TABLE IF NOT EXISTS team_pages(
 revision_id TEXT NOT NULL REFERENCES team_revisions(id), page INTEGER NOT NULL,
 document_id TEXT NOT NULL REFERENCES documents(id), raw_text TEXT NOT NULL,
 quality TEXT NOT NULL, PRIMARY KEY(revision_id,page), UNIQUE(document_id));
CREATE TABLE IF NOT EXISTS team_jobs(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES team_users(id), kb_id TEXT NOT NULL,
 revision_id TEXT NOT NULL REFERENCES team_revisions(id), status TEXT NOT NULL,
 pages_total INTEGER, pages_done INTEGER NOT NULL DEFAULT 0, attempt INTEGER NOT NULL DEFAULT 0,
 lease_until REAL NOT NULL DEFAULT 0, fence INTEGER NOT NULL DEFAULT 0,
 cancel INTEGER NOT NULL DEFAULT 0, error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS team_receipts(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, token_hash TEXT NOT NULL, kb_ids TEXT NOT NULL,
 snapshots TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS team_idempotency(
 owner TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL, request_hash TEXT NOT NULL,
 result_id TEXT NOT NULL, status TEXT NOT NULL, expires REAL NOT NULL,
 PRIMARY KEY(owner,operation,key));
CREATE TABLE IF NOT EXISTS team_wikis(
 wiki_id TEXT PRIMARY KEY REFERENCES wikis(id), kb_id TEXT NOT NULL, owner TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS team_audit(
 id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
 resource TEXT NOT NULL, metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS team_metadata(
 book_id TEXT PRIMARY KEY REFERENCES team_books(id), payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS team_runtime(name TEXT PRIMARY KEY,payload TEXT NOT NULL);
"""
