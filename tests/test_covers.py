"""Cover cache and the /covers route. No network: the HTTP client is mocked."""

from __future__ import annotations

import time
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import covers, main
from app.models import Base, Game
from app.scraper.http import PoliteClient

PNG = bytes.fromhex("89504e470d0a1a0a") + b"pretend png"
JPEG = b"\xff\xd8\xff" + b"pretend jpeg"
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"pretend webp"


@pytest.fixture
def cover_dir(monkeypatch, tmp_path):
    directory = tmp_path / "covers"
    monkeypatch.setattr(covers, "covers_dir", lambda: directory)
    return directory


def fake_client(handler) -> PoliteClient:
    client = PoliteClient(min_delay=0.0, retries=1, backoff_base=0.01)
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def image_response(body=JPEG, content_type="image/jpeg", length=None):
    headers = {"content-type": content_type}
    if length is not None:
        headers["content-length"] = str(length)
    return lambda request: httpx.Response(200, content=body, headers=headers)


# --------------------------------------------------------------------- caching


def test_cover_is_downloaded_and_stored_under_the_slug(cover_dir):
    name = covers.cache_cover("nba-2k27", "https://img.test/x.jpg", fake_client(image_response()))
    assert name == "nba-2k27.jpg"
    assert (cover_dir / "nba-2k27.jpg").read_bytes() == JPEG
    assert not list(cover_dir.glob(".*.part"))  # the temp file is renamed away


def test_extension_follows_the_content_type(cover_dir):
    name = covers.cache_cover(
        "g", "https://img.test/x", fake_client(image_response(PNG, "image/png; charset=binary"))
    )
    assert name == "g.png"
    assert (cover_dir / "g.png").exists()


def test_the_bytes_win_over_a_wrong_content_type(cover_dir):
    # Metacritic really does serve some PNG covers as image/jpeg.
    name = covers.cache_cover(
        "g", "https://img.test/x", fake_client(image_response(PNG, "image/jpeg"))
    )
    assert name == "g.png"
    assert (cover_dir / "g.png").read_bytes() == PNG


def test_a_recognised_body_survives_a_useless_content_type(cover_dir):
    handler = image_response(WEBP, "application/octet-stream")
    assert covers.cache_cover("g", "https://img.test/x", fake_client(handler)) == "g.webp"


def test_changing_format_replaces_the_old_file(cover_dir):
    covers.cache_cover("g", "https://img.test/x", fake_client(image_response(JPEG)))
    assert (cover_dir / "g.jpg").exists()
    covers.cache_cover("g", "https://img.test/x", fake_client(image_response(PNG)), force=True)
    assert (cover_dir / "g.png").exists()
    assert not (cover_dir / "g.jpg").exists()


def test_a_fresh_copy_is_not_downloaded_again(cover_dir):
    hits = []

    def handler(request):
        hits.append(request.url)
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    client = fake_client(handler)
    covers.cache_cover("g", "https://img.test/x.jpg", client)
    covers.cache_cover("g", "https://img.test/x.jpg", client)
    assert len(hits) == 1

    covers.cache_cover("g", "https://img.test/x.jpg", client, force=True)
    assert len(hits) == 2


def test_a_stale_copy_is_refetched(cover_dir):
    hits = []

    def handler(request):
        hits.append(request.url)
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    client = fake_client(handler)
    covers.cache_cover("g", "https://img.test/x.jpg", client)
    stale = (time.time() - (covers.MAX_AGE + timedelta(days=1)).total_seconds())
    import os

    os.utime(cover_dir / "g.jpg", (stale, stale))
    assert covers.is_fresh("g.jpg") is False

    covers.cache_cover("g", "https://img.test/x.jpg", client)
    assert len(hits) == 2


def test_non_images_are_refused(cover_dir):
    handler = image_response(b"<html>nope</html>", "text/html")
    assert covers.cache_cover("g", "https://img.test/x", fake_client(handler)) is None
    assert not cover_dir.exists() or not list(cover_dir.iterdir())


def test_oversize_declared_length_is_refused_before_writing(cover_dir):
    handler = image_response(JPEG, "image/jpeg", length=covers.MAX_BYTES + 1)
    assert covers.cache_cover("g", "https://img.test/x", fake_client(handler)) is None
    assert not cover_dir.exists() or not list(cover_dir.iterdir())


def test_oversize_body_is_refused_even_when_the_length_lies(cover_dir):
    body = b"\xff\xd8\xff" + b"x" * (covers.MAX_BYTES + 10)
    handler = image_response(body, "image/jpeg", length=10)
    assert covers.cache_cover("g", "https://img.test/x", fake_client(handler)) is None
    assert not cover_dir.exists() or not list(cover_dir.iterdir())


@pytest.mark.parametrize("status", [403, 404, 500])
def test_a_failed_download_keeps_the_previous_copy(cover_dir, status):
    client = fake_client(image_response())
    covers.cache_cover("g", "https://img.test/x.jpg", client)

    broken = fake_client(lambda request: httpx.Response(status))
    assert covers.cache_cover("g", "https://img.test/x.jpg", broken, force=True) == "g.jpg"
    assert (cover_dir / "g.jpg").read_bytes() == JPEG


@pytest.mark.parametrize("status", [403, 500])
def test_a_failed_download_never_raises(cover_dir, status):
    # A cover is cosmetic; a 403 on one image must not abort the whole crawl.
    broken = fake_client(lambda request: httpx.Response(status))
    assert covers.cache_cover("fresh", "https://img.test/x.jpg", broken) is None


def test_legacy_signed_urls_are_rewritten_to_the_original():
    signed = (
        "https://www.metacritic.com/a/img/resize/58df2b2b6a9dc8df88e6d5f79175f18616bb4a45"
        "/catalog/provider/7/2/7-1781631535.jpg?auto=webp&fit=cover&width=226"
    )
    assert covers.unsigned_url(signed) == (
        "https://www.metacritic.com/a/img/catalog/provider/7/2/7-1781631535.jpg"
    )
    # Already-clean URLs and empty values pass through untouched.
    plain = "https://www.metacritic.com/a/img/catalog/provider/7/2/7-1.jpg"
    assert covers.unsigned_url(plain) == plain
    assert covers.unsigned_url(None) is None


def test_a_missing_url_does_not_wipe_the_cache(cover_dir):
    covers.cache_cover("g", "https://img.test/x.jpg", fake_client(image_response()))
    assert covers.cache_cover("g", None) == "g.jpg"
    assert covers.cache_cover("unknown", None) is None


# --------------------------------------------------------------- name checking


@pytest.mark.parametrize(
    "name",
    [
        "../app.db",
        "..%2Fapp.db",
        "/etc/passwd",
        "sub/dir.jpg",
        "game.txt",
        "game.jpg.exe",
        ".hidden.jpg",
        "",
        "UPPER.jpg",
    ],
)
def test_path_for_rejects_anything_that_is_not_a_cover_name(cover_dir, name):
    assert covers.path_for(name) is None


def test_path_for_requires_the_file_to_exist(cover_dir):
    assert covers.path_for("ghost.jpg") is None
    covers.cache_cover("ghost", "https://img.test/x.jpg", fake_client(image_response()))
    assert covers.path_for("ghost.jpg") == cover_dir / "ghost.jpg"


# ---------------------------------------------------------------- the web side


@pytest.fixture
def client(monkeypatch, tmp_path, cover_dir):
    engine = create_engine(f"sqlite:///{tmp_path / 'cov.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "create_scheduler", lambda: _NullScheduler())
    with factory() as session:
        session.add(Game(slug="cached", title="Cached Game", cover_path="cached.jpg",
                         cover_url="https://img.test/remote.jpg", genres=[]))
        session.add(Game(slug="remote", title="Remote Game",
                         cover_url="https://img.test/remote.jpg", genres=[]))
        session.add(Game(slug="bare", title="Bare Game", genres=[]))
        session.commit()
    covers.cache_cover("cached", "https://img.test/x.jpg", fake_client(image_response()))
    with TestClient(app=main.app) as test_client:
        yield test_client


class _NullScheduler:
    def start(self): pass
    def shutdown(self, wait=True): pass


def test_templates_prefer_the_local_copy(client):
    body = client.get("/").text
    assert 'src="/covers/cached.jpg"' in body
    # No cached file yet: fall back to the source URL rather than showing nothing.
    assert 'src="https://img.test/remote.jpg"' in body
    # No image at all: the placeholder initial, not a broken <img>.
    assert 'class="cover__empty"' in body


def test_the_game_page_uses_the_local_copy(client):
    assert 'src="/covers/cached.jpg"' in client.get("/game/cached").text


def test_covers_route_serves_the_file(client):
    response = client.get("/covers/cached.jpg")
    assert response.status_code == 200
    assert response.content == JPEG
    assert response.headers["content-type"].startswith("image/")


@pytest.mark.parametrize("name", ["../cov.db", "nope.jpg", "cached.png", "cached.jpg.bak"])
def test_covers_route_404s_on_anything_else(client, name):
    assert client.get(f"/covers/{name}").status_code == 404


def test_covers_route_404s_on_an_encoded_traversal(client):
    assert client.get("/covers/%2e%2e%2fcov.db").status_code == 404


def test_api_exposes_the_local_cover_and_the_source(client):
    payload = client.get("/api/games/cached").json()
    assert payload["cover_url"] == "/covers/cached.jpg"
    assert payload["cover_source_url"] == "https://img.test/remote.jpg"
