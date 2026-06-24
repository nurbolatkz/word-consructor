from __future__ import annotations

import sys

if __name__ == "__main__" and sys.path and sys.path[0].replace("\\", "/").endswith("/word_constructor/ai_correction"):
    sys.path.pop(0)

import hashlib
import json
import math
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STORE_PATH = os.environ.get("AI_RAG_STORE_PATH", "/tmp/kazuni_word_constructor/rag_store")

try:  # Chroma is optional; the JSONL backend keeps tests/offline installs working.
    import chromadb
    from chromadb.config import Settings
    from chromadb import EmbeddingFunction

    _CHROMADB_AVAILABLE = True
except Exception:  # pragma: no cover - depends on deployment extras
    chromadb = None
    Settings = None

    class EmbeddingFunction:  # type: ignore[no-redef]
        pass

    _CHROMADB_AVAILABLE = False


class SimpleHashEmbedding(EmbeddingFunction):
    """Small deterministic tokenizer/hash embedder for offline fallback use."""

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002 - Chroma API name
        embeddings = []
        for text in input:
            vec = [0.0] * 64
            for tok in (text or "").lower().split():
                h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
                vec[h % len(vec)] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            embeddings.append([v / norm for v in vec])
        return embeddings


@dataclass(frozen=True)
class RagEntry:
    id: str
    kind: str
    placeholder_role: str
    context_type: str
    governing_phrase: str
    original_value: str
    corrected_value: str
    case: str
    note: str
    source: str
    created_at: str


class RagStore:
    def __init__(self, path: str = STORE_PATH, embedding_function: Any | None = None, force_jsonl: bool = False):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.embedding_function = embedding_function or SimpleHashEmbedding()
        self._use_chroma = _CHROMADB_AVAILABLE and not force_jsonl
        self._jsonl_path = self.path / "correction_patterns.jsonl"

        if self._use_chroma:
            self.client = chromadb.PersistentClient(  # type: ignore[union-attr]
                path=str(self.path),
                settings=Settings(anonymized_telemetry=False),  # type: ignore[misc]
            )
            self.collection = self.client.get_or_create_collection(
                name="correction_patterns",
                embedding_function=self.embedding_function,
            )
        else:
            self.client = None
            self.collection = None

    def _build_document_text(self, entry: RagEntry | dict[str, Any]) -> str:
        data = asdict(entry) if isinstance(entry, RagEntry) else entry
        return (
            f"role={data.get('placeholder_role', '')} "
            f"context_type={data.get('context_type', '')} "
            f"governing_phrase={data.get('governing_phrase', '')} "
            f"original={data.get('original_value', '')} "
            f"corrected={data.get('corrected_value', '')} "
            f"case={data.get('case', '')} "
            f"note={data.get('note', '')}"
        )

    def add_entry(self, entry: RagEntry) -> None:
        data = asdict(entry)
        if self._use_chroma:
            self.collection.upsert(  # type: ignore[union-attr]
                ids=[entry.id],
                documents=[self._build_document_text(entry)],
                metadatas=[data],
            )
            return

        existing = self._read_jsonl_entries()
        by_id = {item["id"]: item for item in existing if item.get("id")}
        by_id[entry.id] = data
        self._write_jsonl_entries(list(by_id.values()))

    def query(
        self,
        placeholder_role: str,
        context_type: str,
        governing_phrase: str,
        original_value: str,
        n_results: int = 3,
        kind_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        query_text = (
            f"role={placeholder_role} context_type={context_type} "
            f"governing_phrase={governing_phrase} original={original_value}"
        )
        if self._use_chroma:
            where = {"kind": kind_filter} if kind_filter else None
            results = self.collection.query(  # type: ignore[union-attr]
                query_texts=[query_text],
                n_results=n_results,
                where=where,
            )
            if not results.get("metadatas") or not results["metadatas"][0]:
                return []
            return results["metadatas"][0]

        entries = self._read_jsonl_entries()
        if kind_filter:
            entries = [entry for entry in entries if entry.get("kind") == kind_filter]
        query_vec = self.embedding_function([query_text])[0]
        scored = []
        for entry in entries:
            doc_vec = self.embedding_function([self._build_document_text(entry)])[0]
            scored.append((sum(a * b for a, b in zip(query_vec, doc_vec)), entry))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [entry for _, entry in scored[:n_results]]

    def count(self) -> int:
        if self._use_chroma:
            return int(self.collection.count())  # type: ignore[union-attr]
        return len(self._read_jsonl_entries())

    def _read_jsonl_entries(self) -> list[dict[str, Any]]:
        if not self._jsonl_path.exists():
            return []
        entries = []
        for line in self._jsonl_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries

    def _write_jsonl_entries(self, entries: list[dict[str, Any]]) -> None:
        payload = "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries)
        self._jsonl_path.write_text(payload + ("\n" if payload else ""), encoding="utf-8")


def make_entry(
    kind: str,
    placeholder_role: str,
    context_type: str,
    governing_phrase: str,
    original_value: str,
    corrected_value: str,
    case: str,
    note: str,
    source: str,
    entry_id: str | None = None,
) -> RagEntry:
    return RagEntry(
        id=entry_id or str(uuid.uuid4()),
        kind=kind,
        placeholder_role=placeholder_role,
        context_type=context_type,
        governing_phrase=governing_phrase,
        original_value=original_value,
        corrected_value=corrected_value,
        case=case,
        note=note,
        source=source,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def seed_default_entries(store: RagStore) -> None:
    seed_entries = [
        make_entry(
            kind="known_pitfall",
            placeholder_role="person_name",
            context_type="label",
            governing_phrase="",
            original_value="Есжанова Зарина Серикалиевна",
            corrected_value="Есжанова З.С.",
            case="без_изменений",
            note="ОШИБКА: AI сократил ФИО до инициалов в контексте подписи.",
            source="deterministic_check",
            entry_id="pitfall_signature_fio_abbreviation",
        ),
        make_entry(
            kind="known_pitfall",
            placeholder_role="person_name",
            context_type="label",
            governing_phrase="",
            original_value="Есжанова Зарина Серикалиевна",
            corrected_value="Есжановой Зариной Серикалиевной",
            case="творительный",
            note="ОШИБКА: AI склонил ФИО в строке подписи без управляющего слова.",
            source="deterministic_check",
            entry_id="pitfall_signature_fio_declined",
        ),
        make_entry(
            kind="known_pitfall",
            placeholder_role="department",
            context_type="sentence",
            governing_phrase="должность",
            original_value="департамента кадровой политики / департамента кадровой политики",
            corrected_value="<duplicated in rendered text>",
            case="родительный",
            note="ОШИБКА: должность уже содержит департамент, а подразделение повторяет его.",
            source="deterministic_check",
            entry_id="pitfall_department_duplication",
        ),
        make_entry(
            kind="good_example",
            placeholder_role="position",
            context_type="sentence",
            governing_phrase="на должность",
            original_value="кассир-повар",
            corrected_value="кассира-повара",
            case="родительный",
            note="Корректное склонение должности после 'на должность'.",
            source="stage1_confirmed",
            entry_id="good_position_na_dolzhnost_cashier_cook",
        ),
        make_entry(
            kind="good_example",
            placeholder_role="person_name",
            context_type="sentence",
            governing_phrase="предоставить",
            original_value="Каниева Айгуль Казиевна",
            corrected_value="Каниевой Айгуль Казиевне",
            case="дательный",
            note="Корректный дательный падеж для конструкции '[Кому] ... предоставить'.",
            source="stage1_confirmed",
            entry_id="good_person_dative_predostavit",
        ),
    ]
    for entry in seed_entries:
        store.add_entry(entry)


if __name__ == "__main__":
    store = RagStore(force_jsonl=not _CHROMADB_AVAILABLE)
    seed_default_entries(store)
    print(f"Seeded examples. Total in store: {store.count()}")

    print("\n--- Query: bare label ФИО ---")
    for result in store.query("person_name", "label", "", "Курманбаев Талгат Серикович", n_results=2, kind_filter="known_pitfall"):
        print(f"  [{result['kind']}] {result['original_value']!r} -> {result['corrected_value']!r}")

    print("\n--- Query: position after 'на должность' ---")
    for result in store.query("position", "sentence", "на должность", "главный менеджер", n_results=2, kind_filter="good_example"):
        print(f"  [{result['kind']}] {result['original_value']!r} -> {result['corrected_value']!r}")
