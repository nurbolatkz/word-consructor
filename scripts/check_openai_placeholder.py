from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def load_dotenv_for_local_diagnostic(path: Path) -> None:
    """Mirror Docker Compose .env interpolation for local diagnostics only."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def main() -> int:
    load_dotenv_for_local_diagnostic(Path(".env"))

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("OPENAI_API_KEY is not set")
        return 2

    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_PLACEHOLDER_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    timeout = max(float(os.environ.get("OPENAI_PLACEHOLDER_TIMEOUT_SECONDS", "8")), 1.0)
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Correct grammar in the user's Russian sentence. Return only the corrected sentence.",
            },
            {"role": "user", "content": "Я пошел в магазин и купила хлеб."},
        ],
        "temperature": 0,
    }
    request = Request(
        f"{base_url}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    print(f"Using model={model} base_url={base_url} timeout={timeout}")
    print(f"OPENAI_API_KEY present: yes, length={len(api_key)}")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            print(f"HTTP status: {getattr(response, 'status', None)}")
            print(raw.decode("utf-8", errors="replace"))
            return 0
    except HTTPError as exc:
        raw = exc.read()
        print(f"HTTP status: {exc.code}")
        print(raw.decode("utf-8", errors="replace"))
        return 1
    except URLError as exc:
        print(f"URL error: {exc!r}")
        traceback.print_exc()
        return 1
    except Exception as exc:
        print(f"Error: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
