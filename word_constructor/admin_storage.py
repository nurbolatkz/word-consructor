from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = "/tmp/kazuni_word_constructor/admin_review.sqlite3"
VALID_STATUSES = {"pending", "approved", "rejected", "logged"}


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def db_path() -> Path:
    return Path(os.environ.get("ADMIN_REVIEW_DB_PATH", DEFAULT_DB_PATH))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, factory=ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    owns_conn = conn is None
    if conn is None:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
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
            """
        )
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


def _json_dump(value: Any) -> str:
    return json.dumps(value if value is not None else None, ensure_ascii=False)


def _json_load(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return fallback


def _review_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "document_name": row["document_name"],
        "log_key": row["log_key"],
        "timestamp": row["timestamp"],
        "checker_result": _json_load(row["checker_result"], {}),
        "corrections": _json_load(row["corrections"], []),
        "rendered_preview": row["rendered_preview"],
        "status": row["status"],
        "decided_at": row["decided_at"],
    }


def _candidate_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "candidate_type": row["candidate_type"],
        "pattern_summary": row["pattern_summary"],
        "occurrence_count": int(row["occurrence_count"] or 0),
        "example_contexts": _json_load(row["example_contexts"], []),
        "created_at": row["created_at"],
        "claude_recommendation": _json_load(row["claude_recommendation"], None),
        "status": row["status"],
        "decided_at": row["decided_at"],
    }


def insert_review_item(item: dict[str, Any]) -> dict[str, Any]:
    row = dict(item)
    row.setdefault("id", str(uuid.uuid4()))
    row.setdefault("document_name", "")
    row.setdefault("log_key", "")
    row.setdefault("timestamp", utc_now_iso())
    row.setdefault("checker_result", {})
    row.setdefault("corrections", [])
    row.setdefault("rendered_preview", "")
    row.setdefault("status", "pending")
    row.setdefault("decided_at", None)
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO review_items
            (id, document_name, log_key, timestamp, checker_result, corrections, rendered_preview, status, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                str(row["document_name"]),
                str(row["log_key"]),
                str(row["timestamp"]),
                _json_dump(row["checker_result"]),
                _json_dump(row["corrections"]),
                str(row["rendered_preview"]),
                str(row["status"]),
                row["decided_at"],
            ),
        )
    return row


def load_review_items() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM review_items ORDER BY timestamp DESC, id DESC").fetchall()
    return [_review_row_to_dict(row) for row in rows]


def save_review_items(items: list[dict[str, Any]]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM review_items")
        for item in items:
            row = dict(item)
            row.setdefault("id", str(uuid.uuid4()))
            row.setdefault("timestamp", utc_now_iso())
            conn.execute(
                """
                INSERT INTO review_items
                (id, document_name, log_key, timestamp, checker_result, corrections, rendered_preview, status, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    str(row.get("document_name", "")),
                    str(row.get("log_key", "")),
                    str(row["timestamp"]),
                    _json_dump(row.get("checker_result", {})),
                    _json_dump(row.get("corrections", [])),
                    str(row.get("rendered_preview", "")),
                    str(row.get("status", "pending")),
                    row.get("decided_at"),
                ),
            )


def update_review_item_status(item_id: str, status: str) -> bool:
    if status not in VALID_STATUSES:
        raise ValueError("invalid status")
    with connect() as conn:
        cur = conn.execute(
            "UPDATE review_items SET status = ?, decided_at = ? WHERE id = ?",
            (status, utc_now_iso(), item_id),
        )
        return cur.rowcount > 0


def insert_rule_candidate(item: dict[str, Any]) -> dict[str, Any]:
    row = dict(item)
    row.setdefault("id", str(uuid.uuid4()))
    row.setdefault("candidate_type", "")
    row.setdefault("pattern_summary", "")
    row.setdefault("occurrence_count", 0)
    row.setdefault("example_contexts", [])
    row.setdefault("created_at", utc_now_iso())
    row.setdefault("claude_recommendation", None)
    row.setdefault("status", "pending")
    row.setdefault("decided_at", None)
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO rule_candidates
            (id, candidate_type, pattern_summary, occurrence_count, example_contexts, created_at,
             claude_recommendation, status, decided_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                str(row["candidate_type"]),
                str(row["pattern_summary"]),
                int(row["occurrence_count"] or 0),
                _json_dump(row["example_contexts"]),
                str(row["created_at"]),
                _json_dump(row["claude_recommendation"]) if row.get("claude_recommendation") is not None else None,
                str(row["status"]),
                row["decided_at"],
            ),
        )
    return row


def insert_rule_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [insert_rule_candidate(item) for item in items]


def load_rule_candidates() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM rule_candidates ORDER BY created_at DESC, id DESC").fetchall()
    return [_candidate_row_to_dict(row) for row in rows]


def save_rule_candidates(items: list[dict[str, Any]]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM rule_candidates")
        for item in items:
            row = dict(item)
            row.setdefault("id", str(uuid.uuid4()))
            row.setdefault("created_at", utc_now_iso())
            conn.execute(
                """
                INSERT INTO rule_candidates
                (id, candidate_type, pattern_summary, occurrence_count, example_contexts, created_at,
                 claude_recommendation, status, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    str(row.get("candidate_type", "")),
                    str(row.get("pattern_summary", "")),
                    int(row.get("occurrence_count", 0) or 0),
                    _json_dump(row.get("example_contexts", [])),
                    str(row["created_at"]),
                    _json_dump(row.get("claude_recommendation")) if row.get("claude_recommendation") is not None else None,
                    str(row.get("status", "pending")),
                    row.get("decided_at"),
                ),
            )


def get_rule_candidate(candidate_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM rule_candidates WHERE id = ?", (candidate_id,)).fetchone()
    return _candidate_row_to_dict(row) if row else None


def update_rule_candidate_status(candidate_id: str, status: str) -> bool:
    if status not in VALID_STATUSES:
        raise ValueError("invalid status")
    with connect() as conn:
        cur = conn.execute(
            "UPDATE rule_candidates SET status = ?, decided_at = ? WHERE id = ?",
            (status, utc_now_iso(), candidate_id),
        )
        return cur.rowcount > 0


def insert_approved_rule_log(candidate_id: str, candidate_snapshot: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO approved_rules_log (approved_at, candidate_id, candidate_snapshot)
            VALUES (?, ?, ?)
            """,
            (utc_now_iso(), candidate_id, _json_dump(candidate_snapshot)),
        )


def load_approved_rules_log() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM approved_rules_log ORDER BY approved_at DESC, id DESC").fetchall()
    return [
        {
            "id": row["id"],
            "approved_at": row["approved_at"],
            "candidate_id": row["candidate_id"],
            "candidate_snapshot": _json_load(row["candidate_snapshot"], {}),
        }
        for row in rows
    ]
