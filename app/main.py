"""FastAPI app: HTML catalogue, a small JSON API and the hourly crawler."""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import similar
from app.config import BASE_DIR, settings
from app.db import SessionLocal, init_db
from app.models import CrawlRun, Game, Platform, Review
from app.scheduler import create_scheduler

log = logging.getLogger(__name__)

PER_PAGE = 24
REVIEWS_SHOWN = 10
SORTS = {
    "metascore_desc": (Game.best_metascore, "desc"),
    "metascore_asc": (Game.best_metascore, "asc"),
    "userscore_desc": (Game.best_userscore, "desc"),
    "userscore_asc": (Game.best_userscore, "asc"),
    "added_desc": (Game.created_at, "desc"),
}
DEFAULT_SORT = "metascore_desc"

_YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|v/)|youtu\.be/)([A-Za-z0-9_-]{11})"
)


def youtube_embed(url: str | None) -> str | None:
    """Embed URL for a YouTube link, or None for anything else (jwplayer, …)."""
    match = _YOUTUBE_RE.search(url or "")
    return f"https://www.youtube.com/embed/{match.group(1)}" if match else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = create_scheduler()
    scheduler.start()
    log.info("scheduler started, crawling every %d min", settings.crawl_interval_minutes)
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Metacritic AI Watch", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
templates.env.globals["youtube_embed"] = youtube_embed


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
) -> tuple[list[Game], int]:
    """Filtered, sorted page of games plus the unpaginated total."""
    statement = select(Game)
    if q.strip():
        needle = f"%{q.strip()}%"
        statement = statement.where(
            or_(Game.title.ilike(needle), Game.developer.ilike(needle))
        )
    if platforms:
        statement = statement.join(Platform).where(Platform.name.in_(platforms)).distinct()

    total = session.scalar(
        select(func.count()).select_from(statement.order_by(None).subquery())
    )

    column, direction = SORTS.get(sort, SORTS[DEFAULT_SORT])
    # Games without a score belong at the end of either direction, not on top.
    statement = statement.order_by(
        column.is_(None), column.desc() if direction == "desc" else column.asc(), Game.id
    )
    page = max(page, 1)
    games = session.scalars(statement.limit(PER_PAGE).offset((page - 1) * PER_PAGE)).all()
    return list(games), total or 0


def all_platform_names(session: Session) -> list[str]:
    return list(session.scalars(select(Platform.name).distinct().order_by(Platform.name)).all())


def game_to_dict(game: Game, full: bool = False) -> dict:
    data = {
        "slug": game.slug,
        "title": game.title,
        "cover_url": game.cover_url,
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
    session: Session = Depends(get_session),
):
    games, total = query_games(session, q, platform, sort, page)
    pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)

    def page_url(target: int) -> str:
        params = [("q", q)] if q else []
        params += [("platform", name) for name in platform]
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
        "platform_names": all_platform_names(session),
    }
    # HTMX asks for the results only; a normal visit gets the whole page.
    name = "_results.html" if request.headers.get("HX-Request") else "index.html"
    return templates.TemplateResponse(request, name, context)


@app.get("/game/{slug}", response_class=HTMLResponse)
def game_page(request: Request, slug: str, session: Session = Depends(get_session)):
    game = session.scalar(select(Game).where(Game.slug == slug))
    if game is None:
        raise HTTPException(status_code=404, detail="game not found")

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
        for g in session.scalars(select(Game).where(Game.id.in_([i for i, _ in ranked]))).all()
    }
    return templates.TemplateResponse(
        request,
        "game.html",
        {
            "game": game,
            "summaries": {s.kind: s for s in game.summaries},
            "reviews": reviews,
            "similar": [by_id[i] for i, _ in ranked if i in by_id],
        },
    )


# ----------------------------------------------------------------------------- api


@app.get("/api/games")
def api_games(
    q: str = "",
    platform: list[str] = Query(default=[]),
    sort: str = DEFAULT_SORT,
    page: int = 1,
    session: Session = Depends(get_session),
):
    games, total = query_games(session, q, platform, sort, page)
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
    return {
        "status": "ok",
        "games": session.scalar(select(func.count(Game.id))) or 0,
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
