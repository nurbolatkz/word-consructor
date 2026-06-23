from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GoverningPhraseRule:
    id: str
    pattern: str
    case: str


@dataclass(frozen=True)
class GoverningPhraseRules:
    version: int
    name_case_rules: list[GoverningPhraseRule]
    business_abbreviations: set[str]
    preserve_abbreviations: set[str]
    department_name_patterns: list[str]

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
    "preserve_abbreviations": ["ВОАД", "АУП", "КПП"],
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


def load_rules_config(force: bool = False) -> RulesConfig:
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



def _to_public_rules(data: dict[str, Any]) -> GoverningPhraseRules:
    name_rules: list[GoverningPhraseRule] = []
    for item in data.get("governing_phrases") or []:
        if not isinstance(item, dict):
            continue
        case = str(item.get("case") or item.get("behavior") or "")
        if case in {"gent", "datv", "accs", "nomn", "loct", "ablt"}:
            name_rules.append(GoverningPhraseRule(
                id=str(item.get("id") or ""),
                pattern=str(item.get("context_pattern") or item.get("pattern") or ""),
                case=case,
            ))
    dept = data.get("department_name_rules") or {}
    preserve = set(str(item) for item in data.get("preserve_abbreviations") or [])
    return GoverningPhraseRules(
        version=int(data.get("version") or 1),
        name_case_rules=name_rules,
        business_abbreviations=set(str(v) for v in (data.get("business_abbreviations") or {}).values()),
        preserve_abbreviations=preserve,
        department_name_patterns=[str(item) for item in dept.get("placeholder_name_patterns") or []],
    )


def load_rules(path: str | None = None) -> GoverningPhraseRules:
    """Load and validate config/ai_governing_phrases.json for notebook/engine use."""
    if path is None:
        return _to_public_rules(load_rules_config().data)
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("rules file root must be a JSON object")
    return _to_public_rules(_merge_defaults(raw))


def rules_health() -> dict[str, Any]:
    cfg = load_rules_config()
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
    cfg = cfg or load_rules_config()
    rules = cfg.data.get("department_name_rules") or {}
    return _matches_any(list(rules.get("placeholder_name_patterns") or []), key or "")


def department_rule(cfg: RulesConfig | None = None) -> dict[str, Any]:
    cfg = cfg or load_rules_config()
    return dict(cfg.data.get("department_name_rules") or {})


def business_abbreviations(cfg: RulesConfig | None = None) -> dict[str, str]:
    cfg = cfg or load_rules_config()
    raw = cfg.data.get("business_abbreviations") or {}
    return {str(k).lower(): str(v) for k, v in raw.items()}


def governing_phrases(cfg: RulesConfig | None = None) -> list[dict[str, Any]]:
    cfg = cfg or load_rules_config()
    raw = cfg.data.get("governing_phrases") or []
    return [item for item in raw if isinstance(item, dict)]
