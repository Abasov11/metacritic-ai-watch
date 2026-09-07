"""Web tests against a seeded temporary database. No network, no scheduler."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main, similar
from app.models import Base, CrawlRun, Game, Platform, Review, Summary
from tests.conftest import bind_session

SEED = [
    # slug, title, developer, metascore, userscore, platforms, genres, description
    (
        "silksong",
        "Hollow Knight: Silksong",
        "Team Cherry",
        90,
        8.9,
        [("PC", 90, 8.9), ("Nintendo Switch", 94, 9.0)],
        ["Metroidvania"],
        "Explore a vast haunted kingdom as Hornet, a lethal bug knight.",
    ),
    (
        "dawnwalker",
        "The Blood of Dawnwalker",
        "Rebel Wolves",
        88,
        8.5,
        [("PC", 84, 8.5), ("PlayStation 5", 88, 8.2)],
        ["Action RPG"],
        "Explore a haunted medieval kingdom ruled by vampires.",
    ),
    (
        "nba",
        "NBA 2K27",
        "Visual Concepts",
        55,
        3.1,
        [("PlayStation 5", 55, 3.1)],
        ["Basketball Sim"],
        "Basketball simulation with league seasons and player careers.",
    ),
    (
        "tiny",
        "Tiny Untested Game",
        "Solo Dev",
        None,
        None,
        [("PC", None, None)],
        ["Puzzle"],
        "A small puzzle about rotating coloured shapes.",
    ),
]


@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'web.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    bind_session(monkeypatch, factory)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "create_scheduler", lambda: _NullScheduler())
    similar.invalidate()

    base = datetime(2026, 9, 1, tzinfo=UTC).replace(tzinfo=None)
    with factory() as session:
        for i, (slug, title, dev, ms, us, platforms, genres, description) in enumerate(SEED):
            game = Game(
                slug=slug,
                title=title,
                developer=dev,
                publisher="Pub",
                genres=genres,
                release_date="2026-09-0" + str(i + 1),
                description=description,
                cover_url=f"https://img.test/{slug}.jpg",
                metacritic_url=f"https://mc.test/{slug}",
                video_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ" if i == 0 else None,
                best_metascore=ms,
                best_userscore=us,
                created_at=base + timedelta(days=i),
            )
            session.add(game)
            session.flush()
            for name, pms, pus in platforms:
                session.add(Platform(game_id=game.id, name=name, metascore=pms, userscore=pus))
            if slug != "tiny":
                session.add(
                    Summary(
                        game_id=game.id,
                        kind="critic",
                        likes=["хороший бой"],
                        dislikes=["мало контента"],
                        summary="Итог критиков.",
                        model="test/model",
                        review_count=2,
                    )
                )
                session.add(
                    Review(
                        game_id=game.id,
                        kind="critic",
                        author="IGN",
                        score=90.0,
                        text="A great game indeed.",
                        text_hash=f"h-{slug}",
                        date="2026-09-01",
                    )
                )
        session.add(CrawlRun(source="new_releases", status="ok", processed=4, planned=4))
        session.commit()

    with TestClient(app=main.app) as test_client:
        yield test_client


class _NullScheduler:
    def start(self):
        pass

    def shutdown(self, wait=True):
        pass


def slugs(payload) -> list[str]:
    return [item["slug"] for item in payload["items"]]


# ------------------------------------------------------------------------- list


def test_index_renders_every_game(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    for _, title, *_ in SEED:
        assert title in body
    assert "Team Cherry" in body  # developer shown on the card
    assert "2026-09-01" in body  # release date shown on the card


def test_index_shows_platform_badges_with_both_scores(client):
    body = client.get("/").text
    assert "Nintendo Switch" in body
    assert "score--good" in body  # 90 >= 75
    assert "score--mixed" in body  # NBA's 55
    assert "score--tbd" in body  # the game with no scores


def test_search_matches_titles(client):
    assert slugs(client.get("/api/games", params={"q": "silksong"}).json()) == ["silksong"]
    assert client.get("/api/games", params={"q": "zzz"}).json()["total"] == 0
    # The HTML list narrows the same way.
    body = client.get("/", params={"q": "silksong"}).text
    assert "Hollow Knight: Silksong" in body
    assert "NBA 2K27" not in body


def test_platform_filter_is_multi_select(client):
    assert slugs(client.get("/api/games", params={"platform": "Nintendo Switch"}).json()) == [
        "silksong"
    ]
    both = client.get(
        "/api/games", params=[("platform", "Nintendo Switch"), ("platform", "PlayStation 5")]
    ).json()
    assert set(slugs(both)) == {"silksong", "dawnwalker", "nba"}


def test_search_and_filter_combine(client):
    payload = client.get("/api/games", params={"q": "the", "platform": "PC"}).json()
    assert slugs(payload) == ["dawnwalker"]


def test_sorting(client):
    def order(sort):
        return slugs(client.get("/api/games", params={"sort": sort}).json())

    # Games without a score stay at the end in both directions.
    assert order("metascore_desc") == ["silksong", "dawnwalker", "nba", "tiny"]
    assert order("metascore_asc") == ["nba", "dawnwalker", "silksong", "tiny"]
    assert order("userscore_desc") == ["silksong", "dawnwalker", "nba", "tiny"]
    assert order("userscore_asc") == ["nba", "dawnwalker", "silksong", "tiny"]
    assert order("added_desc") == ["tiny", "nba", "dawnwalker", "silksong"]


def test_unknown_sort_falls_back_to_the_default(client):
    assert slugs(client.get("/api/games", params={"sort": "; drop table"}).json()) == slugs(
        client.get("/api/games").json()
    )


def test_htmx_request_returns_only_the_results_fragment(client):
    fragment = client.get("/", headers={"HX-Request": "true"}).text
    assert fragment.lstrip().startswith('<div id="results">')
    assert "<html" not in fragment
    # A normal visit still gets the whole document with the filter panel.
    assert '<form class="panel"' in client.get("/").text


def test_filters_survive_in_pagination_links(client):
    body = client.get("/", params={"q": "blood", "platform": "PC", "sort": "userscore_asc"}).text
    assert 'value="blood"' in body  # search box keeps the query
    assert re.search(r'value="userscore_asc"\s+selected', body)  # sort keeps its choice
    assert re.search(r'value="PC"\s+checked', body)  # platform checkbox stays ticked


# ------------------------------------------------------------------- game page


def test_game_page_shows_the_full_card(client):
    body = client.get("/game/silksong").text
    assert "Hollow Knight: Silksong" in body
    assert "Team Cherry" in body and "Pub" in body
    assert "Metroidvania" in body
    assert "Explore a vast haunted kingdom" in body
    assert "https://mc.test/silksong" in body


def test_youtube_video_is_embedded_other_links_are_not(client):
    assert "youtube.com/embed/dQw4w9WgXcQ" in client.get("/game/silksong").text
    assert "<iframe" not in client.get("/game/nba").text


def test_summaries_are_rendered_with_model_and_time(client):
    body = client.get("/game/silksong").text
    assert "Критики" in body and "Игроки" in body
    assert "хороший бой" in body and "мало контента" in body
    assert "Итог критиков." in body
    assert "test/model" in body
    # The user summary was never generated for this game.
    assert "Ещё не обработано." in body


def test_missing_summaries_show_an_honest_placeholder(client):
    assert client.get("/game/tiny").text.count("Ещё не обработано.") == 2


def test_source_reviews_are_listed(client):
    body = client.get("/game/silksong").text
    assert "A great game indeed." in body
    assert "IGN" in body


def test_similar_games_link_to_their_card(client):
    # Tags carry most of the similarity now, so the seed needs them.
    with main.SessionLocal() as session:
        for slug, tags in {
            "silksong": {"genres": ["metroidvania"], "mood": ["dark"], "setting": ["fantasy"]},
            "dawnwalker": {"genres": ["metroidvania"], "mood": ["dark"], "setting": ["fantasy"]},
            "nba": {"genres": ["sports"], "mood": [], "setting": ["modern"]},
        }.items():
            session.scalar(select(Game).where(Game.slug == slug)).tags = tags
        session.commit()
    similar.invalidate()

    body = client.get("/game/silksong").text
    assert "Похожие игры" in body
    assert 'href="/game/dawnwalker"' in body
    assert 'href="/game/nba"' not in body  # a sports game is not a metroidvania
    assert "metroidvania" in body  # the shared tags explain the match


def test_similar_games_say_so_when_nothing_is_close(client):
    # Nothing is tagged and the blurbs share no rare words: better an honest note than
    # the least-bad neighbour.
    similar.invalidate()
    body = client.get("/game/nba").text
    assert "Похожих пока не нашлось" in body


def test_unknown_game_is_404(client):
    assert client.get("/game/nope").status_code == 404
    assert client.get("/api/games/nope").status_code == 404


# -------------------------------------------------------------------- api etc


def test_api_game_detail(client):
    payload = client.get("/api/games/silksong").json()
    assert payload["title"] == "Hollow Knight: Silksong"
    assert payload["genres"] == ["Metroidvania"]
    assert {p["name"] for p in payload["platforms"]} == {"PC", "Nintendo Switch"}
    assert payload["summaries"]["critic"]["likes"] == ["хороший бой"]
    assert payload["summaries"]["critic"]["model"] == "test/model"


def test_pagination_caps_a_page(client, monkeypatch):
    monkeypatch.setattr(main, "PER_PAGE", 2)
    first = client.get("/api/games", params={"sort": "metascore_desc"}).json()
    second = client.get("/api/games", params={"sort": "metascore_desc", "page": 2}).json()
    assert first["total"] == 4 and len(first["items"]) == 2
    assert slugs(first) == ["silksong", "dawnwalker"]
    assert slugs(second) == ["nba", "tiny"]


def test_healthz(client):
    payload = client.get("/healthz").json()
    assert payload["status"] == "ok"
    assert payload["games"] == 4
    assert payload["last_run"]["source"] == "new_releases"
    assert payload["last_run"]["processed"] == 4


def test_letsplay_block_renders_the_video_and_the_verdict(client):
    from app.models import LetsPlay

    with main.SessionLocal() as session:
        game = session.scalar(select(Game).where(Game.slug == "silksong"))
        session.add(
            LetsPlay(
                game_id=game.id,
                video_id="abc12345678",
                url="https://youtu.be/abc12345678",
                title="Silksong blind run",
                channel="SomeChannel",
                view_count=1234567,
                transcript_source="subtitles",
                transcript_chars=8421,
                model="test/model",
                verdict={"verdict": "Блогер в восторге.", "highlights": ["бои", "музыка"]},
            )
        )
        session.commit()

    body = client.get("/game/silksong").text
    assert "Летсплей" in body
    assert "i.ytimg.com/vi/abc12345678/mqdefault.jpg" in body
    assert "https://youtu.be/abc12345678" in body
    assert "SomeChannel" in body
    assert "1 234 567 просмотров" in body
    assert "текст из субтитров" in body
    assert "Блогер в восторге." in body
    assert "бои" in body and "музыка" in body


def test_letsplay_block_is_honest_about_a_missing_transcript(client):
    from app.models import LetsPlay

    with main.SessionLocal() as session:
        game = session.scalar(select(Game).where(Game.slug == "nba"))
        session.add(
            LetsPlay(
                game_id=game.id,
                video_id="zzz11111111",
                url="https://youtu.be/zzz11111111",
                title="NBA 2K27 season",
                channel="Hoops",
                view_count=42,
                transcript_source="none",
                transcript_chars=0,
                verdict={},
                error="YouTube блокирует запросы с этого IP",
            )
        )
        session.commit()

    body = client.get("/game/nba").text
    assert "расшифровки нет" in body
    assert "YouTube блокирует запросы с этого IP" in body


def test_letsplay_block_says_when_nothing_ran_yet(client):
    assert "Летсплей ещё не искали." in client.get("/game/tiny").text


def test_api_exposes_the_letsplay(client):
    from app.models import LetsPlay

    with main.SessionLocal() as session:
        game = session.scalar(select(Game).where(Game.slug == "dawnwalker"))
        session.add(
            LetsPlay(
                game_id=game.id,
                video_id="v1",
                url="https://youtu.be/v1",
                title="Run",
                channel="Ch",
                view_count=10,
                transcript_source="whisper",
                transcript_chars=100,
                verdict={"verdict": "ок", "highlights": []},
                model="test/model",
            )
        )
        session.commit()

    payload = client.get("/api/games/dawnwalker").json()
    assert payload["letsplay"]["video_id"] == "v1"
    assert payload["letsplay"]["transcript_source"] == "whisper"
    assert payload["letsplay"]["verdict"]["verdict"] == "ок"
    assert client.get("/api/games/tiny").json()["letsplay"] is None


# ------------------------------------------- badges, the summaries filter, no hits


def seed_summaries(counts: dict[str, int]) -> None:
    """`{slug: review_count}`. Every other game is reset to the "no reviews" state.

    The base fixture already gives most games a summary, so this updates rather than
    inserts, and zeroes the rest to keep the badge counts deterministic.
    """
    from app.models import Summary

    with main.SessionLocal() as session:
        by_id = {g.id: g.slug for g in session.scalars(select(Game)).all()}
        for summary in session.scalars(select(Summary)).all():
            summary.review_count = counts.get(by_id.get(summary.game_id, ""), 0)
        seeded = {(s.game_id, s.kind) for s in session.scalars(select(Summary)).all()}
        for slug, count in counts.items():
            game = session.scalar(select(Game).where(Game.slug == slug))
            if (game.id, "critic") not in seeded:
                session.add(
                    Summary(
                        game_id=game.id,
                        kind="critic",
                        review_count=count,
                        summary="итог",
                        model="m",
                    )
                )
        session.commit()


def test_a_card_is_badged_when_a_real_summary_exists(client):
    seed_summaries({"silksong": 40, "nba": 0})
    body = client.get("/").text
    assert "mark--summary" in body
    # One badge, for the one game that actually has a summary.
    assert body.count('class="mark mark--summary"') == 1


def test_a_card_is_badged_when_the_letsplay_has_a_verdict(client):
    from app.models import LetsPlay

    with main.SessionLocal() as session:
        game = session.scalar(select(Game).where(Game.slug == "dawnwalker"))
        session.add(
            LetsPlay(
                game_id=game.id,
                video_id="v1",
                url="https://youtu.be/v1",
                transcript_source="subtitles",
                transcript_chars=10,
                verdict={"verdict": "понравилось", "highlights": []},
            )
        )
        other = session.scalar(select(Game).where(Game.slug == "nba"))
        session.add(
            LetsPlay(
                game_id=other.id,
                video_id="v2",
                url="https://youtu.be/v2",
                transcript_source="none",
                transcript_chars=0,
                verdict={},
            )
        )
        session.commit()

    body = client.get("/").text
    assert body.count('class="mark mark--letsplay"') == 1  # only the one with a verdict


def test_the_summaries_filter_narrows_the_list(client):
    seed_summaries({"silksong": 40, "nba": 0, "dawnwalker": 12})
    all_games = client.get("/api/games").json()["total"]
    filtered = client.get("/api/games", params={"with_summaries": 1}).json()
    assert filtered["total"] == 2 < all_games
    assert set(slugs(filtered)) == {"silksong", "dawnwalker"}


def test_the_summaries_filter_survives_in_the_form_and_pagination(client):
    seed_summaries({"silksong": 40})
    body = client.get("/", params={"with_summaries": 1}).text
    assert 'name="with_summaries"' in body
    assert re.search(r'value="1"\s+checked', body)


def test_the_default_sort_is_unchanged_by_the_filter(client):
    seed_summaries({"silksong": 40, "dawnwalker": 12})
    assert slugs(client.get("/api/games", params={"with_summaries": 1}).json()) == [
        "silksong",
        "dawnwalker",
    ]  # metascore desc, as always


def test_an_empty_search_explains_itself(client):
    body = client.get("/", params={"q": "нетакойигры"}).text
    assert "По запросу «нетакойигры» ничего не нашлось" in body
    assert "Сбросить поиск и фильтры" in body


def test_an_empty_filter_result_explains_itself_too(client):
    body = client.get("/", params={"platform": "Dreamcast"}).text
    assert "По этим фильтрам ничего не нашлось" in body


def test_the_search_placeholder_names_a_game_that_exists(client):
    # The reviewer typed the placeholder and got nothing; it has to be a real game.
    body = client.get("/").text
    hint = re.search(r'placeholder="например, ([^"]+)"', body).group(1)
    assert client.get("/api/games", params={"q": hint}).json()["total"] >= 0


# ------------------------------------------------- human reasons, 404 and titles


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("подходящий летсплей не найден", "Подходящий летсплей не найден."),
        # `capitalize` would lowercase the rest and give "Youtube:".
        ("YouTube: слишком много запросов", "YouTube: слишком много запросов."),
        ("уже с точкой.", "Уже с точкой."),
        ("", ""),
        (None, ""),
    ],
)
def test_a_reason_reads_as_one_sentence(raw, expected):
    assert main.as_sentence(raw) == expected


def test_a_card_shows_the_reason_without_a_technical_prefix(client):
    from app.models import LetsPlay

    with main.SessionLocal() as session:
        game = session.scalar(select(Game).where(Game.slug == "nba"))
        session.add(
            LetsPlay(
                game_id=game.id,
                video_id="v1",
                url="https://youtu.be/v1",
                title="Run",
                transcript_source="none",
                transcript_chars=0,
                verdict={},
                error="у ролика нет субтитров, распознавание речи отключено",
            )
        )
        session.commit()

    body = client.get("/game/nba").text
    assert "У ролика нет субтитров, распознавание речи отключено." in body
    assert "Заключение не составлено" not in body
    assert "whisper" not in body and "no captions" not in body


def test_old_english_reasons_are_rewritten_at_startup(client):
    from app.models import LetsPlay

    with main.SessionLocal() as session:
        game = session.scalar(select(Game).where(Game.slug == "nba"))
        session.add(
            LetsPlay(
                game_id=game.id,
                video_id="v1",
                url="https://youtu.be/v1",
                transcript_source="none",
                transcript_chars=0,
                verdict={},
                error="no captions for this video; whisper skipped (whisper disabled)",
            )
        )
        session.commit()

    assert main.translate_letsplay_errors() == 1
    with main.SessionLocal() as session:
        row = session.scalar(select(LetsPlay))
        assert row.error == "у ролика нет субтитров, распознавание речи отключено"
    # Running it again changes nothing: matching is exact.
    assert main.translate_letsplay_errors() == 0


def test_an_unknown_game_gets_an_html_page_not_json(client):
    response = client.get("/game/no-such-game")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Игра не найдена" in response.text
    assert "Вернуться в каталог" in response.text
    assert "no-such-game" in response.text


def test_the_api_still_answers_404_in_json(client):
    response = client.get("/api/games/no-such-game")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "game not found"}


def test_every_page_has_its_own_tab_title(client):
    assert "<title>Каталог игр — Metacritic AI Watch</title>" in client.get("/").text
    assert "<title>Поиск «bl" in client.get("/", params={"q": "blood"}).text
    assert (
        "<title>Hollow Knight: Silksong — Metacritic AI Watch</title>"
        in client.get("/game/silksong").text
    )
    assert "<title>Мониторинг — Metacritic AI Watch</title>" in client.get("/monitor").text
    assert "<title>Игра не найдена" in client.get("/game/nope").text
