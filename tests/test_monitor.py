"""Monitor bus, SSE stream and the force-run button. No network, no scheduler."""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import db as app_db
from app import main, monitor
from app.models import Base, CrawlRun


@pytest.fixture(autouse=True)
def clean_bus():
    monitor.reset()
    yield
    monitor.reset()


@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mon.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", factory)
    # `_today_totals` resolves app.db.SessionLocal lazily, so patch it at the source.
    monkeypatch.setattr(app_db, "SessionLocal", factory)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "create_scheduler", lambda: _NullScheduler())
    # The manual-run rate limit is module state; keep tests independent of each other.
    monkeypatch.setattr(main, "_last_manual_run", 0.0)
    with factory() as session:
        session.add(
            CrawlRun(source="new_releases", reason="scheduled", status="ok", planned=3, processed=3)
        )
        session.commit()
    with TestClient(app=main.app) as test_client:
        yield test_client


class _NullScheduler:
    def start(self):
        pass

    def shutdown(self, wait=True):
        pass


# ---------------------------------------------------------------------- the bus


def test_emit_lands_in_the_buffer_with_a_sequence_and_time():
    event = monitor.emit(type="ping", message="привет")
    assert event["seq"] == 1 and event["type"] == "ping"
    assert event["at"].startswith("20")
    assert monitor.events() == [event]
    assert monitor.last_seq() == 1


def test_emit_updates_the_worker_state():
    monitor.emit(type="game_start", worker="crawler", status="busy", detail="fetching nba")
    crawler = monitor.snapshot(with_events=False)["workers"]["crawler"]
    assert crawler["status"] == "busy"
    assert crawler["detail"] == "fetching nba"

    monitor.emit(type="run_end", worker="crawler", status="idle")
    assert monitor.snapshot(with_events=False)["workers"]["crawler"]["status"] == "idle"


def test_unknown_worker_names_are_ignored():
    monitor.emit(type="x", worker="ghost", status="busy")
    assert set(monitor.snapshot(with_events=False)["workers"]) == set(monitor.WORKERS)


def test_youtube_worker_idles_when_the_stage_is_enabled(monkeypatch):
    monkeypatch.setattr(monitor.settings, "youtube_enabled", True)
    monitor.reset()
    youtube = monitor.snapshot(with_events=False)["workers"]["youtube"]
    assert youtube["status"] == "idle"
    assert youtube["detail"] is None


def test_youtube_worker_is_off_only_when_the_stage_is_disabled(monkeypatch):
    monkeypatch.setattr(monitor.settings, "youtube_enabled", False)
    monitor.reset()
    youtube = monitor.snapshot(with_events=False)["workers"]["youtube"]
    assert youtube["status"] == "off"
    assert youtube["detail"] == "выключено настройкой"
    # The other workers are unaffected by the setting.
    assert monitor.snapshot(with_events=False)["workers"]["crawler"]["status"] == "idle"


def test_run_lifecycle_tracks_progress():
    assert monitor.is_busy() is False
    monitor.emit(
        type="run_start",
        worker="crawler",
        status="busy",
        run_id=7,
        source="browse:2",
        reason="manual",
    )
    monitor.emit(type="run_planned", worker="crawler", status="busy", planned=3)
    monitor.emit(type="game_done", worker="crawler", status="busy", slug="a")
    monitor.emit(type="game_failed", worker="crawler", status="busy", slug="b")

    run = monitor.snapshot(with_events=False)["run"]
    assert (run["id"], run["source"], run["reason"]) == (7, "browse:2", "manual")
    assert (run["planned"], run["processed"], run["failed"]) == (3, 1, 1)
    assert monitor.is_busy() is True

    monitor.emit(type="run_end", worker="crawler", status="idle", run_id=7)
    assert monitor.snapshot(with_events=False)["run"] is None
    assert monitor.is_busy() is False


def test_progress_events_without_a_run_do_not_crash():
    monitor.emit(type="game_done", worker="crawler", status="busy")
    assert monitor.snapshot(with_events=False)["run"] is None


def test_since_returns_only_newer_events():
    monitor.emit(type="a")
    mark = monitor.last_seq()
    monitor.emit(type="b")
    monitor.emit(type="c")
    assert [e["type"] for e in monitor.since(mark)] == ["b", "c"]
    assert monitor.since(monitor.last_seq()) == []


def test_the_buffer_is_capped():
    for i in range(monitor.MAX_EVENTS + 25):
        monitor.emit(type="x", n=i)
    kept = monitor.events(limit=monitor.MAX_EVENTS + 50)
    assert len(kept) == monitor.MAX_EVENTS
    assert kept[-1]["n"] == monitor.MAX_EVENTS + 24  # newest survived


def test_today_totals_come_from_the_database(client):
    today = client.get("/healthz").json()["today"]
    assert today["runs"] == 1 and today["games"] == 3


# -------------------------------------------------------------------- the page


def test_monitor_page_renders_workers_runs_and_log(client):
    monitor.emit(
        type="run_start",
        worker="crawler",
        status="busy",
        run_id=1,
        source="new_releases",
        reason="manual",
        message="обход #1 начат",
    )
    body = client.get("/monitor").text
    assert "Мониторинг" in body
    for name in monitor.WORKERS:
        assert f'data-worker="{name}"' in body
    assert "обход #1 начат" in body  # live log seeded from the snapshot
    assert "new_releases" in body  # recent runs table
    assert "Запустить обход сейчас" in body


def test_every_page_links_to_the_monitor(client):
    assert 'href="/monitor"' in client.get("/").text


def test_healthz_reports_workers_and_the_lock(client):
    payload = client.get("/healthz").json()
    assert payload["status"] == "ok"
    assert set(payload["workers"]) == set(monitor.WORKERS)
    assert payload["crawl_running"] is False


# ------------------------------------------------------------------ the button


def test_force_run_starts_a_crawl_in_the_background(client, monkeypatch):
    started = []
    monkeypatch.setattr(main, "run_crawl", lambda reason: started.append(reason))

    response = client.post("/monitor/run")
    assert response.status_code == 202
    assert response.json()["detail"] == "обход запущен"

    for _ in range(200):  # the crawl runs on its own thread
        if started:
            break
        __import__("time").sleep(0.01)
    assert started == ["manual"]
    assert any(e["type"] == "run_requested" for e in monitor.events())


def test_force_run_is_refused_while_a_crawl_is_running(client, monkeypatch):
    called = []
    monkeypatch.setattr(main, "is_running", lambda: True)
    monkeypatch.setattr(main, "run_crawl", lambda reason: called.append(reason))

    response = client.post("/monitor/run")
    assert response.status_code == 409
    assert response.json()["detail"] == "обход уже идёт"
    assert called == []


def test_the_button_starts_disabled_while_a_crawl_runs(client, monkeypatch):
    monkeypatch.setattr(main, "is_running", lambda: True)
    assert "disabled" in client.get("/monitor").text


# ------------------------------------------------------------------ the stream


def read_sse(client, url="/monitor/stream", max_lines=200):
    """Read every SSE frame until the server closes the (time-bounded) stream."""
    collected, event = [], None
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        for i, line in enumerate(response.iter_lines()):
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                collected.append((event, json.loads(line.split(":", 1)[1])))
            if i > max_lines:  # never hang the suite on a broken stream
                break
    return collected


def test_stream_opens_with_a_state_snapshot(client, monkeypatch):
    # The stream is time-bounded so the test never blocks; shorten it to a blink.
    monkeypatch.setattr(main, "SSE_MAX_SECONDS", 1.0)
    monitor.emit(
        type="run_start",
        worker="crawler",
        status="busy",
        run_id=3,
        source="browse:1",
        reason="manual",
    )
    frames = read_sse(client)

    event, payload = frames[0]
    assert event == "state"
    assert payload["run"]["id"] == 3
    assert payload["workers"]["crawler"]["status"] == "busy"
    assert payload["today"]["runs"] == 1
    assert any(e["type"] == "run_start" for e in payload["events"])
    assert payload["seq"] == monitor.last_seq()


def test_stream_pushes_events_emitted_after_it_opened(client, monkeypatch):
    monkeypatch.setattr(main, "SSE_MAX_SECONDS", 2.0)

    def emit_soon():
        __import__("time").sleep(0.4)
        monitor.emit(
            type="game_done",
            worker="crawler",
            status="busy",
            slug="late-game",
            message="late-game: ok",
        )

    threading.Thread(target=emit_soon, daemon=True).start()
    frames = read_sse(client)

    logs = [payload for name, payload in frames if name == "log"]
    assert any(e["slug"] == "late-game" for e in logs)
    # A state frame follows the log frame, so the dashboard counters stay in step.
    assert [name for name, _ in frames][-1] == "state"


def test_skipped_runs_do_not_count_towards_today(client):
    with main.SessionLocal() as session:
        session.add(
            CrawlRun(
                source="-",
                reason="scheduled",
                status="skipped",
                error="another crawl is already running",
            )
        )
        session.commit()
    assert client.get("/healthz").json()["today"]["runs"] == 1


def test_a_run_left_running_by_a_dead_process_is_closed_at_startup(monkeypatch, tmp_path):
    from sqlalchemy import create_engine as _create_engine

    engine = _create_engine(f"sqlite:///{tmp_path / 'orphan.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", factory)
    with factory() as session:
        session.add(
            CrawlRun(
                source="browse:3", reason="scheduled", status="running", planned=20, processed=4
            )
        )
        session.commit()

    assert main.close_orphaned_runs() == 1
    with factory() as session:
        run = session.scalar(select(CrawlRun))
        assert run.status == "aborted"
        assert run.finished_at is not None
        assert "процесс завершился" in run.error
    # Nothing left to close on the next start.
    assert main.close_orphaned_runs() == 0


def test_aborted_runs_do_not_count_as_running_anywhere(client):
    with main.SessionLocal() as session:
        session.add(
            CrawlRun(
                source="browse:9", reason="scheduled", status="running", planned=20, processed=4
            )
        )
        session.commit()
    main.close_orphaned_runs()

    # The dashboard shows it with its own pill, and it is not "in progress".
    body = client.get("/monitor").text
    assert "pill--aborted" in body
    assert "browse:9" in body
    assert client.get("/healthz").json()["crawl_running"] is False
    assert monitor.snapshot(with_events=False)["run"] is None


def test_the_scheduler_can_be_switched_off(monkeypatch, tmp_path):
    started = []

    class _SpyScheduler:
        def start(self):
            started.append("start")

        def shutdown(self, wait=True):
            started.append("shutdown")

    engine = create_engine(f"sqlite:///{tmp_path / 'sched.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(main, "SessionLocal", factory)
    monkeypatch.setattr(main, "init_db", lambda: None)
    monkeypatch.setattr(main, "create_scheduler", lambda: _SpyScheduler())

    monkeypatch.setattr(main.settings, "scheduler_enabled", False)
    with TestClient(app=main.app) as client:
        assert client.get("/healthz").status_code == 200
    assert started == []  # never created, never started
    assert any("планировщик выключен" in (e.get("message") or "") for e in monitor.events())

    monkeypatch.setattr(main.settings, "scheduler_enabled", True)
    with TestClient(app=main.app):
        pass
    assert started == ["start", "shutdown"]
