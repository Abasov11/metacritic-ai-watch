"""Tag vocabulary and hybrid similarity. No network."""

from __future__ import annotations

import pytest

from app import similar
from app.llm import MAX_TAGS_PER_FIELD, TAG_VOCABULARY, clean_tags


def test_only_vocabulary_values_survive():
    tags = clean_tags(
        {
            "genres": ["rpg", "метроидвания", "battle-royale", "metroidvania"],
            "mechanics": ["parry", "телепортация сквозь стены"],
            "mood": ["dark", "DARK", "весёлый"],
            "setting": ["fantasy"],
            "perspective": "side-scrolling",
            "multiplayer": True,
        }
    )
    assert tags["genres"] == ["rpg", "metroidvania"]  # invented ones dropped, no dupes
    assert tags["mechanics"] == ["parry"]
    assert tags["mood"] == ["dark"]
    assert tags["perspective"] == "side-scrolling"
    assert tags["multiplayer"] is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Side Scrolling", "side-scrolling"),
        ("TOP_DOWN", "top-down"),
        ("  isometric  ", "isometric"),
    ],
)
def test_values_are_normalised_before_matching(raw, expected):
    assert clean_tags({"perspective": raw})["perspective"] == expected


def test_an_unknown_perspective_becomes_none():
    assert clean_tags({"perspective": "vr"})["perspective"] is None
    assert clean_tags({"perspective": 42})["perspective"] is None


def test_lists_are_capped():
    tags = clean_tags({"mechanics": list(TAG_VOCABULARY["mechanics"])})
    assert len(tags["mechanics"]) == MAX_TAGS_PER_FIELD


@pytest.mark.parametrize("raw", ["мусор", None, 42, [], ["genres"]])
def test_a_non_object_answer_yields_nothing(raw):
    assert clean_tags(raw) == {}


def test_missing_and_wrongly_typed_fields_default_to_empty():
    tags = clean_tags({"genres": "rpg", "multiplayer": "yes"})
    assert tags["genres"] == []  # a bare string is not a list
    assert tags["mood"] == []
    assert tags["multiplayer"] is True  # truthy string still means multiplayer


# ------------------------------------------------------------------- similarity


def test_tag_set_flattens_into_comparable_strings():
    flat = similar.tag_set(
        {
            "genres": ["rpg"],
            "mechanics": [],
            "mood": ["dark"],
            "setting": [],
            "perspective": "top-down",
            "multiplayer": True,
        }
    )
    assert flat == {"genres:rpg", "mood:dark", "perspective:top-down", "multiplayer:yes"}
    assert similar.tag_set(None) == set()
    assert similar.tag_set({"genres": [None, 7, ""]}) == set()


def test_jaccard_is_the_share_of_shared_tags():
    assert similar.jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert similar.jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)
    assert similar.jaccard(set(), {"a"}) == 0.0
    assert similar.jaccard({"a"}, set()) == 0.0


def test_the_weights_add_up_to_one():
    assert similar.TAG_WEIGHT + similar.TEXT_WEIGHT == pytest.approx(1.0)
    assert similar.TAG_WEIGHT > similar.TEXT_WEIGHT  # tags dominate by design


def test_identical_tags_alone_clear_the_threshold():
    # 0.6 * 1.0 with no textual overlap at all is still a confident match.
    assert similar.TAG_WEIGHT * 1.0 >= similar.MIN_SCORE


def test_a_single_shared_tag_out_of_many_does_not():
    # 0.6 * (1/9) = 0.067 — below the bar without help from the wording.
    assert similar.TAG_WEIGHT * (1 / 9) < similar.MIN_SCORE
