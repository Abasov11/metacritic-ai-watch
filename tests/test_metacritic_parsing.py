"""Parser tests against saved pages. No network access."""

import pytest

from app.scraper.http import ScrapeError
from app.scraper.metacritic import (
    parse_browse_page,
    parse_game,
    parse_new_releases,
    parse_platform_user_score,
    parse_reviews_api,
    parse_reviews_page,
)
from tests.conftest import load_html, load_json

# ------------------------------------------------------------------------ listings


def test_new_releases_returns_twenty_slugs(front_door_html):
    slugs = parse_new_releases(front_door_html)
    assert len(slugs) == 20
    assert len(set(slugs)) == 20
    assert slugs[0] == "onimusha-way-of-the-sword"
    assert all(s.islower() and " " not in s for s in slugs)


def test_new_releases_honours_limit(front_door_html):
    assert parse_new_releases(front_door_html, limit=3) == [
        "onimusha-way-of-the-sword",
        "nba-2k27",
        "the-blood-of-dawnwalker",
    ]


def test_new_releases_excludes_other_carousels(front_door_html):
    # "Trending Now" and the nav menu also link to /game/<slug>/ on this page.
    assert "halloween-the-game" not in parse_new_releases(front_door_html)


def test_browse_page_returns_result_cards(browse_html):
    slugs = parse_browse_page(browse_html)
    assert len(slugs) == 24
    assert slugs[0] == "oblation-prince-of-corruption"
    # The header nav links to games too; only the result grid must be picked up.
    assert "mortal-shell-ii" not in slugs


def test_listing_parsers_reject_unrelated_pages():
    with pytest.raises(ScrapeError):
        parse_browse_page("<html><body>nothing here</body></html>")


# ---------------------------------------------------------------------- game card


@pytest.fixture(scope="module")
def game(game_html):
    return parse_game(game_html, "hollow-knight-silksong")


def test_game_core_fields(game):
    assert game.title == "Hollow Knight: Silksong"
    assert game.slug == "hollow-knight-silksong"
    assert game.metacritic_url == "https://www.metacritic.com/game/hollow-knight-silksong/"
    assert game.developer == "Team Cherry"
    assert game.publisher == "Team Cherry"
    assert game.release_date == "2025-09-04"
    assert game.genres == ["Metroidvania"]
    assert game.description.startswith("Discover a vast, haunted kingdom")


def test_game_cover_points_at_the_unsigned_original(game):
    # Signed /a/img/resize/<hash>/ URLs answer 403 "Invalid hash" when replayed.
    assert game.cover_url == (
        "https://www.metacritic.com/a/img/catalog/provider/7/2/7-1757261088.jpg"
    )
    assert "/resize/" not in game.cover_url


def test_game_video_url(game):
    assert game.video_url == "https://cdn.jwplayer.com/players/gLtOK2AW.html"


def test_game_platforms_carry_metascores(game):
    by_name = {p.name: p for p in game.platforms}
    assert set(by_name) == {
        "Nintendo Switch",
        "Nintendo Switch 2",
        "PC",
        "PlayStation 4",
        "PlayStation 5",
        "Xbox One",
        "Xbox Series X",
    }
    assert by_name["PC"].metascore == 90
    assert by_name["Nintendo Switch"].metascore == 94
    assert by_name["Nintendo Switch"].slug == "nintendo-switch"
    # A platform with no reviews yet must survive as None, not blow up.
    assert by_name["Xbox One"].metascore is None


def test_game_user_score_of_the_rendered_platform(game):
    # The card page only knows the user score of its lead platform (PC).
    by_name = {p.name: p for p in game.platforms}
    assert by_name["PC"].userscore == 8.9
    assert by_name["Nintendo Switch"].userscore is None


def test_parse_game_rejects_a_non_game_page(front_door_html):
    with pytest.raises(ScrapeError):
        parse_game(front_door_html, "whatever")


def test_platform_user_score_from_its_own_page():
    html = load_html("user_reviews_hollow-knight-silksong_nintendo-switch.html")
    assert parse_platform_user_score(html) == 9


def test_platform_user_score_from_api():
    payload = load_json("api_user_score_hollow-knight-silksong_nintendo-switch.json")
    assert parse_platform_user_score(payload) == 9


# ------------------------------------------------------------------------ reviews


def test_critic_reviews_from_page(critic_reviews_html):
    reviews = parse_reviews_page(critic_reviews_html, "critic")
    assert len(reviews) == 10
    first = reviews[0]
    assert first.kind == "critic"
    assert first.author == "GamingBolt"  # publication, not a person
    assert first.score == 100
    assert first.date == "2025-09-08"
    assert first.url.startswith("https://gamingbolt.com/")
    assert "masterpiece" in first.text
    assert all(r.text.strip() for r in reviews)


def test_user_reviews_from_page(user_reviews_html):
    reviews = parse_reviews_page(user_reviews_html, "user")
    assert len(reviews) == 40  # capped by the default limit; the page ships 50
    first = reviews[0]
    assert first.kind == "user"
    assert first.author == "josepepe1986"
    assert first.score == 10
    assert first.date == "2026-09-04"
    assert first.platform == "PC"
    assert all(0 <= r.score <= 10 for r in reviews if r.score is not None)


def test_review_limit_is_respected(user_reviews_html):
    assert len(parse_reviews_page(user_reviews_html, "user", limit=5)) == 5


def test_critic_reviews_from_api():
    payload = load_json("api_critic_reviews_hollow-knight-silksong.json")
    reviews = parse_reviews_api(payload, "critic")
    assert len(reviews) == 10
    assert reviews[0].author == "GamingBolt"
    assert reviews[0].score == 100


def test_reviews_parser_wants_the_matching_component(critic_reviews_html):
    with pytest.raises(ScrapeError):
        parse_reviews_page(critic_reviews_html, "user")


def test_empty_quotes_are_dropped():
    payload = {"data": {"items": [{"quote": "  ", "score": 5}, {"quote": "ok", "score": 7}]}}
    reviews = parse_reviews_api(payload, "user")
    assert [r.text for r in reviews] == ["ok"]


def test_tbd_scores_are_not_reported_as_zero():
    # Below its review threshold Metacritic sends `score: 0` with a null sentiment.
    from app.scraper.metacritic import _score

    assert _score({"score": 0, "reviewCount": 3, "sentiment": None}) is None
    assert _score({"score": None, "reviewCount": None, "sentiment": None}) is None
    assert _score({"score": 8.9, "reviewCount": 7391, "sentiment": "Generally favorable"}) == 8.9
    assert _score(None) is None


def test_new_releases_drops_slugs_that_are_not_slug_shaped(monkeypatch):
    # The slug becomes a URL segment and a cover file name, so it is validated at parse
    # time rather than trusted downstream.
    from app.scraper import metacritic

    payload = {
        "items": [
            {"slug": "good-game"},
            {"slug": "../../etc/passwd"},
            {"slug": "Has Spaces"},
            {"slug": None},
            {"slug": "ok2"},
        ]
    }
    monkeypatch.setattr(metacritic, "_page_components", lambda html: [])
    monkeypatch.setattr(metacritic, "_component", lambda comps, name: payload)
    assert metacritic.parse_new_releases("<html></html>") == ["good-game", "ok2"]
