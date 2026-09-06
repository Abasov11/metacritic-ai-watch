"""Fixture loading. Nothing in the test suite touches the network."""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def front_door_html() -> str:
    return load_html("games_front_door.html")


@pytest.fixture(scope="session")
def browse_html() -> str:
    return load_html("browse_new_page2.html")


@pytest.fixture(scope="session")
def game_html() -> str:
    return load_html("game_hollow-knight-silksong.html")


@pytest.fixture(scope="session")
def critic_reviews_html() -> str:
    return load_html("critic_reviews_hollow-knight-silksong.html")


@pytest.fixture(scope="session")
def user_reviews_html() -> str:
    return load_html("user_reviews_hollow-knight-silksong.html")
