"""FastAPI app: HTML catalogue, a small JSON API and the hourly crawler."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlparse

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import covers, llm, monitor, similar
from app.config import BASE_DIR, settings
from app.crawler import is_running, process_game, run_crawl
from app.db import SessionLocal, init_db
from app.models import CrawlRun, Game, LetsPlay, Platform, Review, Summary, utcnow
from app.scheduler import create_scheduler

log = logging.getLogger(__name__)

PER_PAGE = 24
REVIEWS_SHOWN = 10
RECENT_RUNS = 10
MAX_QUERY_CHARS = 100
MAX_PLATFORM_FILTERS = 40
MAX_PAGE = 10_000
#: Same idea per game, for the refresh button on a card.
REFRESH_MIN_INTERVAL = 60.0

#: The app serves its own JS and CSS, embeds YouTube, and loads video thumbnails from
#: i.ytimg.com. Nothing else is allowed, and nothing may frame us.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' https://i.ytimg.com https://www.metacritic.com data:; "
        "frame-src https://www.youtube.com; "
        "script-src 'self'; "
        "style-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}
SSE_POLL_SECONDS = 0.5
SSE_HEARTBEAT_SECONDS = 15
# Streams end on their own and the browser's EventSource reconnects. Bounding them
# means a client that vanishes without a clean disconnect cannot pin a task forever.
SSE_MAX_SECONDS = 300
SORTS = {
    "metascore_desc": (Game.best_metascore, "desc"),
    "metascore_asc": (Game.best_metascore, "asc"),
    "userscore_desc": (Game.best_userscore, "desc"),
    "userscore_asc": (Game.best_userscore, "asc"),
    "added_desc": (Game.created_at, "desc"),
}
DEFAULT_SORT = "metascore_desc"

_YOUTUBE_RE = re.compile(r"(?:youtube\.com/(?:watch\?v=|embed/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def as_sentence(text: str | None) -> str:
    """Capitalise the first letter only and end with a single full stop.

    Jinja's `capitalize` lowercases the rest, which turns "YouTube" into "Youtube".
    """
    text = (text or "").strip().rstrip(".")
    if not text:
        return ""
    return f"{text[:1].upper()}{text[1:]}."


def safe_url(url: str | None) -> str | None:
    """Render external links only when they are plain http(s).

    Everything here comes from Metacritic or yt-dlp rather than from a user, so this is
    a backstop: it keeps a `javascript:` or `data:` value from ever reaching an href.
    """
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return url
    # A leading "//" is protocol-relative and points off-site, not at our own tree.
    if not parsed.scheme and url.startswith("/") and not url.startswith("//"):
        return url  # our own /covers/... paths
    return None


def youtube_embed(url: str | None) -> str | None:
    """Embed URL for a YouTube link, or None for anything else (jwplayer, …)."""
    match = _YOUTUBE_RE.search(url or "")
    return f"https://www.youtube.com/embed/{match.group(1)}" if match else None


#: Reasons stored before the texts were written in Russian. Rewritten at startup so
#: old rows read like the new ones; matching is exact, so running it twice is a no-op.
LEGACY_LETSPLAY_ERRORS = {
    "no captions for this video; whisper skipped (whisper disabled)": (
        "у ролика нет субтитров, распознавание речи отключено"
    ),
    "no captions for this video": "у ролика нет субтитров",
}


def translate_letsplay_errors() -> int:
    """Replace known English reasons with the Russian ones shown on a card."""
    with SessionLocal() as session:
        rows = session.scalars(
            select(LetsPlay).where(LetsPlay.error.in_(LEGACY_LETSPLAY_ERRORS))
        ).all()
        for row in rows:
            row.error = LEGACY_LETSPLAY_ERRORS[row.error]
        session.commit()
        return len(rows)


def close_orphaned_runs() -> int:
    """Mark crawls that died with a previous process, so the dashboard stays honest.

    Safe at startup: nothing of ours can be running yet. A crawl started by
    `python -m app.crawler` in a separate process would be mislabelled, but that
    already conflicts with the single-process lock.
    """
    with SessionLocal() as session:
        orphans = session.scalars(select(CrawlRun).where(CrawlRun.status == "running")).all()
        for run in orphans:
            run.status = "aborted"
            run.finished_at = run.finished_at or utcnow()
            run.error = (run.error or "") + " прерван: процесс завершился во время обхода"
        session.commit()
        return len(orphans)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    orphans = close_orphaned_runs()
    if orphans:
        log.warning("closed %d crawl run(s) left running by a previous process", orphans)
    translated = translate_letsplay_errors()
    if translated:
        log.info("rewrote %d legacy let's play reason(s) in Russian", translated)
    monitor.reset()
    monitor.emit(type="startup", message="сервис запущен")

    scheduler = None
    if settings.scheduler_enabled:
        scheduler = create_scheduler()
        scheduler.start()
        log.info("scheduler started, crawling every %d min", settings.crawl_interval_minutes)
    else:
        log.warning("scheduler disabled, crawls only run when triggered by hand")
        monitor.emit(
            type="startup",
            worker="crawler",
            status="idle",
            detail="планировщик выключен",
            message="планировщик выключен настройкой (SCHEDULER_ENABLED=0)",
        )
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(title="Metacritic AI Watch", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
templates.env.globals["youtube_embed"] = youtube_embed
templates.env.globals["safe_url"] = safe_url
templates.env.filters["sentence"] = as_sentence


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response


#: When the last manual crawl was accepted, on the monotonic clock. `None` means never
#: — 0.0 would not do, because monotonic() starts near zero at boot.
_last_manual_run: float | None = None
#: Same, per game slug, for the card refresh button.
_last_refresh: dict[str, float] = {}
#: Slugs a background refresh is working on right now.
_refreshing: set[str] = set()


def get_session():
    with SessionLocal() as session:
        yield session


# --------------------------------------------------------------------------- query


def query_games(
    session: Session,
    q: str = "",
    platforms: list[str] | None = None,
    sort: str = DEFAULT_SORT,
    page: int = 1,
    with_summaries: bool = False,
) -> tuple[list[Game], int]:
    """Filtered, sorted page of games plus the unpaginated total."""
    statement = select(Game)
    q = (q or "")[:MAX_QUERY_CHARS]
    platforms = (platforms or [])[:MAX_PLATFORM_FILTERS]
    if q.strip():
        needle = f"%{q.strip()}%"
        statement = statement.where(or_(Game.title.ilike(needle), Game.developer.ilike(needle)))
    if platforms:
        statement = statement.join(Platform).where(Platform.name.in_(platforms)).distinct()
    if with_summaries:
        # Most freshly released games have no reviews yet, so their summaries are the
        # "Отзывов пока нет" note; this keeps only the ones a model actually wrote.
        statement = statement.where(
            Game.id.in_(select(Summary.game_id).where(Summary.review_count > 0))
        )

    total = session.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))

    column, direction = SORTS.get(sort, SORTS[DEFAULT_SORT])
    # Games without a score belong at the end of either direction, not on top.
    statement = statement.order_by(
        column.is_(None), column.desc() if direction == "desc" else column.asc(), Game.id
    )
    page = min(max(page, 1), MAX_PAGE)
    games = session.scalars(statement.limit(PER_PAGE).offset((page - 1) * PER_PAGE)).all()
    return list(games), total or 0


def all_platform_names(session: Session) -> list[str]:
    return list(session.scalars(select(Platform.name).distinct().order_by(Platform.name)).all())


def summary_flags(session: Session, games: list[Game]) -> dict[int, dict[str, bool]]:
    """Which games have something worth clicking: a real summary, a let's play verdict."""
    ids = [g.id for g in games]
    if not ids:
        return {}
    with_summary = set(
        session.scalars(
            select(Summary.game_id).where(Summary.game_id.in_(ids), Summary.review_count > 0)
        ).all()
    )
    with_letsplay = {
        row.game_id
        for row in session.scalars(select(LetsPlay).where(LetsPlay.game_id.in_(ids))).all()
        if (row.verdict or {}).get("verdict")
    }
    return {
        game.id: {"summary": game.id in with_summary, "letsplay": game.id in with_letsplay}
        for game in games
    }


def game_to_dict(game: Game, full: bool = False) -> dict:
    data = {
        "slug": game.slug,
        "title": game.title,
        "cover_url": f"/covers/{game.cover_path}" if game.cover_path else game.cover_url,
        "cover_source_url": game.cover_url,
        "developer": game.developer,
        "release_date": game.release_date,
        "best_metascore": game.best_metascore,
        "best_userscore": game.best_userscore,
        "platforms": [
            {"name": p.name, "metascore": p.metascore, "userscore": p.userscore}
            for p in game.platforms
        ],
    }
    if not full:
        return data
    return data | {
        "publisher": game.publisher,
        "description": game.description,
        "genres": game.genres,
        "video_url": game.video_url,
        "metacritic_url": game.metacritic_url,
        "last_crawled_at": game.last_crawled_at.isoformat() if game.last_crawled_at else None,
        "letsplay": game.letsplay
        and {
            "video_id": game.letsplay.video_id,
            "url": game.letsplay.url,
            "title": game.letsplay.title,
            "channel": game.letsplay.channel,
            "view_count": game.letsplay.view_count,
            "transcript_source": game.letsplay.transcript_source,
            "transcript_chars": game.letsplay.transcript_chars,
            "verdict": game.letsplay.verdict,
            "model": game.letsplay.model,
            "error": game.letsplay.error,
            "updated_at": game.letsplay.updated_at.isoformat(),
        },
        "summaries": {
            s.kind: {
                "likes": s.likes,
                "dislikes": s.dislikes,
                "summary": s.summary,
                "model": s.model,
                "review_count": s.review_count,
                "updated_at": s.updated_at.isoformat(),
            }
            for s in game.summaries
        },
    }


# ---------------------------------------------------------------------------- html


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: str = "",
    platform: list[str] = Query(default=[]),
    sort: str = DEFAULT_SORT,
    page: int = 1,
    with_summaries: bool = False,
    session: Session = Depends(get_session),
):
    games, total = query_games(session, q, platform, sort, page, with_summaries)
    pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)

    def page_url(target: int) -> str:
        params = [("q", q)] if q else []
        params += [("platform", name) for name in platform]
        if with_summaries:
            params.append(("with_summaries", "1"))
        params += [("sort", sort), ("page", str(target))]
        return "?" + urlencode(params)

    context = {
        "games": games,
        "total": total,
        "page": min(max(page, 1), pages),
        "pages": pages,
        "page_url": page_url,
        "q": q,
        "selected_platforms": platform,
        "sort": sort,
        "with_summaries": with_summaries,
        "flags": summary_flags(session, games),
        "platform_names": all_platform_names(session),
    }
    # HTMX asks for the results only; a normal visit gets the whole page.
    name = "_results.html" if request.headers.get("HX-Request") else "index.html"
    return templates.TemplateResponse(request, name, context)


@app.get("/covers/{name}")
def cover(name: str):
    """Serve a cached cover. Only `<slug>.<ext>` names resolve, so `..` cannot escape."""
    path = covers.path_for(name)
    if path is None:
        raise HTTPException(status_code=404, detail="cover not found")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


def _refresh_game(slug: str) -> None:
    """Re-crawl one game in the background; never lets an error escape the thread."""
    try:
        with SessionLocal() as session:
            status, error = process_game(session, slug)
        monitor.emit(
            type="refresh_done" if status != "failed" else "refresh_failed",
            worker="crawler",
            status="idle",
            slug=slug,
            message=f"{slug}: обновление завершено ({status})" + (f" — {error}" if error else ""),
        )
        similar.invalidate()
    except Exception as exc:  # pragma: no cover - defensive, the thread must not die loudly
        log.exception("refresh of %s failed", slug)
        monitor.emit(
            type="refresh_failed",
            worker="crawler",
            status="idle",
            slug=slug,
            message=f"{slug}: обновление упало — {exc}",
        )
    finally:
        _refreshing.discard(slug)


@app.post("/game/{slug}/refresh")
def refresh_game(slug: str, request: Request, session: Session = Depends(get_session)):
    """Re-fetch one game now. Same guards as the manual crawl, but per game."""
    if not is_same_origin(request):
        return JSONResponse({"detail": "запрос с чужого источника"}, status_code=403)
    if session.scalar(select(Game.id).where(Game.slug == slug)) is None:
        raise HTTPException(status_code=404, detail="game not found")
    if slug in _refreshing:
        return JSONResponse({"detail": "уже обновляется"}, status_code=409)

    last = _last_refresh.get(slug)
    if last is not None:
        waited = time.monotonic() - last
        if waited < REFRESH_MIN_INTERVAL:
            retry_after = int(REFRESH_MIN_INTERVAL - waited) + 1
            return JSONResponse(
                {"detail": f"слишком часто, попробуйте через {retry_after} с"},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

    _last_refresh[slug] = time.monotonic()
    _refreshing.add(slug)
    monitor.emit(
        type="refresh_requested",
        worker="crawler",
        status="busy",
        slug=slug,
        detail=f"refreshing {slug}",
        message=f"{slug}: обновление запрошено из карточки",
    )
    threading.Thread(target=_refresh_game, args=(slug,), daemon=True).start()
    return JSONResponse({"detail": "обновление запущено"}, status_code=202)


@app.get("/game/{slug}/status", response_class=HTMLResponse)
def game_status(request: Request, slug: str, session: Session = Depends(get_session)):
    """The little status strip the card polls while a refresh is running."""
    game = session.scalar(select(Game).where(Game.slug == slug))
    if game is None:
        raise HTTPException(status_code=404, detail="game not found")
    return templates.TemplateResponse(
        request,
        "_game_status.html",
        {"game": game, "busy": slug in _refreshing},
    )


@app.get("/game/{slug}", response_class=HTMLResponse)
def game_page(request: Request, slug: str, session: Session = Depends(get_session)):
    game = session.scalar(select(Game).where(Game.slug == slug))
    if game is None:
        return templates.TemplateResponse(
            request, "not_found.html", {"slug": slug}, status_code=404
        )

    reviews: dict[str, list[Review]] = {}
    for kind in ("critic", "user"):
        reviews[kind] = list(
            session.scalars(
                select(Review)
                .where(Review.game_id == game.id, Review.kind == kind)
                .order_by(Review.score.desc().nulls_last(), Review.id)
                .limit(REVIEWS_SHOWN)
            ).all()
        )

    ranked = similar.similar_games(game.id, k=6)
    by_id = {
        g.id: g
        for g in session.scalars(select(Game).where(Game.id.in_([i for i, _, _ in ranked]))).all()
    }
    return templates.TemplateResponse(
        request,
        "game.html",
        {
            "game": game,
            "busy": slug in _refreshing,
            "summaries": {s.kind: s for s in game.summaries},
            "reviews": reviews,
            "similar": [
                {"game": by_id[i], "score": score, "shared": shared}
                for i, score, shared in ranked
                if i in by_id
            ],
        },
    )


# ------------------------------------------------------------------------- monitor


def recent_runs(session: Session) -> list[CrawlRun]:
    return list(
        session.scalars(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(RECENT_RUNS)).all()
    )


@app.get("/monitor", response_class=HTMLResponse)
def monitor_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request,
        "monitor.html",
        {
            "state": monitor.snapshot() | {"cooldown_left": manual_cooldown_left()},
            "runs": recent_runs(session),
            "busy": is_running(),
        },
    )


@app.get("/monitor/stream")
async def monitor_stream():
    """Server-sent events: a state snapshot, then every new event as it happens."""

    async def gen():
        snapshot = monitor.snapshot() | {"cooldown_left": manual_cooldown_left()}
        yield f"event: state\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
        seq = snapshot["seq"]
        idle_for = 0.0
        deadline = asyncio.get_running_loop().time() + SSE_MAX_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            new = monitor.since(seq)
            if new:
                seq = new[-1]["seq"]
                for event in new:
                    yield f"event: log\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                state = monitor.snapshot(with_events=False) | {
                    "cooldown_left": manual_cooldown_left()
                }
                yield f"event: state\ndata: {json.dumps(state, ensure_ascii=False)}\n\n"
                idle_for = 0.0
            else:
                idle_for += SSE_POLL_SECONDS
                if idle_for >= SSE_HEARTBEAT_SECONDS:
                    idle_for = 0.0
                    yield ": heartbeat\n\n"
            await asyncio.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def manual_cooldown_left() -> float:
    """Seconds until the next manual crawl is allowed. Scheduled runs ignore this."""
    if _last_manual_run is None:
        return 0.0
    cooldown = settings.manual_run_cooldown_minutes * 60
    return max(cooldown - (time.monotonic() - _last_manual_run), 0.0)


def is_same_origin(request: Request) -> bool:
    """Reject cross-site POSTs. A tool with no Origin header (curl) is left alone."""
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return True

    # Host names only: the app never sees the public scheme behind nginx, and nginx
    # forwards `Host` without a port while `Origin` carries one.
    def host_of(value: str) -> str:
        return value.rsplit("@", 1)[-1].rsplit(":", 1)[0].strip("[]").lower()

    return host_of(urlparse(origin).netloc) == host_of(request.headers.get("host", ""))


@app.post("/monitor/run")
def monitor_run(request: Request):
    """Force a crawl. 403 cross-site, 429 too soon, 409 while one is already running."""
    global _last_manual_run

    if not is_same_origin(request):
        return JSONResponse({"detail": "запрос с чужого источника"}, status_code=403)
    if is_running():
        return JSONResponse({"detail": "обход уже идёт"}, status_code=409)

    left = manual_cooldown_left()
    if left > 0:
        minutes = max(int(left // 60) + (1 if left % 60 else 0), 1)
        return JSONResponse(
            {"detail": f"ручной запуск доступен через {minutes} мин"},
            status_code=429,
            headers={"Retry-After": str(int(left) + 1)},
        )
    _last_manual_run = time.monotonic()

    # The 409 above is advisory UX; a request that slips through the race still hits
    # the crawler's own lock and comes back as a `skipped` run.
    threading.Thread(target=run_crawl, args=("manual",), daemon=True).start()
    monitor.emit(type="run_requested", message="запуск обхода вручную из веб-интерфейса")
    return JSONResponse({"detail": "обход запущен"}, status_code=202)


# ----------------------------------------------------------------------------- api


@app.get("/api/games")
def api_games(
    q: str = "",
    platform: list[str] = Query(default=[]),
    sort: str = DEFAULT_SORT,
    page: int = 1,
    with_summaries: bool = False,
    session: Session = Depends(get_session),
):
    games, total = query_games(session, q, platform, sort, page, with_summaries)
    return {
        "total": total,
        "page": max(page, 1),
        "per_page": PER_PAGE,
        "items": [game_to_dict(g) for g in games],
    }


@app.get("/api/games/{slug}")
def api_game(slug: str, session: Session = Depends(get_session)):
    game = session.scalar(select(Game).where(Game.slug == slug))
    if game is None:
        raise HTTPException(status_code=404, detail="game not found")
    return game_to_dict(game, full=True)


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)):
    run = session.scalar(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(1))
    state = monitor.snapshot(with_events=False)
    return {
        "status": "ok",
        "llm_budget": llm.budget_state(),
        "games": session.scalar(select(func.count(Game.id))) or 0,
        "crawl_running": is_running(),
        "workers": state["workers"],
        "today": state["today"],
        "last_run": run
        and {
            "id": run.id,
            "source": run.source,
            "status": run.status,
            "processed": run.processed,
            "failed": run.failed,
            "started_at": run.started_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        },
    }
