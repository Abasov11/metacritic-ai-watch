"""Summary evaluation. No network: the judge is stubbed everywhere."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import eval as evaluation
from app.models import Base, Game, Review, Summary, text_hash


def claims(*pairs) -> list[tuple[str, str]]:
    return list(pairs)


# ------------------------------------------------------------ parsing the verdicts


def test_verdicts_line_up_with_the_claims():
    payload = {
        "verdicts": [
            {"index": 1, "supported": True, "evidence": "цитата", "note": "есть"},
            {"index": 2, "supported": False, "evidence": "", "note": "нет"},
        ]
    }
    parsed = evaluation.parse_verdicts(payload, claims(("likes", "a"), ("dislikes", "b")))
    assert [v["supported"] for v in parsed] == [True, False]
    assert parsed[0]["evidence"] == "цитата"
    assert parsed[1]["note"] == "нет"


def test_a_numeric_string_index_is_accepted():
    payload = {"verdicts": [{"index": "2", "supported": True}, {"index": "1", "supported": False}]}
    parsed = evaluation.parse_verdicts(payload, claims(("likes", "a"), ("likes", "b")))
    assert [v["supported"] for v in parsed] == [False, True]


def test_a_shuffled_index_still_finds_its_claim():
    payload = {"verdicts": [{"index": 2, "supported": True}, {"index": 1, "supported": False}]}
    parsed = evaluation.parse_verdicts(payload, claims(("likes", "a"), ("likes", "b")))
    assert [v["supported"] for v in parsed] == [False, True]


def test_a_missing_verdict_counts_as_unsupported():
    payload = {"verdicts": [{"index": 1, "supported": True}]}
    parsed = evaluation.parse_verdicts(payload, claims(("likes", "a"), ("likes", "b")))
    assert len(parsed) == 2
    assert parsed[1]["supported"] is False
    assert "не вернул" in parsed[1]["note"]


def test_extra_verdicts_are_ignored():
    payload = {"verdicts": [{"index": i, "supported": True} for i in range(1, 6)]}
    assert len(evaluation.parse_verdicts(payload, claims(("likes", "a")))) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"verdicts": None},
        {"verdicts": "not a list"},
        {"verdicts": ["not a dict", 42]},
        {"wrong_key": [{"index": 1, "supported": True}]},
    ],
)
def test_a_malformed_answer_never_raises(payload):
    parsed = evaluation.parse_verdicts(payload, claims(("likes", "a"), ("likes", "b")))
    assert len(parsed) == 2
    assert all(v["supported"] is False for v in parsed)


@pytest.mark.parametrize("index", [None, "не число", 3.7, True, [], {}])
def test_an_index_that_is_not_a_number_falls_back_to_position(index):
    payload = {"verdicts": [{"index": index, "supported": True, "evidence": "e"}]}
    parsed = evaluation.parse_verdicts(payload, claims(("likes", "a")))
    assert parsed[0]["supported"] is True


def test_evidence_is_capped():
    payload = {"verdicts": [{"index": 1, "supported": True, "evidence": "я" * 900}]}
    parsed = evaluation.parse_verdicts(payload, claims(("likes", "a")))
    assert len(parsed[0]["evidence"]) == evaluation.MAX_EVIDENCE_CHARS


# ------------------------------------------------------------------------ metrics


def verdict(kind="critic", sign="likes", supported=True, slug="g") -> evaluation.Verdict:
    return evaluation.Verdict(
        slug=slug,
        title="G",
        kind=kind,
        sign=sign,
        claim="c",
        supported=supported,
        evidence="",
        note="",
    )


def test_metrics_split_by_kind_and_sign():
    stats = evaluation.metrics(
        [
            verdict("critic", "likes", True),
            verdict("critic", "dislikes", False),
            verdict("user", "likes", True),
            verdict("user", "dislikes", True),
        ]
    )
    assert stats["total"] == (3, 4, 75.0)
    assert stats["critic"] == (1, 2, 50.0)
    assert stats["user"] == (2, 2, 100.0)
    assert stats["likes"] == (2, 2, 100.0)
    assert stats["dislikes"] == (1, 2, 50.0)


def test_metrics_count_games_and_summaries():
    stats = evaluation.metrics(
        [
            verdict(slug="a", kind="critic"),
            verdict(slug="a", kind="user"),
            verdict(slug="b", kind="critic"),
        ]
    )
    assert stats["games"] == 2
    assert stats["summaries"] == 3


def test_metrics_on_an_empty_slice_report_no_percentage():
    stats = evaluation.metrics([verdict("critic")])
    assert stats["user"] == (0, 0, None)


def test_nearest_review_picks_the_closest_by_words():
    reviews = ["Совершенно про другое, погода и коты", "Боевая система с парированием хороша"]
    # Word forms differ throughout, so matching has to survive Russian inflection.
    assert "парированием" in evaluation.nearest_review("парирование в боевой системе", reviews)
    assert evaluation.nearest_review("парирование", []) == ""


def test_nearest_review_says_nothing_when_nothing_is_close():
    assert evaluation.nearest_review("парирование", ["погода и коты"]) == ""


# ------------------------------------------------------------------- the whole run


@pytest.fixture
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'eval.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        game = Game(slug="silksong", title="Silksong", genres=[])
        session.add(game)
        session.flush()
        session.add(
            Summary(
                game_id=game.id,
                kind="critic",
                likes=["отличные бои", "красивая музыка"],
                dislikes=["слишком сложно"],
                summary="итог",
                model="author/model",
            )
        )
        # Never written by a model: must be skipped.
        session.add(
            Summary(
                game_id=game.id,
                kind="user",
                likes=[],
                dislikes=[],
                summary="Отзывов пока нет.",
                model=None,
            )
        )
        for i, text in enumerate(["бои отличные", "музыка красивая", "сложность высокая"]):
            session.add(
                Review(
                    game_id=game.id,
                    kind="critic",
                    text=text,
                    text_hash=text_hash(text) + str(i),
                    author="A",
                )
            )
        session.commit()
    return factory


def test_collect_judges_every_claim(db, monkeypatch):
    seen = {}

    def judge(title, kind, claim_list, reviews, game_id):
        seen["claims"] = claim_list
        seen["reviews"] = reviews
        return [
            {"supported": True, "evidence": "бои отличные", "note": "есть"},
            {"supported": True, "evidence": "музыка красивая", "note": "есть"},
            {"supported": False, "evidence": "", "note": "никто не жалуется"},
        ]

    monkeypatch.setattr(evaluation, "judge_summary", judge)
    with db() as session:
        verdicts = evaluation.collect(session)

    assert len(verdicts) == 3  # the model-less summary contributed nothing
    assert [v.sign for v in verdicts] == ["likes", "likes", "dislikes"]
    assert seen["reviews"] == ["бои отличные", "музыка красивая", "сложность высокая"]
    unsupported = [v for v in verdicts if not v.supported]
    assert unsupported[0].claim == "слишком сложно"
    assert unsupported[0].nearest  # the closest review is attached for the report


def test_a_judge_failure_marks_claims_unsupported_and_keeps_going(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("судья лёг")

    monkeypatch.setattr(evaluation, "judge_summary", boom)
    with db() as session:
        verdicts = evaluation.collect(session)
    assert len(verdicts) == 3
    assert all(not v.supported for v in verdicts)
    assert "судья лёг" in verdicts[0].note


def test_the_kind_filter_and_limit_are_honoured(db, monkeypatch):
    monkeypatch.setattr(
        evaluation,
        "judge_summary",
        lambda *a, **k: [{"supported": True, "evidence": "", "note": ""}] * 3,
    )
    with db() as session:
        assert evaluation.collect(session, kind="user") == []
        assert len(evaluation.collect(session, limit=1)) == 3


def test_summaries_whose_reviews_are_gone_are_skipped(db, monkeypatch):
    monkeypatch.setattr(evaluation, "judge_summary", lambda *a, **k: [])
    with db() as session:
        for review in session.scalars(select(Review)).all():
            session.delete(review)
        session.commit()
        assert evaluation.collect(session) == []


# ------------------------------------------------------------------------- report


def test_the_report_lists_every_unsupported_claim():
    verdicts = [
        evaluation.Verdict("g", "G", "critic", "likes", "хорошие бои", True, "бои!", "ok"),
        evaluation.Verdict(
            "g",
            "G",
            "critic",
            "dislikes",
            "плохая камера",
            False,
            "",
            "нет",
            nearest="камера иногда мешает",
        ),
        evaluation.Verdict("g", "G", "user", "likes", "музыка", False, "", "нет"),
    ]
    report = evaluation.render_report(verdicts, evaluation.metrics(verdicts), 0.0012, 2)

    assert "Неподтверждённые утверждения — все 2" in report
    assert "плохая камера" in report and "музыка" in report
    assert "камера иногда мешает" in report  # the nearest review is quoted
    assert "«бои!»" in report  # a supported example with its quote
    assert "$0.001200" in report
    assert "## Методика" in report and "Ограничения" in report


def test_the_report_is_honest_when_everything_passed():
    verdicts = [evaluation.Verdict("g", "G", "critic", "likes", "бои", True, "бои!", "ok")]
    report = evaluation.render_report(verdicts, evaluation.metrics(verdicts), 0.0, 1)
    assert "Судья подтвердил каждое утверждение." in report


def test_the_prompt_carries_claims_and_reviews():
    prompt = evaluation.build_prompt(
        "Silksong", "user", claims(("likes", "бои"), ("dislikes", "камера")), ["отзыв раз"]
    )
    assert "1. [плюс] бои" in prompt
    assert "2. [минус] камера" in prompt
    assert "[Отзыв 1] отзыв раз" in prompt
    assert "игроков" in prompt


# ------------------------------------------------------------- the judge control


def test_the_control_feeds_the_judge_fabrications(db, monkeypatch):
    seen = {}

    def judge(title, kind, claim_list, reviews, game_id):
        seen["claims"] = claim_list
        return [{"supported": False, "evidence": "", "note": "нет такого"}] * len(claim_list)

    monkeypatch.setattr(evaluation, "judge_summary", judge)
    with db() as session:
        control = evaluation.control_check(session)

    assert seen["claims"] == evaluation.CONTROL_CLAIMS
    assert len(control) == len(evaluation.CONTROL_CLAIMS)
    assert all(not v.supported for v in control)


def test_a_control_failure_is_shouted_about_in_the_report():
    verdicts = [evaluation.Verdict("g", "G", "critic", "likes", "бои", True, "бои!", "ok")]
    control = [
        evaluation.Verdict("g", "G", "critic", "likes", "выдумка", False, "", "нет"),
        evaluation.Verdict("g", "G", "critic", "likes", "вторая выдумка", True, "", "да"),
    ]
    report = evaluation.render_report(verdicts, evaluation.metrics(verdicts), 0.0, 1, control)
    assert "Отвергнуто 1 из 2 выдумок." in report
    assert "судье нет доверия" in report
    assert "завышены" in report


def test_a_clean_control_reads_as_clean():
    verdicts = [evaluation.Verdict("g", "G", "critic", "likes", "бои", True, "бои!", "ok")]
    control = [evaluation.Verdict("g", "G", "critic", "likes", "выдумка", False, "", "нет")]
    report = evaluation.render_report(verdicts, evaluation.metrics(verdicts), 0.0, 1, control)
    assert "Отвергнуто 1 из 1 выдумок." in report
    assert "судье нет доверия" not in report
    assert "завышены" not in report


def test_a_broken_control_does_not_stop_the_run(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("судья лёг")

    monkeypatch.setattr(evaluation, "judge_summary", boom)
    with db() as session:
        assert evaluation.control_check(session) == []


def test_the_report_admits_when_the_control_was_skipped():
    verdicts = [evaluation.Verdict("g", "G", "critic", "likes", "бои", True, "бои!", "ok")]
    report = evaluation.render_report(verdicts, evaluation.metrics(verdicts), 0.0, 1, [])
    assert "Контроль в этом прогоне не выполнялся." in report
