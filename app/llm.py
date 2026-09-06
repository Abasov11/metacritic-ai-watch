"""Minimal OpenRouter client + the review-summary prompt.

Plain httpx against the chat-completions endpoint — an SDK buys nothing here.
Every call is billed, so every call is written to `llm_calls`.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from app import monitor
from app.config import settings
from app.db import SessionLocal
from app.models import LlmCall

log = logging.getLogger(__name__)

MAX_REVIEWS = 40
MAX_REVIEW_CHARS = 700
ATTEMPTS_PER_MODEL = 2

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class LLMError(RuntimeError):
    pass


def _extract_json(content: str) -> dict[str, Any]:
    """Parse the reply, tolerating ```json fences and prose around the object."""
    text = _FENCE_RE.sub("", content).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMError(f"no JSON object in reply: {content[:200]!r}")
    return json.loads(text[start : end + 1])


def _record(session_maker, **kwargs) -> None:
    """Bookkeeping must never break the caller."""
    try:
        with session_maker() as session:
            session.add(LlmCall(**kwargs))
            session.commit()
    except Exception:  # pragma: no cover - accounting is best-effort
        log.exception("could not record llm call")


def chat_json(
    prompt: str,
    schema_hint: str,
    *,
    purpose: str = "chat",
    game_id: int | None = None,
    models: list[str] | None = None,
) -> dict[str, Any]:
    """Ask for one JSON object. Tries the primary model, then the fallback.

    `models` overrides that pair — the evaluator uses it to grade with a model that
    never writes summaries here.
    """
    if not settings.openrouter_api_key:
        raise LLMError("OPENROUTER_API_KEY is not set")

    body = {
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты аналитик игровых отзывов. Отвечай только валидным JSON "
                    f"по схеме: {schema_hint}. Без пояснений и markdown."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "usage": {"include": True},  # makes OpenRouter report usage.cost
    }
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "HTTP-Referer": settings.site_url,
        "X-Title": settings.site_title,
    }

    last_error: Exception | None = None
    models = models or [settings.openrouter_model, settings.openrouter_fallback_model]
    for model in models:
        for attempt in range(1, ATTEMPTS_PER_MODEL + 1):
            started = time.monotonic()
            monitor.emit(
                type="llm_start",
                worker="llm",
                status="busy",
                detail=f"{purpose} · {model}",
                game_id=game_id,
                message=f"запрос к {model} ({purpose}, попытка {attempt})",
            )
            try:
                response = httpx.post(
                    settings.openrouter_url,
                    json=body | {"model": model},
                    headers=headers,
                    timeout=settings.llm_timeout,
                )
                response.raise_for_status()
                payload = response.json()
                if "error" in payload:
                    raise LLMError(str(payload["error"]))
                content = payload["choices"][0]["message"]["content"]
                result = _extract_json(content)
            except Exception as exc:
                last_error = exc
                log.warning("llm %s attempt %d failed: %s", model, attempt, exc)
                _record(
                    SessionLocal,
                    game_id=game_id,
                    purpose=purpose,
                    model=model,
                    ms=int((time.monotonic() - started) * 1000),
                    ok=False,
                    error=str(exc)[:1000],
                )
                monitor.emit(
                    type="llm_error",
                    worker="llm",
                    status="idle",
                    game_id=game_id,
                    model=model,
                    message=f"{model} ({purpose}) ошибка: {exc}",
                )
                continue

            usage = payload.get("usage") or {}
            _record(
                SessionLocal,
                game_id=game_id,
                purpose=purpose,
                model=payload.get("model") or model,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                cost=usage.get("cost"),
                ms=int((time.monotonic() - started) * 1000),
                ok=True,
            )
            monitor.emit(
                type="llm_call",
                worker="llm",
                status="idle",
                game_id=game_id,
                model=payload.get("model") or model,
                ms=int((time.monotonic() - started) * 1000),
                cost=usage.get("cost"),
                message=(
                    f"{payload.get('model') or model} ({purpose}): "
                    f"{int((time.monotonic() - started) * 1000)} мс, "
                    f"${usage.get('cost') or 0:.6f}, "
                    f"{usage.get('prompt_tokens')}/{usage.get('completion_tokens')} токенов"
                ),
            )
            result["_model"] = payload.get("model") or model
            return result

    raise LLMError(f"all models failed, last error: {last_error}")


# ---------------------------------------------------------------------------- tags

#: A closed vocabulary keeps tags comparable between games — free-form labels would
#: never overlap, which is exactly the problem tagging is here to solve. Anything the
#: model invents outside these lists is dropped.
TAG_VOCABULARY: dict[str, tuple[str, ...]] = {
    "genres": (
        "action",
        "adventure",
        "rpg",
        "shooter",
        "platformer",
        "metroidvania",
        "roguelike",
        "strategy",
        "tactics",
        "simulation",
        "management",
        "puzzle",
        "horror",
        "survival",
        "sandbox",
        "racing",
        "sports",
        "fighting",
        "rhythm",
        "visual-novel",
        "point-and-click",
        "card-game",
        "idle",
        "party",
        "mmo",
    ),
    "mechanics": (
        "combat",
        "parry",
        "stealth",
        "crafting",
        "building",
        "exploration",
        "puzzle-solving",
        "resource-management",
        "base-building",
        "deck-building",
        "turn-based",
        "real-time",
        "physics",
        "procedural-generation",
        "permadeath",
        "levelling",
        "loot",
        "dialogue-choices",
        "time-limit",
        "speedrunning",
        "economy",
        "farming",
        "driving",
        "shooting",
        "story-driven",
    ),
    "mood": (
        "dark",
        "cozy",
        "funny",
        "relaxing",
        "tense",
        "challenging",
        "atmospheric",
        "emotional",
        "chaotic",
        "serious",
        "cute",
        "surreal",
        "nostalgic",
        "brutal",
    ),
    "setting": (
        "fantasy",
        "sci-fi",
        "medieval",
        "modern",
        "historical",
        "post-apocalyptic",
        "cyberpunk",
        "space",
        "underwater",
        "urban",
        "rural",
        "school",
        "office",
        "war",
        "mythology",
        "animals",
        "abstract",
    ),
    "perspective": (
        "first-person",
        "third-person",
        "top-down",
        "isometric",
        "side-scrolling",
        "text",
    ),
}
MAX_TAGS_PER_FIELD = 6

TAGS_SCHEMA = (
    '{"genres": ["строка"], "mechanics": ["строка"], "mood": ["строка"], '
    '"setting": ["строка"], "perspective": "строка", "multiplayer": true}'
)


def clean_tags(raw: Any) -> dict[str, Any]:
    """Keep only vocabulary values; drop everything the model made up."""
    if not isinstance(raw, dict):
        return {}
    tags: dict[str, Any] = {}
    for field, allowed in TAG_VOCABULARY.items():
        if field == "perspective":
            continue
        values = raw.get(field)
        values = values if isinstance(values, list) else []
        kept: list[str] = []
        for value in values:
            if not isinstance(value, str):
                continue
            normalised = value.strip().lower().replace(" ", "-").replace("_", "-")
            if normalised in allowed and normalised not in kept:
                kept.append(normalised)
        tags[field] = kept[:MAX_TAGS_PER_FIELD]

    perspective = raw.get("perspective")
    if isinstance(perspective, str):
        normalised = perspective.strip().lower().replace(" ", "-").replace("_", "-")
        tags["perspective"] = normalised if normalised in TAG_VOCABULARY["perspective"] else None
    else:
        tags["perspective"] = None
    tags["multiplayer"] = bool(raw.get("multiplayer"))
    return tags


def build_tags(
    title: str,
    genres: list[str],
    description: str | None,
    reviews: list[str],
    game_id: int | None,
) -> dict[str, Any]:
    """One cheap call per game: a closed-vocabulary description of what it is."""
    allowed = "\n".join(
        f"- {field}: {', '.join(values)}" for field, values in TAG_VOCABULARY.items()
    )
    excerpt = "\n".join(f"- {r.strip()[:400]}" for r in reviews[:5])
    prompt = (
        f"Игра: {title}\n"
        f"Жанры с Metacritic: {', '.join(genres) or 'не указаны'}\n"
        f"Описание: {(description or 'нет').strip()[:1500]}\n"
        + (f"\nФрагменты отзывов:\n{excerpt}\n" if excerpt else "")
        + "\nОпиши игру тегами. Разрешены ТОЛЬКО значения из списков ниже, "
        "ничего не придумывай и не переводи:\n"
        f"{allowed}\n\n"
        "multiplayer — true, если в игре есть совместная или соревновательная игра "
        "с людьми, иначе false. perspective — одно значение или пустая строка, если "
        "непонятно. В каждом списке до 6 значений, только уверенные. Если про поле "
        "нечего сказать, верни пустой список."
    )
    data = chat_json(prompt, TAGS_SCHEMA, purpose="tags", game_id=game_id)
    return clean_tags(data)


SUMMARY_SCHEMA = '{"likes": ["строка"], "dislikes": ["строка"], "summary": "строка"}'


def summarize_reviews(
    title: str, kind: str, reviews: list[tuple[str | None, float | None, str]], game_id: int | None
) -> dict[str, Any]:
    """Short RU summary of what people like / dislike. `reviews` is (author, score, text)."""
    who = "критиков" if kind == "critic" else "игроков"
    lines = []
    for author, score, text in reviews[:MAX_REVIEWS]:
        head = f"[{author or 'аноним'}"
        if score is not None:
            head += f", оценка {score}"
        lines.append(f"{head}] {text.strip()[:MAX_REVIEW_CHARS]}")

    prompt = (
        f"Игра: {title}\n"
        f"Ниже {len(lines)} отзывов {who} с Metacritic.\n\n"
        + "\n\n".join(lines)
        + "\n\nСделай короткое резюме на русском языке: что нравится и что не нравится "
        f"в игре {who}. В likes и dislikes — по 3-5 конкретных пунктов, каждый одной "
        "фразой, опирайся только на текст отзывов. В summary — 2-3 предложения общего "
        "вывода. Если чего-то в отзывах нет, оставь список пустым."
    )

    data = chat_json(prompt, SUMMARY_SCHEMA, purpose=f"summary:{kind}", game_id=game_id)
    return {
        "likes": [str(x) for x in data.get("likes") or []],
        "dislikes": [str(x) for x in data.get("dislikes") or []],
        "summary": str(data.get("summary") or ""),
        "model": data.get("_model"),
    }
