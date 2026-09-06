"""Similar games by TF-IDF cosine over the game's own text. No LLM, no extra deps.

A few hundred games make a matrix small enough to keep in memory and rebuild whenever
the row count changes.
"""

from __future__ import annotations

import math
import re
from collections import Counter

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Game

_WORD_RE = re.compile(r"[a-zа-яё0-9]{3,}", re.IGNORECASE)

# Words that appear in half the marketing blurbs and carry no signal.
_STOPWORDS = frozenset(
    """the and for with you your this that from are was has have not but all can its
    new game games play player players world can will one out more into their они
    очень игра игры""".split()
)

_cache: tuple[int, list[int], list[dict[str, float]]] | None = None


def _document(game: Game) -> str:
    parts = [
        game.title or "",
        game.title or "",  # the title matters more than the blurb — count it twice
        " ".join(game.genres or []),
        " ".join(game.genres or []),
        game.developer or "",
        game.publisher or "",
        " ".join(p.name for p in game.platforms),
        game.description or "",
    ]
    return " ".join(parts).lower()


def _tokenize(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text) if w not in _STOPWORDS]


def _build() -> tuple[int, list[int], list[dict[str, float]]]:
    with SessionLocal() as session:
        games = session.scalars(select(Game).order_by(Game.id)).all()
        docs = [Counter(_tokenize(_document(g))) for g in games]
        ids = [g.id for g in games]

    n = len(docs)
    df = Counter(term for doc in docs for term in doc)
    vectors: list[dict[str, float]] = []
    for doc in docs:
        total = sum(doc.values()) or 1
        vector = {
            term: (count / total) * math.log((n + 1) / (df[term] + 1))
            for term, count in doc.items()
        }
        norm = math.sqrt(sum(v * v for v in vector.values())) or 1.0
        vectors.append({t: v / norm for t, v in vector.items()})
    return n, ids, vectors


def _vectors() -> tuple[list[int], list[dict[str, float]]]:
    global _cache
    with SessionLocal() as session:
        count = session.scalar(select(func.count(Game.id))) or 0
    if _cache is None or _cache[0] != count:
        _cache = _build()
    return _cache[1], _cache[2]


def invalidate() -> None:
    """Force a rebuild — call after a crawl edits existing rows."""
    global _cache
    _cache = None


def similar_games(game_id: int, k: int = 6) -> list[tuple[int, float]]:
    """Up to `k` (game_id, score) pairs, most similar first."""
    ids, vectors = _vectors()
    if game_id not in ids:
        return []
    target = vectors[ids.index(game_id)]

    scored = []
    for other_id, vector in zip(ids, vectors, strict=True):
        if other_id == game_id:
            continue
        # Iterate the shorter vector; both are unit length, so the dot product is cosine.
        small, large = (target, vector) if len(target) < len(vector) else (vector, target)
        score = sum(weight * large.get(term, 0.0) for term, weight in small.items())
        if score > 0:
            scored.append((other_id, score))
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[:k]
