"""Similar games: LLM tags first, wording second.

TF-IDF alone reads the marketing blurb, and on a catalogue of obscure indies almost no
two blurbs share a rare word, so every cosine sits near zero. The closed-vocabulary
tags (`app.llm.TAG_VOCABULARY`) give the two games something they *can* share, and the
cosine still separates games that happen to carry the same tags.

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

#: Tags dominate; wording only reorders games that already look alike.
TAG_WEIGHT = 0.6
TEXT_WEIGHT = 0.4
#: Below this a "similar" game is just the nearest of a bad lot, so we show nothing.
MIN_SCORE = 0.12

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


def tag_set(tags: dict | None) -> set[str]:
    """Flatten a tag object into comparable `field:value` strings."""
    if not isinstance(tags, dict):
        return set()
    flat: set[str] = set()
    for field in ("genres", "mechanics", "mood", "setting"):
        for value in tags.get(field) or []:
            if isinstance(value, str) and value:
                flat.add(f"{field}:{value}")
    if tags.get("perspective"):
        flat.add(f"perspective:{tags['perspective']}")
    if tags.get("multiplayer"):
        flat.add("multiplayer:yes")
    return flat


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def cosine(left: dict[str, float], right: dict[str, float]) -> float:
    # Iterate the shorter vector; both are unit length, so the dot product is cosine.
    small, large = (left, right) if len(left) < len(right) else (right, left)
    return sum(weight * large.get(term, 0.0) for term, weight in small.items())


def similar_games(
    game_id: int, k: int = 6, min_score: float = MIN_SCORE
) -> list[tuple[int, float, list[str]]]:
    """Up to `k` (game_id, score, shared tags) triples, most similar first.

    Returns nothing rather than the least-bad neighbours when nothing clears the bar.
    """
    ids, vectors = _vectors()
    if game_id not in ids:
        return []
    position = ids.index(game_id)
    target = vectors[position]

    with SessionLocal() as session:
        tags = {
            row.id: tag_set(row.tags)
            for row in session.scalars(select(Game).where(Game.id.in_(ids))).all()
        }
    target_tags = tags.get(game_id, set())

    scored: list[tuple[int, float, list[str]]] = []
    for other_id, vector in zip(ids, vectors, strict=True):
        if other_id == game_id:
            continue
        shared = target_tags & tags.get(other_id, set())
        score = TAG_WEIGHT * jaccard(target_tags, tags.get(other_id, set()))
        score += TEXT_WEIGHT * cosine(target, vector)
        if score >= min_score:
            scored.append((other_id, score, sorted(shared)))
    scored.sort(key=lambda triple: (-triple[1], triple[0]))
    return scored[:k]
