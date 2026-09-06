"""Let's-play stage. No network: search, captions and the LLM are all stubbed."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import crawler, youtube
from app.models import Base, Game, LetsPlay

SEARCH_RESULTS = [
    {"id": "trailer1", "title": "Silksong — Official Launch Trailer", "duration": 300,
     "view_count": 9_000_000, "channel": "IGN", "description": "Hollow Knight Silksong"},
    {"id": "short1", "title": "Hollow Knight Silksong first boss", "duration": 45,
     "view_count": 5_000_000, "channel": "Clips", "description": "silksong"},
    {"id": "review1", "title": "Hollow Knight Silksong Review", "duration": 900,
     "view_count": 3_000_000, "channel": "Critic", "description": "silksong review"},
    {"id": "other1", "title": "Elden Ring let's play part 1", "duration": 3600,
     "view_count": 8_000_000, "channel": "Someone", "description": "elden ring"},
    {"id": "good1", "title": "Hollow Knight Silksong — playthrough part 1", "duration": 3600,
     "view_count": 120_000, "channel": "Small", "description": "silksong blind run"},
    {"id": "good2", "title": "Playing Hollow Knight: Silksong for the first time",
     "duration": 5400, "view_count": 480_000, "channel": "Big", "description": "silksong"},
]


@pytest.fixture(autouse=True)
def no_cookies(monkeypatch):
    monkeypatch.setattr(youtube.settings, "youtube_cookies_file", "")
    monkeypatch.setattr(youtube.settings, "youtube_whisper_enabled", False)


@pytest.fixture
def search(monkeypatch):
    queries = []

    def _ytsearch(query):
        queries.append(query)
        return SEARCH_RESULTS

    monkeypatch.setattr(youtube, "_ytsearch", _ytsearch)
    return queries


# ------------------------------------------------------------------- selection


def test_picks_the_most_watched_real_playthrough(search):
    video = youtube.search_letsplay("Hollow Knight: Silksong")
    assert video.video_id == "good2"  # 480k, beats good1; the bigger numbers are junk
    assert video.channel == "Big"
    assert video.view_count == 480_000
    assert video.url == "https://www.youtube.com/watch?v=good2"
    assert search == ["ytsearch15:Hollow Knight: Silksong let's play"]


@pytest.mark.parametrize(
    "entry,reason",
    [
        (SEARCH_RESULTS[0], "trailer"),
        (SEARCH_RESULTS[1], "shorter than three minutes"),
        (SEARCH_RESULTS[2], "review"),
        (SEARCH_RESULTS[3], "a different game"),
    ],
)
def test_junk_is_filtered_out(entry, reason):
    assert youtube.is_letsplay(entry, "Hollow Knight: Silksong") is False, reason


def test_real_playthroughs_are_kept():
    for entry in SEARCH_RESULTS[4:]:
        assert youtube.is_letsplay(entry, "Hollow Knight: Silksong") is True


def test_a_game_name_in_the_description_is_enough():
    entry = {"id": "x", "title": "Blind run, episode 1", "duration": 2000,
             "view_count": 10, "description": "Hollow Knight Silksong gameplay"}
    assert youtube.is_letsplay(entry, "Hollow Knight: Silksong") is True


def test_no_candidates_returns_nothing(monkeypatch):
    monkeypatch.setattr(youtube, "_ytsearch", lambda query: SEARCH_RESULTS[:4])
    assert youtube.search_letsplay("Hollow Knight: Silksong") is None


def test_search_failures_are_wrapped(monkeypatch):
    def boom(query):
        raise OSError("network down")

    monkeypatch.setattr(youtube, "_ytsearch", boom)
    with pytest.raises(youtube.YouTubeError):
        youtube.search_letsplay("Anything")


# ------------------------------------------------------------------ transcript


def test_subtitles_are_preferred(monkeypatch):
    monkeypatch.setattr(youtube, "fetch_subtitles", lambda vid: "он говорит вот это")
    assert youtube.get_transcript("v", 60) == ("он говорит вот это", "subtitles", None)


def test_without_subtitles_and_without_whisper_the_source_is_none(monkeypatch):
    monkeypatch.setattr(youtube, "fetch_subtitles", lambda vid: "")
    text, source, error = youtube.get_transcript("v", 60)
    assert (text, source) == ("", "none")
    assert "whisper skipped" in error


def test_a_blocked_subtitle_request_is_reported_not_raised(monkeypatch):
    def blocked(vid):
        raise RuntimeError("RequestBlocked: YouTube is blocking requests from your IP")

    monkeypatch.setattr(youtube, "fetch_subtitles", blocked)
    text, source, error = youtube.get_transcript("v", 60)
    assert (text, source) == ("", "none")
    assert "blocking requests" in error


def test_whisper_runs_only_when_it_is_affordable(monkeypatch):
    monkeypatch.setattr(youtube, "fetch_subtitles", lambda vid: "")
    monkeypatch.setattr(youtube, "whisper_is_affordable", lambda: (True, "plenty"))
    monkeypatch.setattr(youtube, "transcribe_audio", lambda vid, left: "распознанный текст")
    assert youtube.get_transcript("v", 300) == ("распознанный текст", "whisper", None)


def test_a_whisper_crash_degrades_to_none(monkeypatch):
    monkeypatch.setattr(youtube, "fetch_subtitles", lambda vid: "")
    monkeypatch.setattr(youtube, "whisper_is_affordable", lambda: (True, "plenty"))

    def boom(vid, left):
        raise MemoryError("out of memory")

    monkeypatch.setattr(youtube, "transcribe_audio", boom)
    text, source, error = youtube.get_transcript("v", 300)
    assert (text, source) == ("", "none")
    assert "whisper failed" in error


def test_whisper_is_refused_when_it_is_switched_off():
    affordable, why = youtube.whisper_is_affordable()
    assert affordable is False and why == "whisper disabled"


def test_whisper_is_refused_when_memory_is_short(monkeypatch):
    monkeypatch.setattr(youtube.settings, "youtube_whisper_enabled", True)
    monkeypatch.setattr(youtube.settings, "youtube_whisper_min_free_mb", 10**9)
    affordable, why = youtube.whisper_is_affordable()
    assert affordable is False
    assert "need" in why or "not installed" in why


def test_long_transcripts_keep_the_start_and_the_middle():
    text = "A" * 5000 + "B" * 5000 + "C" * 5000
    trimmed = youtube.trim(text, limit=900)
    assert len(trimmed) <= 900 + len("\n[…]\n")
    assert trimmed.startswith("A")
    assert "[…]" in trimmed
    assert youtube.trim("short text", limit=900) == "short text"


# ----------------------------------------------------------------- whole stage


@pytest.fixture
def llm(monkeypatch):
    calls = []

    def chat_json(prompt, schema, *, purpose, game_id=None):
        calls.append((purpose, game_id, prompt))
        return {"verdict": "Блогеру понравилось.", "highlights": ["бои", "музыка"],
                "_model": "test/model"}

    monkeypatch.setattr(youtube, "chat_json", chat_json)
    return calls


def test_build_letsplay_happy_path(search, llm, monkeypatch):
    monkeypatch.setattr(youtube, "fetch_subtitles", lambda vid: "речь блогера " * 50)
    record = youtube.build_letsplay("Hollow Knight: Silksong", 7)

    assert record["video_id"] == "good2"
    assert record["transcript_source"] == "subtitles"
    assert record["transcript_chars"] == len("речь блогера " * 50)
    assert record["verdict"] == {"verdict": "Блогеру понравилось.",
                                 "highlights": ["бои", "музыка"]}
    assert record["model"] == "test/model"
    assert record["error"] is None
    assert llm[0][0] == "letsplay" and llm[0][1] == 7


def test_build_letsplay_records_why_it_failed(search, monkeypatch):
    monkeypatch.setattr(youtube, "fetch_subtitles", lambda vid: "")
    record = youtube.build_letsplay("Hollow Knight: Silksong", 7)

    # The video was still found and stored; only the transcript is missing.
    assert record["video_id"] == "good2"
    assert record["transcript_source"] == "none"
    assert record["transcript_chars"] == 0
    assert record["verdict"] == {}
    assert "whisper skipped" in record["error"]


def test_build_letsplay_never_calls_the_llm_without_text(search, llm, monkeypatch):
    monkeypatch.setattr(youtube, "fetch_subtitles", lambda vid: "")
    youtube.build_letsplay("Hollow Knight: Silksong", 7)
    assert llm == []


def test_build_letsplay_survives_an_llm_failure(search, monkeypatch):
    monkeypatch.setattr(youtube, "fetch_subtitles", lambda vid: "речь")

    def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(youtube, "chat_json", boom)
    record = youtube.build_letsplay("Hollow Knight: Silksong", 7)
    assert record["transcript_source"] == "subtitles"
    assert "llm down" in record["error"]


def test_build_letsplay_reports_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(youtube, "_ytsearch", lambda query: [])
    record = youtube.build_letsplay("Obscure Game", None)
    assert record["video_id"] is None
    assert record["error"] == "подходящий летсплей не найден"


# ------------------------------------------------------------ crawler wiring


@pytest.fixture
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'yt.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(crawler, "SessionLocal", factory)
    with factory() as session:
        session.add(Game(slug="silksong", title="Hollow Knight: Silksong", genres=[]))
        session.commit()
    return factory


def game_of(factory) -> Game:
    with factory() as session:
        return session.scalar(select(Game))


def test_the_stage_stores_a_record(db, monkeypatch):
    monkeypatch.setattr(
        crawler.youtube, "build_letsplay",
        lambda title, game_id, budget=None: {
            "video_id": "abc", "url": "https://y/abc", "title": "Run", "channel": "Ch",
            "view_count": 5, "transcript_source": "subtitles", "transcript_chars": 10,
            "verdict": {"verdict": "ок", "highlights": []}, "model": "m", "error": None,
        },
    )
    with db() as session:
        game = session.scalar(select(Game))
        assert crawler.process_letsplay(session, game) is None
        stored = session.scalar(select(LetsPlay))
        assert (stored.video_id, stored.channel, stored.transcript_source) == (
            "abc", "Ch", "subtitles"
        )


def test_a_fresh_letsplay_is_not_recomputed(db, monkeypatch):
    calls = []

    def build(title, game_id, budget=None):
        calls.append(title)
        return {"video_id": "abc", "transcript_source": "subtitles", "error": None,
                "verdict": {"verdict": "ок"}, "transcript_chars": 5}

    monkeypatch.setattr(crawler.youtube, "build_letsplay", build)
    with db() as session:
        game = session.scalar(select(Game))
        crawler.process_letsplay(session, game)
        crawler.process_letsplay(session, game)
    assert len(calls) == 1


def test_a_stale_letsplay_is_recomputed(db, monkeypatch):
    from datetime import UTC, datetime, timedelta

    calls = []
    monkeypatch.setattr(
        crawler.youtube, "build_letsplay",
        lambda title, game_id, budget=None: (
            calls.append(title),
            {"video_id": "abc", "transcript_source": "subtitles", "error": None,
             "verdict": {}, "transcript_chars": 5},
        )[1],
    )
    with db() as session:
        game = session.scalar(select(Game))
        crawler.process_letsplay(session, game)
        stale = datetime.now(UTC) - timedelta(days=crawler.settings.youtube_max_age_days + 1)
        session.scalar(select(LetsPlay)).updated_at = stale.replace(tzinfo=None)
        session.commit()
        crawler.process_letsplay(session, game)
    assert len(calls) == 2


def test_a_failed_lookup_is_retried_next_time(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        crawler.youtube, "build_letsplay",
        lambda title, game_id, budget=None: (
            calls.append(title),
            {"video_id": None, "transcript_source": "none", "transcript_chars": 0,
             "verdict": {}, "error": "нет расшифровки"},
        )[1],
    )
    with db() as session:
        game = session.scalar(select(Game))
        assert crawler.process_letsplay(session, game) == "нет расшифровки"
        crawler.process_letsplay(session, game)
    assert len(calls) == 2  # `none` is never treated as current


def test_a_crashing_stage_does_not_break_the_crawl(db, monkeypatch):
    def boom(title, game_id, budget=None):
        raise RuntimeError("yt-dlp exploded")

    monkeypatch.setattr(crawler.youtube, "build_letsplay", boom)
    with db() as session:
        game = session.scalar(select(Game))
        error = crawler.process_letsplay(session, game)
    assert "yt-dlp exploded" in error
    with db() as session:
        assert session.scalar(select(LetsPlay)).transcript_source == "none"
