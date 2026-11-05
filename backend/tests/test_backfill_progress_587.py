"""TDM#587: the counter of the history of runs grows from the first date computed (cumulative: the dates already
current plus those computed now, «посчитано X из M, сейчас ДД.ММ»), «Осталось» says why by reason, the last finished
backfill step is kept between the cycles and the status route returns it, and the inputs' stamp covers the books
reference. On the market of test_r23_backfill (synthetic data only)."""

from __future__ import annotations

from datetime import date

from test_r23_backfill import DATES, cache, load, settings  # noqa: F401 - the module's fixtures

from desk import backfill, store
from desk.autopilot import Autopilot, progress_text


def test_progress_text_of_a_date_is_cumulative():
    p = {"step": "backfill", "done": 3, "total": 10, "detail": "2026-10-13"}
    assert progress_text(p) == "История прогонов: посчитано 3 из 10, сейчас 13.10.2026"
    assert progress_text({**p, "detail": "отчёты", "total": None, "done": 2}) == "История прогонов отчёты"


def test_reason_keys():
    assert [backfill.reason_key(w) for w in ("program updated", "no default run", "positions changed",
                                              "ocp report changed after the run", "private inputs changed", None)] \
        == ["program", "no_run", "positions", "late", "private_inputs", "other"]


def test_the_run_counts_from_the_dates_already_current(tmp_path, cache, store_url):  # noqa: F811
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    with store.connect(store_url) as con:
        p = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))
    first = p.todo[0]
    seen: list[tuple[int, int, str]] = []
    # one date first, so that the next run starts with one current date
    backfill.run(s, backfill.Plan(p.date_from, p.date_to, [first], p.hub), store_url=store_url, out=lambda x: None)
    with store.connect(store_url) as con:
        p = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))
    assert p.count("done") == 1 and len(p.days) == len(DATES)
    s2 = p.summary()
    assert s2["todo_by_reason"] == [{"key": "no_run", "text": "ещё не считалась", "n": 3}]
    backfill.run(s, p, store_url=store_url, out=lambda x: None, progress=lambda *a: seen.append(a))
    assert seen == [(1, 4, "2026-10-13"), (2, 4, "2026-10-14"), (3, 4, "2026-10-15")]


def test_the_last_backfill_step_is_kept_between_the_cycles(tmp_path, cache, store_url, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    from desk.api import create_app

    load(store_url, tmp_path, "2026-10-14", 10)
    s = settings(tmp_path, cache, store_url, backfill_from="2026-10-01")
    ap = Autopilot(settings_loader=lambda: s, store_url=store_url, today=lambda: date(2026, 11, 30))
    assert ap.last_backfill() is None
    st = ap.run_cycle(hub=False)
    step = next(x for x in st["steps"] if x["name"] == "backfill")
    assert ap.last_backfill() == step
    ap._update(steps=[])  # the next cycle's start clears the steps
    for k, v in {"DESK_ENV_FILE": str(tmp_path / "none.env"), "DESK_STORE_URL": store_url,
                 "DESK_HOME": str(tmp_path / "home"), "DESK_SNAPSHOT_CACHE": str(cache),
                 "DESK_BACKFILL_FROM": "2026-10-01"}.items():
        monkeypatch.setenv(k, v)
    app = create_app(store_url)
    app.state.autopilot = ap
    doc = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)).get(
        "/api/v1/backfill/status").json()
    assert doc["step"] == step and doc["last_step"] == step and doc["current"] is None
    ap._update(progress={"step": "backfill", "done": 2, "total": 5, "detail": "2026-10-13"})
    doc = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)).get(
        "/api/v1/backfill/status").json()
    assert doc["current"] == {"date": "2026-10-13", "done": 2, "total": 5}


def test_the_stamp_covers_the_books_reference(tmp_path, cache, store_url):  # noqa: F811
    s = settings(tmp_path, cache, store_url)
    with store.connect(store_url) as con:
        before = backfill.inputs_stamp(con, s)
        store.store_books(con, [{"book_id": "B1", "name": "Book", "desk": "D", "book_type": "TRADING", "active": True,
                                 "entity": "E1"}], "fp")
        after = backfill.inputs_stamp(con, s)
    assert before != after
