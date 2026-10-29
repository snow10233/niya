"""Embeddings live in SQLite as raw float32 bytes; search happens in numpy over an in-memory hot index.

Vectors are L2-normalised on the way in, so cosine similarity is a plain dot product. The hot index holds
only labile and active memories; archived ones are searched from the database in batches when niya
deliberately tries to remember.
"""

from collections.abc import Awaitable, Sequence
from typing import Protocol

import numpy as np

from niya.core.db import Database

ARCHIVE_BATCH = 5000


class Embedder(Protocol):
    model: str

    def embed(self, texts: Sequence[str]) -> Awaitable[list[list[float]]]: ...


def normalise(vec: Sequence[float] | np.ndarray) -> np.ndarray:
    v = np.asarray(vec, dtype="<f4")
    norm = float(np.linalg.norm(v))
    return v / norm if norm else v


def to_blob(vec: Sequence[float] | np.ndarray) -> bytes:
    return normalise(vec).tobytes()


def from_blob(blob: bytes, dim: int) -> np.ndarray:
    v = np.frombuffer(blob, dtype="<f4")
    if v.size != dim:
        raise ValueError(f"vector has {v.size} values, expected {dim}")
    return v


def store_vector(db: Database, memory_id: int, model: str, vec: Sequence[float] | np.ndarray) -> np.ndarray:
    v = normalise(vec)
    db.execute(
        "INSERT INTO memory_vectors(memory_id, model, dim, vec) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(memory_id, model) DO UPDATE SET dim = excluded.dim, vec = excluded.vec",
        (memory_id, model, v.size, v.tobytes()),
    )
    return v


class VectorIndex:
    """Brute-force cosine search; ~1–3 ms for 10k × 1024. Rows are dropped lazily and compacted on rebuild."""

    def __init__(self) -> None:
        self.ids: list[int] = []
        self._rows: np.ndarray | None = None
        self._size = 0
        self._dead: set[int] = set()
        self._pos: dict[int, int] = {}

    @classmethod
    def load(cls, db: Database, model: str) -> "VectorIndex":
        index = cls()
        rows = db.query(
            "SELECT v.memory_id, v.dim, v.vec FROM memory_vectors v JOIN memories m ON m.id = v.memory_id"
            " WHERE m.state IN ('labile', 'active') AND v.model = ? ORDER BY v.memory_id",
            (model,),
        )
        for row in rows:
            index.add(row["memory_id"], from_blob(row["vec"], row["dim"]))
        return index

    def __len__(self) -> int:
        return self._size - len(self._dead)

    def add(self, memory_id: int, vec: np.ndarray) -> None:
        if memory_id in self._pos:
            self.remove(memory_id)
        if self._rows is None:
            self._rows = np.zeros((16, vec.size), dtype="<f4")
        elif vec.size != self._rows.shape[1]:
            raise ValueError("all vectors in one index must have the same dimension")
        if self._size == len(self._rows):
            grown = np.zeros((len(self._rows) * 2, vec.size), dtype="<f4")
            grown[: self._size] = self._rows[: self._size]
            self._rows = grown
        self._rows[self._size] = vec
        self._pos[memory_id] = self._size
        self.ids.append(memory_id)
        self._size += 1

    def remove(self, memory_id: int) -> None:
        if memory_id in self._pos:
            self._dead.add(self._pos.pop(memory_id))

    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        if self._rows is None or not len(self):
            return []
        scores = self._rows[: self._size] @ query
        if self._dead:
            scores[list(self._dead)] = -2.0
        k = min(k, len(self))
        top = np.argpartition(-scores, k - 1)[:k]
        return [(self.ids[i], float(scores[i])) for i in sorted(top, key=lambda i: -scores[i])]


def search_archived(db: Database, model: str, query: np.ndarray, k: int) -> list[tuple[int, float]]:
    """Deliberate recall reaches archived memories too: scan them from the database in batches."""
    best: list[tuple[int, float]] = []
    last = 0
    while True:
        rows = db.query(
            "SELECT v.memory_id, v.dim, v.vec FROM memory_vectors v JOIN memories m ON m.id = v.memory_id"
            " WHERE m.state = 'archived' AND v.model = ? AND v.memory_id > ? ORDER BY v.memory_id LIMIT ?",
            (model, last, ARCHIVE_BATCH),
        )
        if not rows:
            break
        matrix = np.stack([from_blob(r["vec"], r["dim"]) for r in rows])
        scores = matrix @ query
        best.extend((rows[i]["memory_id"], float(scores[i])) for i in range(len(rows)))
        best = sorted(best, key=lambda item: -item[1])[:k]
        last = rows[-1]["memory_id"]
    return best
