from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_RULES_PATH = Path(os.environ.get("AI_GOVERNING_RULES_PATH", "config/ai_governing_phrases.json"))

DEFAULT_RULES: dict[str, Any] = {
    "version": 1,
    "business_abbreviations": {
        "hr": "HR",
        "it": "IT",
        "pr": "PR",
        "ceo": "CEO",
        "cfo": "CFO",
        "cto": "CTO",
    },
    "preserve_code_placeholder_patterns": [
        ".*Номер.*",
        ".*РегНомер.*",
        ".*Код.*",
        ".*Code.*",
        ".*Number.*",
    ],
    "governing_phrases": [
        {"id": "contract_number_after_no", "placeholder_name_pattern": ".*", "context_pattern": "(?:№|номер\\s+)\\s*\\[{placeholder}\\]", "behavior": "preserve"},
        {"id": "date_ot_goda", "placeholder_name_pattern": ".*(Дата|Date).*", "context_pattern": "(?:^|\\s)от\\s+\\[{placeholder}\\]\\s*(?:года|г\\.|год)(?:\\s|$|[.,;:])", "behavior": "date_ru_no_year_word"},
        {"id": "name_after_ot", "placeholder_name_pattern": ".*(ФИО|Сотрудник).*", "context_pattern": "(?:^|\\s)от\\s+\\[{placeholder}\\](?:\\s|$|[.,;:])", "case": "gent"},
        {"id": "name_after_zayavlenie", "placeholder_name_pattern": ".*(ФИО|Сотрудник).*", "context_pattern": "(?:^|\\s)заявлени[еяю]\\s+(?:от\\s+)?\\[{placeholder}\\](?:\\s|$|[.,;:])", "case": "gent"},
        {"id": "name_after_prinyat", "placeholder_name_pattern": ".*(ФИО|Сотрудник).*", "context_pattern": "(?:^|\\s)принять\\s+\\[{placeholder}\\](?:\\s|$|[.,;:])", "case": "accs"},
        {"id": "name_after_predostavit_otpusk", "placeholder_name_pattern": ".*(ФИО|Сотрудник).*", "context_pattern": "предоставить[\\s\\S]{0,180}\\[{placeholder}\\][\\s\\S]{0,180}отпуск", "case": "datv"},
    ],
    "department_name_rules": {
        "placeholder_name_patterns": [
            "Подразделение.*",
            ".*Подразделение.*",
            "Департамент.*",
            ".*Департамент.*",
            "Отдел.*",
            ".*Отдел.*",
            ".*ПодразделениеНаименование",
            ".*(Department|Division).*",
        ],
        "behavior": "fixed_form",
        "default_case": "nominative",
        "never_merge_with_adjacent_occurrence": True,
        "preserve_internal_abbreviations": True,
    },
}


@dataclass(frozen=True)
class RulesConfig:
    data: dict[str, Any]
    path: str
    mtime: float | None
    loaded: bool
    error: str = ""


_LOCK = threading.Lock()
_CACHE: RulesConfig | None = None


def _current_path() -> Path:
    return Path(os.environ.get("AI_GOVERNING_RULES_PATH", str(DEFAULT_RULES_PATH)))


def _merge_defaults(data: dict[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_RULES, ensure_ascii=False))
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def load_rules(force: bool = False) -> RulesConfig:
    global _CACHE
    path = _current_path()
    try:
        stat = path.stat()
        mtime = stat.st_mtime
    except FileNotFoundError:
        mtime = None

    with _LOCK:
        if not force and _CACHE is not None and _CACHE.path == str(path) and _CACHE.mtime == mtime:
            return _CACHE
        try:
            if mtime is None:
                cfg = RulesConfig(DEFAULT_RULES, str(path), None, False, "rules file not found; using defaults")
            else:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("rules file root must be a JSON object")
                cfg = RulesConfig(_merge_defaults(raw), str(path), mtime, True, "")
        except Exception as exc:
            cfg = RulesConfig(DEFAULT_RULES, str(path), mtime, False, str(exc))
        _CACHE = cfg
        return cfg


def rules_health() -> dict[str, Any]:
    cfg = load_rules()
    return {
        "ai_rules_loaded": cfg.loaded,
        "ai_rules_path": cfg.path,
        "ai_rules_mtime": cfg.mtime,
        "ai_rules_version": cfg.data.get("version"),
        "ai_rules_error": cfg.error,
    }


def _matches_any(patterns: list[str], value: str) -> bool:
    for pattern in patterns or []:
        try:
            if re.fullmatch(pattern, value, flags=re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def is_department_placeholder(key: str, cfg: RulesConfig | None = None) -> bool:
    cfg = cfg or load_rules()
    rules = cfg.data.get("department_name_rules") or {}
    return _matches_any(list(rules.get("placeholder_name_patterns") or []), key or "")


def department_rule(cfg: RulesConfig | None = None) -> dict[str, Any]:
    cfg = cfg or load_rules()
    return dict(cfg.data.get("department_name_rules") or {})


def business_abbreviations(cfg: RulesConfig | None = None) -> dict[str, str]:
    cfg = cfg or load_rules()
    raw = cfg.data.get("business_abbreviations") or {}
    return {str(k).lower(): str(v) for k, v in raw.items()}


def governing_phrases(cfg: RulesConfig | None = None) -> list[dict[str, Any]]:
    cfg = cfg or load_rules()
    raw = cfg.data.get("governing_phrases") or []
    return [item for item in raw if isinstance(item, dict)]
