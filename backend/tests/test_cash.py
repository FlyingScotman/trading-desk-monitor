"""B3 cash (issue #61): balances per book with history from multi-date ``cash_balances`` files, the ledger statuses
of ``desk.cash`` (match, break, one ledger, single) and ``GET /api/v1/cash`` behind the C15 screen. Synthetic
fixtures only (``fixtures/private/v1/datasets``)."""

from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import cash, store
from desk.api import auth, create_app
from desk.inbox import scan
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures"
DS = FIX / "private" / "v1" / "datasets"
DS_CONFIG, SNAPS = DS / "config", FIX / "mdhub_v1"
D = date.fromisoformat


def _row(book: str, ccy: str, amount: str, ledger: str | None = None, entity: str = "E1", account: str = "A",
         **kw) -> dict:
    r = {"entity": entity, "book_id": book, "ccy": ccy, "account_id": account, "balance_ccy": amount, **kw}
    if ledger:
        r["ledger"] = ledger
    return r


def test_compare_sums_accounts_per_ledger_and_gives_a_status():
    rows = [_row("B1", "RUB", "-100", "L1", account="A"), _row("B1", "RUB", "-50.5", "L1", account="B"),
            _row("B1", "RUB", "-150.5", "L2"),  # the accounts' sum agrees: match
            _row("B1", "USD", "10", "L1"), _row("B1", "USD", "12", "L2"),  # a break of 2
            _row("B2", "RUB", "7", "L1"),  # the entity has two ledgers, this balance one: one_ledger
            _row("B3", "RUB", "5", entity="E2"), _row("B3", "RUB", "1", entity="E2", account="B")]  # one system
    by = {(b.book_id, b.ccy): b for b in cash.compare(rows)}
    m = by[("B1", "RUB")]
    assert (m.status, m.known, m.diff, m.ledgers()) == ("match", Decimal("-150.5"), Decimal(0), ["L1", "L2"])
    assert [a["account_id"] for a in m.accounts] == ["A", "B", "A"]
    br = by[("B1", "USD")]
    assert (br.status, br.known, br.diff) == ("break", None, Decimal(2))
    assert (by[("B2", "RUB")].status, by[("B2", "RUB")].known) == ("one_ledger", Decimal(7))
    s = by[("B3", "RUB")]
    assert (s.status, s.known, s.ledgers()) == ("single", Decimal(6), [cash.NO_LEDGER])


def test_tolerance_is_the_controls_rule_and_a_parameter():
    big = [_row("B", "RUB", "1000000000000", "L1"), _row("B", "RUB", "1000000000005", "L2")]
    assert cash.compare(big)[0].status == "match"  # 5 <= 1e-8 x 1e12
    small = [_row("B", "RUB", "100.00", "L1"), _row("B", "RUB", "100.02", "L2")]
    assert cash.compare(small)[0].status == "break"
    assert cash.compare([_row("B", "RUB", "100.00", "L1"), _row("B", "RUB", "100.01", "L2")])[0].status == "match"
    loose = cash.Tolerance(abs_ccy=Decimal(1))
    assert cash.compare(small, loose)[0].status == "match"


@pytest.fixture(autouse=True)
def _fresh_process():
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


def _scan_cash(store_url: str, tmp_path: Path) -> None:
    inbox = tmp_path / "in"
    inbox.mkdir()
    for n in ("cash_balances_20261020.csv", "cash_balances_20261021.csv"):
        shutil.copy(DS / n, inbox / n)
    s = Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive", private_adapter=None,
                 private_adapter_config=DS_CONFIG, inbox_poll_seconds=0, inbox_lookback_days=0)
    with store.connect(store_url) as con:
        r = scan.scan(con, s)
    assert r.ok and r.files == {"accepted": 2}, r.text()


def test_history_files_load_and_compare_per_date(store_url, tmp_path):
    _scan_cash(store_url, tmp_path)
    with store.connect(store_url) as con:
        assert cash.cash_dates(con, D("2026-10-31"), 10) == [D("2026-10-21"), D("2026-10-20"), D("2026-10-19"),
                                                             D("2026-10-16")]
        on20, units = cash.balances(con, D("2026-10-20"))
        assert len(units) == 2
        st20 = {(b.book_id, b.ccy): (b.status, b.known, b.diff) for b in on20}
        assert st20[("D1", "RUB")] == ("break", None, Decimal(10000))  # S1 -52 000 000, S2 -51 990 000
        assert st20[("D1", "USD")] == ("one_ledger", Decimal(410000), None)
        assert st20[("C1", "RUB")] == ("single", Decimal(125000000), None)
        on21, _ = cash.balances(con, D("2026-10-21"))
        st21 = {(b.book_id, b.ccy): (b.status, b.known) for b in on21}
        assert st21 == {("C1", "RUB"): ("single", Decimal(126000000)), ("C1", "USD"): ("single", Decimal(1000000)),
                        ("D1", "RUB"): ("match", Decimal(-54000000)), ("D1", "USD"): ("one_ledger", Decimal(420000))}
        # a date between files: the stock is carried forward (the 17th reads the 16th's D1)
        on17, _ = cash.balances(con, D("2026-10-17"))
        assert [(b.book_id, b.ccy, b.valid_date) for b in on17] == [("D1", "RUB", D("2026-10-16")),
                                                                   ("D1", "USD", D("2026-10-16"))]

        v = cash.view(con, None, days=10)
    assert v["date"] == "2026-10-21" and v["ledgers"] == ["", "S1", "S2"]
    assert v["summary"] | {"breaks_by_ccy": None} == {"balances": 4, "match": 1, "break": 0, "one_ledger": 1,
                                                      "single": 2, "breaks_by_ccy": None, "books": 2, "units": 2}
    assert [d["date"] for d in v["days"]] == ["2026-10-21", "2026-10-20", "2026-10-19", "2026-10-16"]
    assert [d["break"] for d in v["days"]] == [0, 1, 0, 0]
    rub = next(r for r in v["rows"] if r["key"] == "ENT1|D1|RUB")
    assert rub["book_type"] == "DERIVS" and rub["by_ledger"] == {"S1": -54000000.0, "S2": -54000000.0}
    assert [a["account_id"] for a in rub["accounts"]] == ["MAIN", "MARGIN", "MAIN"]
    assert [(h["date"], h["status"], h["known"]) for h in rub["history"]] == [
        ("2026-10-16", "single", -50000000.0), ("2026-10-19", "match", -51000000.0),
        ("2026-10-20", "break", None), ("2026-10-21", "match", -54000000.0)]


def test_cash_api_and_the_c15_page(store_url, tmp_path):
    _scan_cash(store_url, tmp_path)
    c = TestClient(create_app(store_url))
    v = c.get("/api/v1/cash", params={"on": "2026-10-20"}).json()
    assert v["date"] == "2026-10-20" and v["summary"]["break"] == 1 and v["summary"]["breaks_by_ccy"] == {"RUB": 10000.0}
    br = next(r for r in v["rows"] if r["status"] == "break")
    assert (br["key"], br["known"], br["diff"], br["status_text"]) == ("ENT1|D1|RUB", None, 10000.0, "расхождение")
    assert c.get("/api/v1/cash", params={"on": "20.10.2026"}).status_code == 400
    assert c.get("/api/v1/cash", params={"days": 0}).status_code == 422
    empty = c.get("/api/v1/cash", params={"on": "2026-10-01"}).json()
    assert empty["rows"] == [] and empty["summary"]["balances"] == 0
    # sign-in on: a viewer reads it from another computer, as the day status
    with store.connect(store_url) as con:
        auth.add_user(con, "cash-viewer", "viewer", "viewer-pass-1", actor="test")
    remote = TestClient(create_app(store_url, auth=auth.AuthConfig(enabled=True)), base_url="http://10.0.0.7:8000",
                        client=("10.0.0.7", 50000))
    assert remote.get("/api/v1/cash").status_code == 401
    assert remote.post("/api/v1/auth/login", json={"username": "cash-viewer", "password": "viewer-pass-1"}
                       ).status_code == 200
    assert remote.get("/api/v1/cash").json()["date"] == "2026-10-21"
    assert remote.get("/concepts/c15-cash.html").status_code == 200
