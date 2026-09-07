"""In-process event bus for the live monitor page.

The crawler runs in a background thread, the SSE endpoint in the event loop. Instead of
plumbing an asyncio queue across that boundary, every event gets a sequence number and
readers poll `since(seq)` — a deque scan under one lock, no cross-thread wakeups.

# ponytail: 0.5s polling ceiling, fine for one dashboard on one process. Swap for an
# asyncio.Queue fanout if this ever needs sub-100ms updates or many concurrent readers.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

from app.config import settings

MAX_EVENTS = 200

#: Workers shown on the dashboard. `youtube` reports `off` only while the let's-play
#: stage is switched off; otherwise it idles like the rest.
WORKERS = ("crawler", "llm", "youtube")

_lock = threading.Lock()
_events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
_seq = 0
_workers: dict[str, dict[str, Any]] = {}
_current_run: dict[str, Any] | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def reset() -> None:
    """Clear the bus. Used at startup and by tests."""
    global _seq, _current_run
    with _lock:
        _events.clear()
        _seq = 0
        _current_run = None
        _workers.clear()
        for name in WORKERS:
            off = name == "youtube" and not settings.youtube_enabled
            _workers[name] = {
                "status": "off" if off else "idle",
                "detail": "выключено настройкой" if off else None,
                "since": _now(),
            }


reset()


def emit(**event: Any) -> dict[str, Any]:
    """Record an event and fold it into the current state. Never raises."""
    global _seq, _current_run
    with _lock:
        _seq += 1
        event = {"seq": _seq, "at": _now(), **event}
        _events.append(event)

        worker = event.get("worker")
        if worker in _workers:
            _workers[worker] = {
                "status": event.get("status", "idle"),
                "detail": event.get("detail") or event.get("message"),
                "since": event["at"],
            }

        kind = event.get("type")
        if kind == "run_start":
            _current_run = {
                "id": event.get("run_id"),
                "source": event.get("source"),
                "reason": event.get("reason"),
                "started_at": event["at"],
                "planned": event.get("planned", 0),
                "processed": 0,
                "failed": 0,
            }
        elif _current_run is not None:
            if kind == "run_planned":
                _current_run["planned"] = event.get("planned", 0)
                _current_run["source"] = event.get("source") or _current_run["source"]
            elif kind == "game_done":
                _current_run["processed"] += 1
            elif kind == "game_failed":
                _current_run["failed"] += 1
            elif kind == "run_end":
                _current_run = None
        return event


def since(seq: int) -> list[dict[str, Any]]:
    with _lock:
        return [e for e in _events if e["seq"] > seq]


def last_seq() -> int:
    with _lock:
        return _seq


def events(limit: int = 50) -> list[dict[str, Any]]:
    """Most recent events, newest last."""
    with _lock:
        return list(_events)[-limit:]


def is_busy() -> bool:
    with _lock:
        return _current_run is not None


def _today_totals() -> dict[str, Any]:
    """Counters for the current local day, read from the database so restarts are safe."""
    from sqlalchemy import func, select

    from app.crawler import day_start_utc  # imported here: crawler imports this module
    from app.db import SessionLocal
    from app.models import CrawlItem, CrawlRun, LlmCall, Summary

    since_utc = day_start_utc().replace(tzinfo=None)
    try:
        with SessionLocal() as session:
            # A run that hit the lock did no work; counting it would inflate the day.
            runs = session.scalars(
                select(CrawlRun).where(
                    CrawlRun.started_at >= since_utc, CrawlRun.status != "skipped"
                )
            ).all()
            run_ids = [r.id for r in runs]
            failed = (
                session.scalar(
                    select(func.count(CrawlItem.id)).where(
                        CrawlItem.run_id.in_(run_ids), CrawlItem.status != "ok"
                    )
                )
                if run_ids
                else 0
            )
            calls = session.scalars(select(LlmCall).where(LlmCall.created_at >= since_utc)).all()
            return {
                "runs": len(runs),
                "games": sum(r.processed for r in runs),
                "summaries": session.scalar(
                    select(func.count(Summary.id)).where(Summary.updated_at >= since_utc)
                )
                or 0,
                "errors": (failed or 0) + sum(1 for c in calls if not c.ok),
                "llm_calls": len(calls),
                "llm_cost": round(sum(c.cost or 0.0 for c in calls), 6),
            }
    except Exception:  # pragma: no cover - the dashboard must render regardless
        return {"runs": 0, "games": 0, "summaries": 0, "errors": 0, "llm_calls": 0, "llm_cost": 0.0}


def snapshot(with_events: bool = True) -> dict[str, Any]:
    """Everything the dashboard needs in one object."""
    with _lock:
        state = {
            "seq": _seq,
            "at": _now(),
            "workers": {k: dict(v) for k, v in _workers.items()},
            "run": dict(_current_run) if _current_run else None,
        }
    state["today"] = _today_totals()
    from app.llm import budget_state

    state["budget"] = budget_state()
    if with_events:
        state["events"] = events()
    return state
