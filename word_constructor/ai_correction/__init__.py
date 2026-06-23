"""Notebook-friendly UseAI correction API."""

from . import log_store
from .engine import CorrectionEngine
from .extraction import document_full_text, find_occurrences, sanity_check, walk_document
from .morphology import DefaultMorphology, KazakhAwareMorphology
from .rules import GoverningPhraseRules, load_rules
from .testing import FakeOpenAIClient
from .types import CorrectionResult, Occurrence, TextUnit

__all__ = [
    "log_store",
    "CorrectionEngine",
    "document_full_text",
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
