"""OpenRouter client tests. No network: `httpx.post` is replaced."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import llm
from app.models import Base, LlmCall
from tests.conftest import bind_session


@pytest.fixture(autouse=True)
def db(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'llm.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    bind_session(monkeypatch, factory)
    monkeypatch.setattr(llm.settings, "openrouter_api_key", "test-key")
    monkeypatch.setattr(llm.settings, "openrouter_model", "primary/model")
    monkeypatch.setattr(llm.settings, "openrouter_fallback_model", "fallback/model")
    return factory


def ok_reply(content: str, model: str, cost: float = 0.001) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20, "cost": cost},
        },
        request=httpx.Request("POST", "https://openrouter.ai/"),
    )


def install_post(monkeypatch, handler) -> list[str]:
    """Replace `httpx.post`; returns the list of models the client asked for."""
    asked: list[str] = []

    def post(url, *, json, headers, timeout):
        asked.append(json["model"])
        return handler(json["model"], len(asked))

    monkeypatch.setattr(llm.httpx, "post", post)
    return asked


def test_happy_path_records_cost(monkeypatch, db):
    install_post(monkeypatch, lambda model, _n: ok_reply('{"a": 1}', model))

    assert llm.chat_json("hi", "{}", purpose="test", game_id=7)["a"] == 1

    with db() as session:
        call = session.scalar(select(LlmCall))
        assert (call.model, call.purpose, call.game_id) == ("primary/model", "test", 7)
        assert (call.prompt_tokens, call.completion_tokens, call.cost) == (10, 20, 0.001)
        assert call.ok is True and call.ms is not None


def test_json_survives_markdown_fences_and_prose(monkeypatch, db):
    install_post(
        monkeypatch,
        lambda model, _n: ok_reply('Вот ответ:\n```json\n{"likes": ["x"]}\n```', model),
    )
    assert llm.chat_json("hi", "{}")["likes"] == ["x"]


def test_retries_the_primary_model_twice_before_the_fallback(monkeypatch, db):
    def handler(model, _n):
        if model == "primary/model":
            raise httpx.ConnectError("down", request=httpx.Request("POST", "https://x/"))
        return ok_reply('{"a": 2}', model)

    asked = install_post(monkeypatch, handler)
    result = llm.chat_json("hi", "{}", purpose="test")

    assert result["a"] == 2
    assert result["_model"] == "fallback/model"
    assert asked == ["primary/model", "primary/model", "fallback/model"]

    with db() as session:
        calls = session.scalars(select(LlmCall).order_by(LlmCall.id)).all()
        assert [(c.model, c.ok) for c in calls] == [
            ("primary/model", False),
            ("primary/model", False),
            ("fallback/model", True),
        ]
        assert "down" in calls[0].error


def test_unparseable_reply_falls_back_too(monkeypatch, db):
    def handler(model, _n):
        return ok_reply("не json вообще" if model == "primary/model" else '{"a": 3}', model)

    asked = install_post(monkeypatch, handler)
    assert llm.chat_json("hi", "{}")["a"] == 3
    assert asked.count("primary/model") == 2


def test_gives_up_when_every_model_fails(monkeypatch, db):
    install_post(
        monkeypatch,
        lambda model, _n: httpx.Response(
            500, request=httpx.Request("POST", "https://openrouter.ai/")
        ),
    )
    with pytest.raises(llm.LLMError):
        llm.chat_json("hi", "{}")

    with db() as session:
        assert len(session.scalars(select(LlmCall)).all()) == 4  # 2 models x 2 attempts


def test_missing_api_key_is_refused_before_any_request(monkeypatch, db):
    monkeypatch.setattr(llm.settings, "openrouter_api_key", "")
    asked = install_post(monkeypatch, lambda model, _n: ok_reply("{}", model))
    with pytest.raises(llm.LLMError, match="OPENROUTER_API_KEY"):
        llm.chat_json("hi", "{}")
    assert asked == []


def test_summary_prompt_is_capped(monkeypatch, db):
    captured = {}

    def post(url, *, json, headers, timeout):
        captured["prompt"] = json["messages"][1]["content"]
        captured["headers"] = headers
        return ok_reply('{"likes": ["a"], "dislikes": [], "summary": "s"}', json["model"])

    monkeypatch.setattr(llm.httpx, "post", post)
    reviews = [("author", 9.0, "x" * 5000)] * 100
    result = llm.summarize_reviews("Some Game", "user", reviews, 1)

    assert result == {"likes": ["a"], "dislikes": [], "summary": "s", "model": "primary/model"}
    assert captured["prompt"].count("[author, оценка 9.0]") == llm.MAX_REVIEWS
    assert "x" * (llm.MAX_REVIEW_CHARS + 1) not in captured["prompt"]
    assert captured["headers"]["X-Title"] and captured["headers"]["HTTP-Referer"]
    assert captured["headers"]["Authorization"] == "Bearer test-key"


# ---------------------------------------------------------------- daily budget


def test_a_call_is_refused_once_the_day_is_spent(monkeypatch, db):
    monkeypatch.setattr(llm.settings, "llm_daily_budget_usd", 1.0)
    monkeypatch.setattr(llm, "spent_today", lambda session_maker=None: 1.5)
    asked = install_post(monkeypatch, lambda model, _n: ok_reply("{}", model))

    with pytest.raises(llm.BudgetExhausted, match="бюджет"):
        llm.chat_json("hi", "{}", purpose="summary:critic")
    assert asked == []  # the model is never contacted


def test_the_refusal_is_announced_in_the_monitor(monkeypatch, db):
    from app import monitor

    monitor.reset()
    monkeypatch.setattr(llm.settings, "llm_daily_budget_usd", 1.0)
    monkeypatch.setattr(llm, "spent_today", lambda session_maker=None: 2.0)
    install_post(monkeypatch, lambda model, _n: ok_reply("{}", model))

    with pytest.raises(llm.BudgetExhausted):
        llm.chat_json("hi", "{}")
    kinds = [e["type"] for e in monitor.events()]
    assert "budget_exhausted" in kinds


def test_spending_under_the_cap_goes_through(monkeypatch, db):
    monkeypatch.setattr(llm.settings, "llm_daily_budget_usd", 1.0)
    monkeypatch.setattr(llm, "spent_today", lambda session_maker=None: 0.4)
    install_post(monkeypatch, lambda model, _n: ok_reply('{"a": 1}', model))
    assert llm.chat_json("hi", "{}")["a"] == 1


def test_a_zero_limit_means_no_limit(monkeypatch, db):
    monkeypatch.setattr(llm.settings, "llm_daily_budget_usd", 0.0)
    monkeypatch.setattr(llm, "spent_today", lambda session_maker=None: 99.0)
    install_post(monkeypatch, lambda model, _n: ok_reply('{"a": 1}', model))
    assert llm.chat_json("hi", "{}")["a"] == 1


def test_budget_state_reports_what_is_left(monkeypatch, db):
    monkeypatch.setattr(llm.settings, "llm_daily_budget_usd", 2.0)
    monkeypatch.setattr(llm, "spent_today", lambda session_maker=None: 0.5)
    assert llm.budget_state() == {"spent": 0.5, "limit": 2.0, "left": 1.5, "exhausted": False}
    monkeypatch.setattr(llm, "spent_today", lambda session_maker=None: 2.5)
    state = llm.budget_state()
    assert state["exhausted"] is True and state["left"] == 0.0


def test_a_broken_accounting_query_never_blocks_a_crawl(monkeypatch, db):
    def boom():
        raise RuntimeError("нет базы")

    monkeypatch.setattr(llm, "SessionLocal", boom)
    assert llm.spent_today() == 0.0
