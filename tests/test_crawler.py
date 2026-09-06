"""Crawler tests. No network, no LLM: the scraper and `summarize_reviews` are stubbed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import crawler, similar
from app.models import Base, CrawlItem, CrawlRun, Game, Platform, Review, Summary
from app.scraper.metacritic import GameData, PlatformScore
from app.scraper.metacritic import Review as ScrapedReview

MSK = 3  # Europe/Moscow is UTC+3; 00:30 MSK is 21:30 UTC the previous day.


@pytest.fixture
def db(monkeypatch, tmp_path):
    """Fresh SQLite file per test, wired into every module that opens sessions."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(crawler, "SessionLocal", factory)
    monkeypatch.setattr(crawler, "init_db", lambda: None)
    monkeypatch.setattr(similar, "SessionLocal", factory)
    similar.invalidate()
    return factory


def make_game(slug: str, title: str | None = None) -> GameData:
    return GameData(
        title=title or slug.replace("-", " ").title(),
        slug=slug,
        metacritic_url=f"https://www.metacritic.com/game/{slug}/",
        cover_url=f"https://img.test/{slug}.jpg",
        description=f"A game called {slug}",
        developer="Test Studio",
        publisher="Test Publisher",
        release_date="2026-09-01",
        genres=["Action"],
        platforms=[
            PlatformScore("PC", "pc", 80, 7.5),
            PlatformScore("PlayStation 5", "playstation-5", 85, 8.0),
        ],
        video_url="https://video.test/x",
    )


@pytest.fixture
def scraper(monkeypatch):
    """Stub the whole scraper surface and record what was asked for."""
    calls = {"games": [], "new_releases": 0, "browse": [], "covers": []}

    def fetch_new_releases(**_kwargs):
        calls["new_releases"] += 1
        return [f"nr-{i}" for i in range(20)]

    def fetch_browse_new(page, **_kwargs):
        calls["browse"].append(page)
        return [f"p{page}-{i}" for i in range(24)]

    def fetch_game(slug, **_kwargs):
        calls["games"].append(slug)
        return make_game(slug)

    def fetch_reviews(slug, kind, limit=40, client=None):
        return [
            ScrapedReview(
                f"{kind} review {i} of {slug}", 8.0, f"{kind}-author-{i}", "2026-09-01", kind
            )
            for i in range(3)
        ]

    monkeypatch.setattr(crawler.metacritic, "fetch_new_releases", fetch_new_releases)
    monkeypatch.setattr(crawler.metacritic, "fetch_browse_new", fetch_browse_new)
    monkeypatch.setattr(crawler.metacritic, "fetch_game", fetch_game)
    def cache_cover(slug, url, client=None, force=False):
        calls["covers"].append((slug, url))
        return f"{slug}.jpg"

    monkeypatch.setattr(crawler.metacritic, "fetch_reviews", fetch_reviews)
    # Covers have their own tests; here they must never reach the network.
    monkeypatch.setattr(crawler.covers, "cache_cover", cache_cover)
    return calls


@pytest.fixture
def llm(monkeypatch):
    def summarize_reviews(title, kind, reviews, game_id):
        return {
            "likes": [f"{kind} like"],
            "dislikes": [f"{kind} dislike"],
            "summary": f"{kind} summary of {title}",
            "model": "test/model",
        }

    monkeypatch.setattr(crawler, "summarize_reviews", summarize_reviews)
    return summarize_reviews


# ------------------------------------------------------------------ source rotation


def test_first_run_of_the_day_uses_new_releases(db, scraper, llm):
    run = crawler.run_crawl("test", limit=2)
    assert run.source == "new_releases"
    assert scraper["new_releases"] == 1
    assert scraper["browse"] == []


def test_later_runs_the_same_day_walk_browse_pages(db, scraper, llm):
    assert crawler.run_crawl("test", limit=2).source == "new_releases"
    assert crawler.run_crawl("test", limit=2).source == "browse:1"
    assert crawler.run_crawl("test", limit=2).source == "browse:2"
    assert crawler.run_crawl("test", limit=2).source == "browse:3"
    assert scraper["browse"] == [1, 2, 3]


def test_a_new_day_starts_over_at_new_releases(db, scraper, llm):
    yesterday = datetime.now(UTC) - timedelta(days=1)
    crawler.run_crawl("test", limit=2, now=yesterday)
    crawler.run_crawl("test", limit=2, now=yesterday)
    with db() as session:
        # Backdate the runs themselves; `now=` only steers the day boundary.
        for run in session.scalars(select(CrawlRun)).all():
            run.started_at = yesterday.replace(tzinfo=None)
        session.commit()

    assert crawler.run_crawl("test", limit=2).source == "new_releases"


def test_day_boundary_uses_the_configured_timezone(db):
    # 21:30 UTC is already the next day in Moscow, so the local day starts at 21:00 UTC.
    moment = datetime(2026, 9, 6, 21, 30, tzinfo=UTC)
    start = crawler.day_start_utc(moment)
    assert start == datetime(2026, 9, 6, 21, 0, tzinfo=UTC)
    assert start.astimezone(UTC) <= moment


def test_skipped_runs_do_not_advance_the_page(db, scraper, llm):
    with db() as session:
        session.add(CrawlRun(source="-", status="skipped"))
        session.commit()
        assert crawler.pick_source(session) == "new_releases"


# ------------------------------------------------------------------ list building


def test_games_already_crawled_today_are_skipped(db, scraper, llm):
    crawler.run_crawl("test", limit=3)
    first = list(scraper["games"])
    assert first == ["nr-0", "nr-1", "nr-2"]

    scraper["games"].clear()
    crawler.run_crawl("test", limit=3)
    # Second run moved to browse; none of the already-done slugs may come back.
    assert not set(scraper["games"]) & set(first)


def test_short_pages_are_topped_up_from_the_next_page(db, scraper, llm, monkeypatch):
    monkeypatch.setattr(
        crawler.metacritic, "fetch_browse_new", lambda page, **_k: [f"p{page}-0", f"p{page}-1"]
    )
    with db() as session:
        session.add(CrawlRun(source="new_releases", status="ok"))
        session.commit()
    run = crawler.run_crawl("test", limit=5)
    assert run.planned == 5
    assert run.source == "browse:3"  # needed pages 1..3 to reach five games


# ------------------------------------------------------------------------- upserts


def test_recrawl_updates_instead_of_duplicating(db, scraper, llm, monkeypatch):
    crawler.run_crawl("test", limit=1)

    # Same slug, new title and a better score; reviews partly repeat.
    changed = make_game("nr-0", title="Renamed")
    changed.platforms[0].metascore = 91
    monkeypatch.setattr(crawler.metacritic, "fetch_game", lambda slug, **_k: changed)
    monkeypatch.setattr(crawler.metacritic, "fetch_browse_new", lambda page, **_k: ["nr-0"])
    with db() as session:
        session.scalar(select(Game).where(Game.slug == "nr-0")).last_crawled_at = None
        session.commit()
    crawler.run_crawl("test", limit=1)

    with db() as session:
        assert session.scalar(select(func.count(Game.id))) == 1
        game = session.scalar(select(Game).where(Game.slug == "nr-0"))
        assert game.title == "Renamed"
        assert game.best_metascore == 91
        assert session.scalar(select(func.count(Platform.id))) == 2
        assert session.scalar(select(func.count(Review.id))) == 6  # 3 critic + 3 user
        assert session.scalar(select(func.count(Summary.id))) == 2


def test_the_cover_is_cached_locally_during_a_crawl(db, scraper, llm):
    crawler.run_crawl("test", limit=1)
    assert scraper["covers"] == [("nr-0", "https://img.test/nr-0.jpg")]
    with db() as session:
        assert session.scalar(select(Game).where(Game.slug == "nr-0")).cover_path == "nr-0.jpg"


def test_scraped_fields_land_in_the_database(db, scraper, llm):
    crawler.run_crawl("test", limit=1)
    with db() as session:
        game = session.scalar(select(Game).where(Game.slug == "nr-0"))
        assert game.developer == "Test Studio"
        assert game.genres == ["Action"]
        assert game.video_url == "https://video.test/x"
        assert game.best_userscore == 8.0
        assert {p.name for p in game.platforms} == {"PC", "PlayStation 5"}
        assert {s.kind: s.summary for s in game.summaries} == {
            "critic": "critic summary of Nr 0",
            "user": "user summary of Nr 0",
        }


def test_missing_reviews_get_a_note_not_a_summary(db, scraper, monkeypatch):
    monkeypatch.setattr(crawler.metacritic, "fetch_reviews", lambda *a, **k: [])

    def boom(*_a, **_k):
        raise AssertionError("must not call the LLM without reviews")

    monkeypatch.setattr(crawler, "summarize_reviews", boom)
    crawler.run_crawl("test", limit=1)

    with db() as session:
        summaries = session.scalars(select(Summary)).all()
        assert len(summaries) == 2
        assert all(s.summary == "Отзывов пока нет." and s.likes == [] for s in summaries)


# ---------------------------------------------------------------------- resilience


def test_one_broken_game_does_not_stop_the_run(db, scraper, llm, monkeypatch):
    def fetch_game(slug, **_kwargs):
        if slug == "nr-1":
            raise RuntimeError("boom")
        return make_game(slug)

    monkeypatch.setattr(crawler.metacritic, "fetch_game", fetch_game)
    run = crawler.run_crawl("test", limit=3)

    assert run.processed == 2
    assert run.failed == 1
    assert run.status == "partial"
    with db() as session:
        items = {i.slug: i for i in session.scalars(select(CrawlItem)).all()}
        assert items["nr-1"].status == "failed"
        assert "boom" in items["nr-1"].error
        assert session.scalar(select(func.count(Game.id))) == 2


def test_llm_failure_keeps_the_game_due_for_a_retry(db, scraper, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(crawler, "summarize_reviews", boom)
    run = crawler.run_crawl("test", limit=1)

    with db() as session:
        game = session.scalar(select(Game).where(Game.slug == "nr-0"))
        assert game is not None  # scraped data was kept
        assert game.last_crawled_at is None  # ...but it will be picked up again
        item = session.scalar(select(CrawlItem))
        assert item.status == "partial"
        assert "llm down" in item.error
    assert run.processed == 1


def test_a_second_crawl_cannot_start_while_one_is_running(db, scraper, llm):
    crawler._run_lock.acquire()
    try:
        run = crawler.run_crawl("test", limit=1)
    finally:
        crawler._run_lock.release()

    assert run.status == "skipped"
    assert scraper["games"] == []


# ------------------------------------------------------------------ similar games


def test_similar_games_ranks_the_closest_first(db, scraper, llm, monkeypatch):
    games = {
        "metroid": ("Silksong", ["Metroidvania"], "bug knight explores a haunted kingdom"),
        "metroid2": ("Hollow Knight", ["Metroidvania"], "knight explores a haunted bug kingdom"),
        "sport": ("NBA 2K27", ["Basketball Sim"], "basketball simulation league season"),
    }

    def fetch_game(slug, **_kwargs):
        title, genres, description = games[slug]
        data = make_game(slug, title=title)
        data.genres, data.description = genres, description
        return data

    monkeypatch.setattr(crawler.metacritic, "fetch_game", fetch_game)
    monkeypatch.setattr(crawler.metacritic, "fetch_new_releases", lambda **_k: list(games))
    crawler.run_crawl("test", limit=3)

    with db() as session:
        ids = {g.slug: g.id for g in session.scalars(select(Game)).all()}
    ranked = similar.similar_games(ids["metroid"], k=5)
    by_id = dict(ranked)
    assert ranked[0][0] == ids["metroid2"]
    # The basketball game shares only boilerplate (studio, platforms), which idf zeroes.
    assert by_id.get(ids["sport"], 0.0) < by_id[ids["metroid2"]]
