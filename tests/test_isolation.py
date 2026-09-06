"""The suite must never read or write the database the deployed service is using."""

from __future__ import annotations

import sqlite3

import pytest

from app.config import BASE_DIR, settings
from tests.conftest import _THROWAWAY_DATA_DIR, REAL_DATA_DIR


def test_settings_point_at_a_throwaway_directory():
    assert settings.data_dir == _THROWAWAY_DATA_DIR
    assert str(REAL_DATA_DIR) not in settings.database_url
    assert str(BASE_DIR / "data") not in settings.database_url


def test_the_audit_hook_blocks_the_real_database():
    # Redirecting DATA_DIR is the fix; this hook is what makes a regression loud.
    with pytest.raises(RuntimeError, match="боевую базу"):
        sqlite3.connect(str(REAL_DATA_DIR / "app.db"))


def test_the_audit_hook_blocks_it_through_a_relative_path_too():
    with pytest.raises(RuntimeError, match="боевую базу"):
        sqlite3.connect(str(BASE_DIR / "data" / ".." / "data" / "app.db"))


def test_throwaway_and_memory_databases_are_still_allowed():
    sqlite3.connect(str(_THROWAWAY_DATA_DIR / "scratch.db")).close()
    sqlite3.connect(":memory:").close()


def test_the_default_engine_is_harmless(tmp_path):
    # Even a module that forgot to be patched writes into the throwaway directory.
    from app.db import engine

    assert str(REAL_DATA_DIR) not in str(engine.url)
