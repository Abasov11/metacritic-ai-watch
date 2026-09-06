"""The hourly crawl: pick 20 games, scrape them, summarise their reviews.

Selection follows the assignment: the first run of a day takes New Releases from the
games front door, every later run that day takes the next page of "SEE ALL / New".
The page counter is derived from today's `crawl_runs` rows rather than kept in its
own table, so it resets with the day for free and cannot drift out of sync.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app import covers, monitor, similar, youtube
from app.config import settings
from app.db import SessionLocal, init_db
from app.llm import build_tags, summarize_reviews
from app.models import (
    CrawlItem,
    CrawlRun,
    Game,
    LetsPlay,
    Platform,
    Review,
    Summary,
    text_hash,
    utcnow,
)
from app.scraper import metacritic
from app.scraper.http import default_client

log = logging.getLogger(__name__)

MAX_BROWSE_PAGES_PER_RUN = 5

# ponytail: a process-local lock. Single-process deployment; if the service is ever
# forked across workers this becomes an advisory row in the DB.
_run_lock = threading.Lock()


def is_running() -> bool:
    """True while a crawl holds the lock. Advisory: the lock itself is the real guard."""
    return _run_lock.locked()


def day_start_utc(now: datetime | None = None) -> datetime:
    """Midnight of the current local day, as UTC — the boundary for "already today"."""
    tz = ZoneInfo(settings.tz)
    local = (now or utcnow()).astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def pick_source(session, now: datetime | None = None) -> str:
    """`new_releases` for the first run of the day, else the next browse page."""
    since = day_start_utc(now)
    sources = session.scalars(
        select(CrawlRun.source)
        .where(CrawlRun.started_at >= since.replace(tzinfo=None), CrawlRun.status != "skipped")
        .order_by(CrawlRun.id)
    ).all()
    if not sources:
        return "new_releases"
    pages = [int(s.split(":", 1)[1]) for s in sources if s.startswith("browse:")]
    return f"browse:{max(pages, default=0) + 1}"


def _crawled_today(session, now: datetime | None = None) -> set[str]:
    since = day_start_utc(now).replace(tzinfo=None)
    return set(session.scalars(select(Game.slug).where(Game.last_crawled_at >= since)).all())


def select_slugs(
    session, source: str, limit: int, now: datetime | None = None
) -> tuple[list[str], str]:
    """Slugs to crawl and the source actually consumed (browse may span pages)."""
    seen = _crawled_today(session, now)
    client = default_client()

    if source == "new_releases":
        slugs = [s for s in metacritic.fetch_new_releases(client=client) if s not in seen]
        return slugs[:limit], source

    page = int(source.split(":", 1)[1])
    slugs: list[str] = []
    for offset in range(MAX_BROWSE_PAGES_PER_RUN):
        for slug in metacritic.fetch_browse_new(page + offset, client=client):
            if slug not in seen and slug not in slugs:
                slugs.append(slug)
        if len(slugs) >= limit:
            return slugs[:limit], f"browse:{page + offset}"
    return slugs[:limit], f"browse:{page + MAX_BROWSE_PAGES_PER_RUN - 1}"


# ------------------------------------------------------------------------- one game


def _upsert_game(session, data: metacritic.GameData) -> Game:
    game = session.scalar(select(Game).where(Game.slug == data.slug))
    if game is None:
        game = Game(slug=data.slug)
        session.add(game)

    game.title = data.title
    game.cover_url = data.cover_url
    game.description = data.description
    game.developer = data.developer
    game.publisher = data.publisher
    game.release_date = data.release_date
    game.genres = data.genres
    game.video_url = data.video_url
    game.metacritic_url = data.metacritic_url
    game.best_metascore = max((p.metascore for p in data.platforms if p.metascore), default=None)
    game.best_userscore = max((p.userscore for p in data.platforms if p.userscore), default=None)
    session.flush()

    existing = {p.name: p for p in game.platforms}
    for scraped in data.platforms:
        platform = existing.get(scraped.name) or Platform(game_id=game.id, name=scraped.name)
        platform.metascore = scraped.metascore
        platform.userscore = scraped.userscore
        if platform.id is None:
            session.add(platform)
    session.flush()
    return game


def _upsert_reviews(session, game: Game, kind: str, reviews: list[metacritic.Review]) -> None:
    known = set(
        session.scalars(
            select(Review.text_hash).where(Review.game_id == game.id, Review.kind == kind)
        ).all()
    )
    for scraped in reviews:
        digest = text_hash(scraped.text)
        if digest in known:
            continue
        known.add(digest)
        session.add(
            Review(
                game_id=game.id,
                kind=kind,
                author=scraped.author,
                score=scraped.score,
                text=scraped.text,
                text_hash=digest,
                date=scraped.date,
            )
        )
    session.flush()


def _upsert_summary(session, game: Game, kind: str, **fields) -> None:
    summary = session.scalar(
        select(Summary).where(Summary.game_id == game.id, Summary.kind == kind)
    )
    if summary is None:
        summary = Summary(game_id=game.id, kind=kind)
        session.add(summary)
    for key, value in fields.items():
        setattr(summary, key, value)
    summary.updated_at = utcnow()
    session.flush()


def _letsplay_is_current(letsplay: LetsPlay | None) -> bool:
    """A recent, successful lookup is not worth repeating."""
    if letsplay is None or letsplay.transcript_source == "none":
        return False
    age = utcnow() - letsplay.updated_at.replace(tzinfo=UTC)
    return age < timedelta(days=settings.youtube_max_age_days)


def _tags_hash(game: Game) -> str:
    """Identity of the text the tags describe."""
    return text_hash(f"{game.title}|{' '.join(game.genres or [])}|{game.description or ''}")


def process_tags(session, game: Game) -> str | None:
    """Tag the game unless the same text is already tagged. Returns an error string."""
    digest = _tags_hash(game)
    if game.tags and game.tags_hash == digest:
        return None

    reviews = list(
        session.scalars(select(Review.text).where(Review.game_id == game.id).limit(5)).all()
    )
    monitor.emit(
        type="tags_start",
        worker="llm",
        status="busy",
        detail=f"tagging {game.slug}",
        slug=game.slug,
        message=f"размечаю теги для {game.title}",
    )
    try:
        tags = build_tags(game.title, game.genres or [], game.description, reviews, game.id)
    except Exception as exc:
        log.warning("tagging failed for %s: %s", game.slug, exc)
        return f"теги: {exc}"
    if not tags:
        return "теги: модель вернула пустой ответ"

    game.tags = tags
    game.tags_hash = digest
    session.commit()
    return None


def process_letsplay(session, game: Game) -> str | None:
    """Find and summarise a let's play. Returns an error string, never raises."""
    existing = session.scalar(select(LetsPlay).where(LetsPlay.game_id == game.id))
    if _letsplay_is_current(existing):
        return None

    monitor.emit(
        type="letsplay_start",
        worker="youtube",
        status="busy",
        detail=f"searching {game.slug}",
        slug=game.slug,
        message=f"ищу летсплей для {game.title}",
    )
    try:
        record = youtube.build_letsplay(game.title, game.id)
    except Exception as exc:  # build_letsplay swallows its own, this is belt and braces
        log.exception("letsplay stage crashed for %s", game.slug)
        record = {"transcript_source": "none", "error": str(exc)[:500]}

    if existing is None:
        existing = LetsPlay(game_id=game.id)
        session.add(existing)
    for key, value in record.items():
        setattr(existing, key, value)
    existing.updated_at = utcnow()
    session.commit()

    monitor.emit(
        type="letsplay_error" if record.get("error") else "letsplay_done",
        worker="youtube",
        status="idle",
        slug=game.slug,
        message=(
            f"{game.slug}: {record['error']}"
            if record.get("error")
            else f"{game.slug}: «{record.get('title')}» "
            f"({record.get('view_count')} просмотров, {record.get('transcript_source')})"
        ),
    )
    return record.get("error")


def process_game(session, slug: str) -> tuple[str, str | None]:
    """Scrape and summarise one game. Returns (status, error)."""
    client = default_client()
    monitor.emit(
        type="game_start",
        worker="crawler",
        status="busy",
        detail=f"fetching {slug}",
        slug=slug,
        message=f"загружаю карточку {slug}",
    )
    data = metacritic.fetch_game(slug, client=client)
    game = _upsert_game(session, data)
    game.cover_path = covers.cache_cover(slug, data.cover_url, client)
    # SQLite takes a single write lock. Commit before every slow network call, or the
    # LLM client's own connection cannot write its `llm_calls` row and times out.
    session.commit()

    llm_error: str | None = None
    for kind in ("critic", "user"):
        monitor.emit(
            type="reviews",
            worker="crawler",
            status="busy",
            detail=f"reviews {slug} {kind}",
            slug=slug,
            message=f"отзывы {kind} для {slug}",
        )
        scraped = metacritic.fetch_reviews(slug, kind, settings.reviews_per_kind, client)
        _upsert_reviews(session, game, kind, scraped)

        stored = session.scalars(
            select(Review)
            .where(Review.game_id == game.id, Review.kind == kind)
            .limit(settings.reviews_per_kind)
        ).all()
        if not stored:
            _upsert_summary(
                session,
                game,
                kind,
                likes=[],
                dislikes=[],
                summary="Отзывов пока нет.",
                model=None,
                review_count=0,
            )
            session.commit()
            continue

        session.commit()
        monitor.emit(
            type="summary_start",
            worker="crawler",
            status="busy",
            detail=f"summarizing {slug} {kind}",
            slug=slug,
            message=f"резюме {kind} для {slug} ({len(stored)} отзывов)",
        )
        try:
            result = summarize_reviews(
                game.title, kind, [(r.author, r.score, r.text) for r in stored], game.id
            )
        except Exception as exc:
            # Keep the scraped data; the game stays due for a retry next run.
            llm_error = f"{kind}: {exc}"
            log.warning("summary failed for %s (%s): %s", slug, kind, exc)
            monitor.emit(
                type="error",
                worker="crawler",
                status="busy",
                slug=slug,
                message=f"резюме {kind} для {slug} не удалось: {exc}",
            )
            continue
        _upsert_summary(session, game, kind, review_count=len(stored), **result)
        session.commit()

    tags_error = process_tags(session, game)
    if tags_error:
        log.info("no tags for %s: %s", slug, tags_error)

    if settings.youtube_enabled:
        letsplay_error = process_letsplay(session, game)
        if letsplay_error:
            log.info("no letsplay for %s: %s", slug, letsplay_error)

    if llm_error is None:
        game.last_crawled_at = utcnow()
    session.commit()
    return ("ok", None) if llm_error is None else ("partial", llm_error)


# ------------------------------------------------------------------------ whole run


def run_crawl(
    reason: str = "scheduled", limit: int | None = None, now: datetime | None = None
) -> CrawlRun:
    """One pass. Concurrent calls return immediately with a `skipped` run."""
    init_db()
    limit = limit or settings.crawl_batch_size

    if not _run_lock.acquire(blocking=False):
        log.warning("a crawl is already running, skipping")
        monitor.emit(type="run_skipped", message=f"обход ({reason}) пропущен: уже идёт другой")
        with SessionLocal() as session:
            run = CrawlRun(
                reason=reason,
                source="-",
                status="skipped",
                finished_at=utcnow(),
                error="another crawl is already running",
            )
            session.add(run)
            session.commit()
            return run
    try:
        return _run_crawl(reason, limit, now)
    finally:
        _run_lock.release()


def _run_crawl(reason: str, limit: int, now: datetime | None) -> CrawlRun:
    with SessionLocal() as session:
        source = pick_source(session, now)
        run = CrawlRun(reason=reason, source=source, status="running")
        session.add(run)
        session.commit()
        log.info("crawl #%d started (%s, source=%s)", run.id, reason, source)
        monitor.emit(
            type="run_start",
            worker="crawler",
            status="busy",
            detail=f"building list ({source})",
            run_id=run.id,
            source=source,
            reason=reason,
            message=f"обход #{run.id} начат ({reason}, {source})",
        )

        try:
            slugs, source = select_slugs(session, source, limit, now)
        except Exception as exc:
            log.exception("could not build the crawl list")
            run.status, run.error, run.finished_at = "failed", str(exc)[:1000], utcnow()
            session.commit()
            monitor.emit(
                type="run_end",
                worker="crawler",
                status="idle",
                run_id=run.id,
                message=f"обход #{run.id} упал на построении списка: {exc}",
            )
            return run

        run.source, run.planned = source, len(slugs)
        session.commit()
        monitor.emit(
            type="run_planned",
            worker="crawler",
            status="busy",
            detail=f"{source}: 0/{len(slugs)}",
            source=source,
            planned=len(slugs),
            message=f"к обработке {len(slugs)} игр ({source})",
        )

        for slug in slugs:
            try:
                status, error = process_game(session, slug)
            except Exception as exc:
                session.rollback()
                status, error = "failed", str(exc)[:1000]
                log.exception("game %s failed", slug)
            session.add(CrawlItem(run_id=run.id, slug=slug, status=status, error=error))
            if status == "failed":
                run.failed += 1
            else:
                run.processed += 1
            session.commit()
            monitor.emit(
                type="game_failed" if status == "failed" else "game_done",
                worker="crawler",
                status="busy",
                slug=slug,
                detail=f"{run.processed + run.failed}/{run.planned}",
                message=f"{slug}: {status}" + (f" — {error}" if error else ""),
            )

        run.status, run.finished_at = "ok" if not run.failed else "partial", utcnow()
        session.commit()
        # Descriptions and platforms may have changed without the game count changing.
        similar.invalidate()
        log.info("crawl #%d done: %d processed, %d failed", run.id, run.processed, run.failed)
        monitor.emit(
            type="run_end",
            worker="crawler",
            status="idle",
            run_id=run.id,
            message=f"обход #{run.id} завершён: {run.processed} обработано, {run.failed} с ошибкой",
        )
        return run


_VIDEO_ID_IN_ERROR = re.compile(r"\[youtube\]\s+[\w-]{6,}:\s*")


def _reason(error: str | None) -> str:
    """Group failures by cause, not by which video hit it."""
    text = _VIDEO_ID_IN_ERROR.sub("", error or "без причины")
    return " ".join(text.split())[:70]


def refresh_letsplays(limit: int | None = None) -> int:
    """Retry the transcript for videos we found but could not read."""
    init_db()
    with SessionLocal() as session:
        pending = session.scalars(
            select(LetsPlay)
            .where(LetsPlay.video_id.is_not(None), LetsPlay.transcript_source == "none")
            .order_by(LetsPlay.game_id)
        ).all()
        slugs = [session.get(Game, row.game_id).slug for row in pending]

    slugs = slugs[: limit or len(slugs)]
    print(f"к пересчёту: {len(slugs)} записей с роликом, но без расшифровки")
    got = still_none = 0
    reasons: Counter[str] = Counter()
    for slug in slugs:
        with SessionLocal() as session:
            game = session.scalar(select(Game).where(Game.slug == slug))
            existing = session.scalar(select(LetsPlay).where(LetsPlay.game_id == game.id))
            if existing is not None:
                session.delete(existing)
                session.commit()
            process_letsplay(session, game)
            row = session.scalar(select(LetsPlay).where(LetsPlay.game_id == game.id))
            if row.transcript_source != "none":
                got += 1
                log.info("%s: %s, %d символов", slug, row.transcript_source, row.transcript_chars)
            else:
                still_none += 1
                reasons[_reason(row.error)] += 1
    print(f"получили расшифровку: {got}, осталось без неё: {still_none}")
    for reason, count in reasons.most_common():
        print(f"  {count:>3} x {reason}")
    return 0


def backfill_tags(limit: int | None = None) -> int:
    """Tag every game in the database that has no current tags."""
    init_db()
    done = skipped = failed = 0
    with SessionLocal() as session:
        games = session.scalars(select(Game).order_by(Game.id)).all()
        for game in games[: limit or len(games)]:
            if game.tags and game.tags_hash == _tags_hash(game):
                skipped += 1
                continue
            error = process_tags(session, game)
            if error:
                failed += 1
                log.warning("%s: %s", game.slug, error)
            else:
                done += 1
                log.info("%s -> %s", game.slug, json.dumps(game.tags, ensure_ascii=False))
    similar.invalidate()
    print(f"теги: {done} размечено, {skipped} уже актуальны, {failed} с ошибкой")
    return 0 if not failed else 1


def refresh_covers() -> int:
    """Re-fetch every cover already referenced in the database."""
    init_db()
    client = default_client()
    done = failed = 0
    with SessionLocal() as session:
        for game in session.scalars(select(Game).order_by(Game.id)).all():
            # Rows written before the signed-URL fix still point at a 403; repair them.
            game.cover_url = covers.unsigned_url(game.cover_url)
            name = covers.cache_cover(game.slug, game.cover_url, client, force=True)
            if name:
                game.cover_path = name
                done += 1
            else:
                failed += 1
                log.warning("no cover for %s (%s)", game.slug, game.cover_url)
            session.commit()
    print(f"covers refreshed: {done} ok, {failed} failed -> {covers.covers_dir()}")
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Metacritic crawl.")
    parser.add_argument("--once", action="store_true", help="run a single crawl and exit")
    parser.add_argument("--limit", type=int, default=None, help="max games this run")
    parser.add_argument("--reason", default="manual")
    parser.add_argument(
        "--refresh-letsplays",
        action="store_true",
        help="retry transcripts for videos found without one, then exit",
    )
    parser.add_argument(
        "--backfill-tags",
        action="store_true",
        help="tag every game already in the database, then exit",
    )
    parser.add_argument(
        "--refresh-covers",
        action="store_true",
        help="re-download the cover of every game already in the database, then exit",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.refresh_letsplays:
        return refresh_letsplays(args.limit)
    if args.backfill_tags:
        return backfill_tags(args.limit)
    if args.refresh_covers:
        return refresh_covers()
    if not args.once:
        parser.error("only --once is supported; use app.scheduler for the hourly loop")

    run = run_crawl(args.reason, args.limit)
    print(
        f"run #{run.id} source={run.source} status={run.status} "
        f"planned={run.planned} processed={run.processed} failed={run.failed}"
    )
    return 0 if run.status in ("ok", "partial") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
