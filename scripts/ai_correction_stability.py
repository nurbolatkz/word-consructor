from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from word_constructor.ai_correction.openai_client import SYSTEM_PROMPT, _build_schema


def _openai_client():
    from openai import OpenAI

    return OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def run_stability_test(
    template_text: str,
    placeholders: dict[str, Any],
    n_runs: int = 5,
    model: str | None = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> dict[str, dict[str, Any]]:
    placeholder_keys = [str(key) for key in placeholders.keys()]
    normalized_placeholders = {str(key): str(value) for key, value in placeholders.items()}
    user_content = {"template": template_text, "placeholders": normalized_placeholders}
    response_format = {
        "type": "json_schema",
        "json_schema": _build_schema(placeholder_keys),
    }

    client = _openai_client()
    results_per_key: dict[str, list[str]] = defaultdict(list)
    review_values: list[str] = []

    for _ in range(n_runs):
        response = client.chat.completions.create(
            model=model or os.environ.get("OPENAI_PLACEHOLDER_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini")),
            temperature=0,
            response_format=response_format,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_content, ensure_ascii=False, indent=2)},
            ],
        )
        try:
            corrected = json.loads(response.choices[0].message.content or "{}")
        except Exception as exc:  # pragma: no cover - diagnostic path
            corrected = {"_PARSE_ERROR": str(exc)}

        for key in placeholder_keys:
            results_per_key[key].append(str(corrected.get(key, "<MISSING>")))
        review_values.append(str(corrected.get("_review_needed", "<MISSING>")))

    report: dict[str, dict[str, Any]] = {}
    for key, values in results_per_key.items():
        counts = Counter(values)
        report[key] = {
            "distinct_values": len(counts),
            "stable": len(counts) == 1,
            "value_counts": dict(counts),
        }

    review_counts = Counter(review_values)
    report["_review_needed"] = {
        "distinct_values": len(review_counts),
        "stable": len(review_counts) == 1,
        "value_counts": dict(review_counts),
    }
    return report


def print_stability_report(report: dict[str, dict[str, Any]]) -> None:
    print(f"{'PLACEHOLDER':45s} {'STABLE':8s} {'VALUES SEEN'}")
    print("-" * 100)
    for key, info in report.items():
        flag = "OK" if info["stable"] else "UNSTABLE"
        print(f"{key:45s} {flag:8s}")
        for value, count in info["value_counts"].items():
            print(f"    [{count}x] {value!r}")
        print()


if __name__ == "__main__":
    fake_report = {
        "РеквизитыСотрудникДолжностьНаименование": {
            "distinct_values": 3,
            "stable": False,
            "value_counts": {
                "кассира-повара": 3,
                "кассиром-поваром": 1,
                "Кассир-повар": 1,
            },
        },
        "РеквизитыРуководительФИО": {
            "distinct_values": 3,
            "stable": False,
            "value_counts": {
                "Есжанова З.С.": 3,
                "Есжановой Зариной Серикалиевной": 3,
                "Есжанова Зарина Серикалиевна": 1,
            },
        },
        "СсылкаДатаПриема": {
            "distinct_values": 1,
            "stable": True,
            "value_counts": {"15 декабря 2025 года": 7},
        },
    }
    print_stability_report(fake_report)
    print("\nTo run a real stability test against OpenAI, import and call run_stability_test(...).")
