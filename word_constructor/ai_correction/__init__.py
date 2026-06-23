"""Notebook-friendly UseAI correction API."""

from .engine import CorrectionEngine
from .extraction import find_occurrences, sanity_check, walk_document
from .morphology import DefaultMorphology, KazakhAwareMorphology
from .rules import GoverningPhraseRules, load_rules
from .testing import FakeOpenAIClient
from .types import CorrectionResult, Occurrence, TextUnit

__all__ = [
    "CorrectionEngine",
    "walk_document",
    "find_occurrences",
    "sanity_check",
    "load_rules",
    "GoverningPhraseRules",
    "DefaultMorphology",
    "KazakhAwareMorphology",
    "FakeOpenAIClient",
    "TextUnit",
    "Occurrence",
    "CorrectionResult",
]
