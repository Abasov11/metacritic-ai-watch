"""Unit tests for the devalue decoder, independent of any Metacritic markup."""

import math

import pytest

from app.scraper.devalue import DevalueError, extract_nuxt_payload, parse


def test_scalar_root():
    assert parse(["hello"]) == "hello"
    assert parse([42]) == 42
    assert parse([None]) is None


def test_indices_are_resolved_and_strings_deduplicated():
    # {"a": "x", "b": "x"} — both keys point at the same string node.
    assert parse([{"a": 1, "b": 1}, "x"]) == {"a": "x", "b": "x"}


def test_nested_arrays_and_objects():
    flat = [{"items": 1}, [2, 3], {"id": 4}, {"id": 5}, 10, 20]
    assert parse(flat) == {"items": [{"id": 10}, {"id": 20}]}


def test_sentinels():
    assert parse([{"a": -2, "b": -4, "c": -5, "d": -1}]) == {
        "a": None,
        "b": math.inf,
        "c": -math.inf,
        "d": None,
    }
    assert math.isnan(parse([{"a": -3}])["a"])


def test_cycles_do_not_recurse_forever():
    # obj = {}; obj.self = obj
    result = parse([{"self": 0}])
    assert result["self"] is result


def test_tagged_types():
    assert parse([["Date", 1], "2025-09-04"]) == "2025-09-04"
    assert parse([["Set", 1, 2], "a", "b"]) == ["a", "b"]
    assert parse([["Map", 1, 2], "k", "v"]) == {"k": "v"}
    assert parse([["null", 1, 2], "k", "v"]) == {"k": "v"}


def test_nuxt_wrappers_are_transparent():
    assert parse([["ShallowReactive", 1], {"data": 2}, "x"]) == {"data": "x"}


def test_array_starting_with_a_plain_string_is_not_a_tag():
    assert parse([["North America", 1], "Europe"]) == ["North America", "Europe"]


def test_bad_input():
    with pytest.raises(DevalueError):
        parse([])
    with pytest.raises(DevalueError):
        parse([{"a": 99}])  # index out of range
    with pytest.raises(DevalueError):
        extract_nuxt_payload("<html><body>no payload</body></html>")


def test_extract_from_real_page(game_html):
    payload = extract_nuxt_payload(game_html)
    assert payload["serverRendered"] is True
    assert "loadPage:games:hollow-knight-silksong:" in payload["data"]
