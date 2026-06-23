from __future__ import annotations

from .types import Occurrence


class FakeOpenAIClient:
    """Test double that echoes input unchanged unless responses override entries."""

    def __init__(self, responses: dict[tuple[str, int], str] | None = None):
        self.responses = responses or {}

    def correct(self, occurrences: list[Occurrence], promt_ai: str) -> dict[tuple[str, int], str]:
        result: dict[tuple[str, int], str] = {}
        for occurrence in occurrences:
            key = (occurrence.placeholder, occurrence.occurrence_index)
            result[key] = self.responses.get(key, occurrence.original_value)
        return result
