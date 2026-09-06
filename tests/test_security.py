"""Security controls: headers, cross-site POSTs, rate limit, URL scheme, input caps."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import main, similar
from app.models import Base, Game


@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sec.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "create_scheduler", lambda: _NullScheduler())
    monkeypatch.setattr(main, "run_crawl", lambda reason: None)
    monkeypatch.setattr(main, "_last_manual_run", 0.0)
    monkeypatch.setattr(similar, "SessionLocal", factory)
    similar.invalidate()
    with factory() as session:
        session.add(Game(slug="a-game", title="A Game", genres=[]))
        session.commit()
    with TestClient(app=main.app) as test_client:
        yield test_client


class _NullScheduler:
    def start(self):
        pass

    def shutdown(self, wait=True):
        pass


# --------------------------------------------------------------------- headers


@pytest.mark.parametrize("path", ["/", "/game/a-game", "/monitor", "/healthz", "/api/games"])
def test_security_headers_are_on_every_response(client, path):
    headers = client.get(path).headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_csp_allows_only_what_the_pages_actually_need(client):
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp  # no 'unsafe-inline'
    assert "https://i.ytimg.com" in csp  # let's play thumbnails
    assert "https://www.metacritic.com" in csp  # cover fallback when the copy failed
    assert "frame-src https://www.youtube.com" in csp  # trailer embeds
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp


def test_a_cover_that_failed_to_cache_is_still_allowed_by_the_csp(client):
    with main.SessionLocal() as session:
        game = session.scalar(select(Game).where(Game.slug == "a-game"))
        game.cover_path = None
        game.cover_url = "https://www.metacritic.com/a/img/catalog/x.jpg"
        session.commit()
    response = client.get("/")
    assert "https://www.metacritic.com/a/img/catalog/x.jpg" in response.text
    host = "https://www.metacritic.com"
    csp = response.headers["Content-Security-Policy"]
    img_src = next(part for part in csp.split(";") if part.strip().startswith("img-src"))
    assert host in img_src


def test_htmx_is_told_not_to_inject_inline_styles(client):
    # A strict style-src would block htmx's injected <style> block.
    assert '"includeIndicatorStyles":false' in client.get("/").text


def test_the_first_manual_run_after_boot_is_not_rate_limited(client, monkeypatch):
    # monotonic() starts near zero at boot, so a 0.0 sentinel would refuse this.
    monkeypatch.setattr(main, "_last_manual_run", None)
    monkeypatch.setattr(main.time, "monotonic", lambda: 3.0)
    assert client.post("/monitor/run").status_code == 202


def test_origin_with_an_explicit_port_still_matches_the_host(client):
    response = client.post(
        "/monitor/run",
        headers={"Origin": "https://example.test:8443", "Host": "example.test"},
    )
    assert response.status_code == 202


def test_no_inline_script_survives_in_the_templates():
    # A strict script-src would silently break an inline handler, so there must be none.
    from pathlib import Path

    for template in Path("app/templates").glob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert "<script>" not in text, template
        assert "onclick=" not in text and "onerror=" not in text, template


# ------------------------------------------------------------ POST /monitor/run


def test_manual_run_accepts_a_same_origin_post(client):
    response = client.post("/monitor/run", headers={"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 202


def test_manual_run_rejects_a_cross_site_post(client):
    response = client.post("/monitor/run", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert response.json()["detail"] == "запрос с чужого источника"


def test_manual_run_rejects_a_foreign_origin_without_fetch_metadata(client):
    response = client.post(
        "/monitor/run", headers={"Origin": "https://evil.example", "Host": "testserver"}
    )
    assert response.status_code == 403


def test_manual_run_allows_a_matching_origin(client):
    response = client.post(
        "/monitor/run", headers={"Origin": "http://testserver", "Host": "testserver"}
    )
    assert response.status_code == 202


def test_manual_run_allows_a_tool_with_no_origin(client):
    # curl and the healthcheck have nothing to forge; only browsers send Origin.
    assert client.post("/monitor/run").status_code == 202


def test_manual_run_is_rate_limited(client):
    assert client.post("/monitor/run").status_code == 202
    second = client.post("/monitor/run")
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) > 0
    assert "слишком часто" in second.json()["detail"]


def test_a_refused_run_never_starts_a_crawl(client, monkeypatch):
    started = []
    monkeypatch.setattr(main, "run_crawl", lambda reason: started.append(reason))
    assert client.post("/monitor/run", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    client.post("/monitor/run")  # accepted, resets the window
    assert client.post("/monitor/run").status_code == 429
    assert len(started) <= 1


# ------------------------------------------------------------------- safe_url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<script>x</script>",
        "vbscript:msgbox",
        "file:///etc/passwd",
        "",
        None,
        "not a url",
    ],
)
def test_safe_url_drops_anything_that_is_not_http(url):
    assert main.safe_url(url) is None


@pytest.mark.parametrize(
    "url",
    ["https://www.metacritic.com/game/x/", "http://example.test/a?b=c", "/covers/x.jpg"],
)
def test_safe_url_keeps_ordinary_links(url):
    assert main.safe_url(url) == url


def test_a_hostile_stored_url_is_not_rendered_as_a_link(client):
    with main.SessionLocal() as session:
        game = session.scalar(select(Game).where(Game.slug == "a-game"))
        game.video_url = "javascript:alert(document.domain)"
        game.metacritic_url = "javascript:alert(1)"
        session.commit()

    body = client.get("/game/a-game").text
    assert "javascript:" not in body
    assert "Трейлер" not in body  # the button disappears rather than linking to nothing


# --------------------------------------------------------------- input limits


def test_an_overlong_search_query_is_truncated_not_rejected(client):
    response = client.get("/api/games", params={"q": "x" * 5000})
    assert response.status_code == 200
    assert response.json()["total"] == 0


def test_a_flood_of_platform_filters_is_capped(client):
    response = client.get("/api/games", params=[("platform", f"p{i}") for i in range(500)])
    assert response.status_code == 200


def test_absurd_page_numbers_do_not_break_the_query(client):
    for page in (-5, 0, 10**12):
        assert client.get("/api/games", params={"page": page}).status_code == 200


def test_sql_injection_attempts_are_just_text(client):
    for probe in ("' OR 1=1 --", "'; DROP TABLE games; --", "%' UNION SELECT 1 --"):
        assert client.get("/api/games", params={"q": probe}).json()["total"] == 0
    # The table is still there.
    assert client.get("/api/games").json()["total"] == 1


# --------------------------------------------------- POST /game/{slug}/refresh


@pytest.fixture(autouse=True)
def clean_refresh_state(monkeypatch):
    monkeypatch.setattr(main, "_last_refresh", {})
    monkeypatch.setattr(main, "_refreshing", set())


@pytest.fixture
def refresh(monkeypatch):
    done = []
    monkeypatch.setattr(
        main, "process_game", lambda session, slug: done.append(slug) or ("ok", None)
    )
    return done


def test_refresh_starts_a_background_crawl_for_one_game(client, refresh):
    response = client.post("/game/a-game/refresh", headers={"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 202
    assert response.json()["detail"] == "обновление запущено"

    for _ in range(300):
        if refresh:
            break
        __import__("time").sleep(0.01)
    assert refresh == ["a-game"]


def test_refresh_rejects_a_cross_site_post(client, refresh):
    response = client.post("/game/a-game/refresh", headers={"Sec-Fetch-Site": "cross-site"})
    assert response.status_code == 403
    assert refresh == []


def test_refresh_is_rate_limited_per_game(client, refresh):
    assert client.post("/game/a-game/refresh").status_code == 202
    for _ in range(300):
        if refresh:
            break
        __import__("time").sleep(0.01)

    second = client.post("/game/a-game/refresh")
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) > 0


def test_refresh_of_an_unknown_game_is_404(client, refresh):
    assert client.post("/game/nope/refresh").status_code == 404
    assert refresh == []


def test_refresh_reports_a_run_already_in_flight(client, monkeypatch):
    monkeypatch.setattr(main, "_refreshing", {"a-game"})
    assert client.post("/game/a-game/refresh").status_code == 409


def test_refresh_emits_a_monitor_event(client, refresh):
    from app import monitor

    monitor.reset()
    client.post("/game/a-game/refresh")
    for _ in range(300):
        if refresh:
            break
        __import__("time").sleep(0.01)

    kinds = [e["type"] for e in monitor.events()]
    assert "refresh_requested" in kinds
    assert any(e.get("slug") == "a-game" for e in monitor.events())


def test_the_status_strip_polls_only_while_busy(client, monkeypatch):
    idle = client.get("/game/a-game/status").text
    assert "hx-trigger" not in idle
    assert "обновляется" not in idle
    assert 'id="refresh-btn"' in idle

    monkeypatch.setattr(main, "_refreshing", {"a-game"})
    busy = client.get("/game/a-game/status").text
    assert 'hx-trigger="every 3s"' in busy
    assert "обновляется…" in busy
    assert "disabled" in busy


def test_the_status_strip_404s_for_an_unknown_game(client):
    assert client.get("/game/nope/status").status_code == 404


def test_the_card_shows_the_refresh_button(client):
    body = client.get("/game/a-game").text
    assert 'id="refresh-btn"' in body
    assert 'data-slug="a-game"' in body
    assert "/static/game.js" in body
