"""Thread-safe rotating JSONL log store capped at MAX_ENTRIES."""
from __future__ import annotations

import json
import os
import pathlib
import threading
from typing import Any

MAX_ENTRIES = 50
_lock = threading.Lock()


def _log_path() -> pathlib.Path:
    env_dir = os.environ.get("AI_CORRECTION_LOG_DIR", "").strip()
    base = pathlib.Path(env_dir) if env_dir else pathlib.Path(__file__).parents[2] / "tmp_logs"
    base.mkdir(parents=True, exist_ok=True)
    return base / "corrections.jsonl"


def append(entry: dict[str, Any]) -> None:
    """Append one log entry and rotate to keep at most MAX_ENTRIES."""
    path = _log_path()
    with _lock:
        existing = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []
        existing.append(json.dumps(entry, ensure_ascii=False, default=str))
        if len(existing) > MAX_ENTRIES:
            existing = existing[-MAX_ENTRIES:]
        path.write_text("\n".join(existing) + "\n", encoding="utf-8")


def read_all() -> list[dict[str, Any]]:
    """Return all stored log entries as dicts, oldest first."""
    path = _log_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
