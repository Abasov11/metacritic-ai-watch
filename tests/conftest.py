"""Fixture loading. Nothing in the test suite touches the network or the real database.

The `DATA_DIR` override below runs at import time, before pytest collects any test
module and therefore before `app.config` is read. Every module that reaches for the
default engine — directly or through a call site a fixture forgot to patch — then lands
in a throwaway directory instead of `data/app.db`, which the deployed service is using.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Must happen before the first `import app.*` anywhere in the suite.
_THROWAWAY_DATA_DIR = Path(tempfile.mkdtemp(prefix="metacritic-ai-watch-tests-"))
os.environ["DATA_DIR"] = str(_THROWAWAY_DATA_DIR)
os.environ.pop("DATABASE_URL", None)  # would otherwise win over DATA_DIR

#: The deployed service reads and writes this file; a test must never reach it.
REAL_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _forbid_the_real_database(event: str, args: tuple) -> None:
    """Turn an accidental connection to `data/` into an immediate, obvious failure.

    Redirecting DATA_DIR is enough in practice, but only an audit hook makes it
    impossible — including for any code that builds a path without going through
    settings.
    """
    if event != "sqlite3.connect" or not args:
        return
    database = args[0]
    if not isinstance(database, (str, bytes, os.PathLike)):
        return
    path = os.fsdecode(database)
    if path == ":memory:":
        return
    if str(REAL_DATA_DIR) in str(Path(path).resolve().parent):
        raise RuntimeError(
            f"тест пытается открыть боевую базу {path}; используйте временную базу и bind_session()"
        )


sys.addaudithook(_forbid_the_real_database)

FIXTURES = Path(__file__).parent / "fixtures"

#: Modules that bound `SessionLocal` at import time. A fixture that swaps only some of
#: them leaves the rest pointing at the default engine, which is exactly how five tests
#: ended up reading the production database until CI ran on a machine without it.
_SESSION_HOLDERS = ("app.db", "app.main", "app.crawler", "app.similar", "app.llm", "app.eval")


def bind_session(monkeypatch, factory) -> None:
    """Point every module at one session factory. Use this instead of hand-picking."""
    import importlib

    for name in _SESSION_HOLDERS:
        monkeypatch.setattr(importlib.import_module(name), "SessionLocal", factory)


@pytest.fixture(autouse=True)
def _never_touch_the_real_database():
    """Fail loudly if a test ever opens the deployed database file."""
    from app.config import BASE_DIR, settings

    assert settings.data_dir == _THROWAWAY_DATA_DIR, (
        f"tests must not use {settings.data_dir}; the DATA_DIR override did not apply"
    )
    assert str(BASE_DIR / "data") not in settings.database_url
    yield


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


def pytest_sessionfinish(session, exitstatus) -> None:
    """Remove the throwaway database directory so runs do not litter /tmp."""
    import shutil

    shutil.rmtree(_THROWAWAY_DATA_DIR, ignore_errors=True)
