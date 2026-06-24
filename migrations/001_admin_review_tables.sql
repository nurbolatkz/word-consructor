CREATE TABLE IF NOT EXISTS review_items (
    id TEXT PRIMARY KEY,
    document_name TEXT NOT NULL DEFAULT '',
    log_key TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL,
    checker_result TEXT NOT NULL DEFAULT '{}',
    corrections TEXT NOT NULL DEFAULT '[]',
    rendered_preview TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    decided_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_items_log_key ON review_items(log_key);
CREATE INDEX IF NOT EXISTS idx_review_items_timestamp ON review_items(timestamp);
CREATE INDEX IF NOT EXISTS idx_review_items_status ON review_items(status);

CREATE TABLE IF NOT EXISTS rule_candidates (
    id TEXT PRIMARY KEY,
    candidate_type TEXT NOT NULL,
    pattern_summary TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    example_contexts TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    claude_recommendation TEXT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_at TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_rule_candidates_created_at ON rule_candidates(created_at);
CREATE INDEX IF NOT EXISTS idx_rule_candidates_status ON rule_candidates(status);

CREATE TABLE IF NOT EXISTS approved_rules_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    approved_at TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_snapshot TEXT NOT NULL,
    FOREIGN KEY(candidate_id) REFERENCES rule_candidates(id)
);
