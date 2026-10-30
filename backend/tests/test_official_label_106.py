"""TDM#106: the compat history says whether a day's ``pnlOfficial`` is the official PnL or its fallback (explained plus
the FX basis at the CBR rates). Synthetic rows only."""

from __future__ import annotations

import pytest

from desk.api import compat


def row(pnl):
    return {"fact": {}, "pnl": pnl}


@pytest.mark.parametrize(("rows", "received"), [
    ([], False),
    ([row(None)], False),
    ([row({"carry": 10.0}), row({"carry": 5.0, "official": None})], False),
    ([row({"carry": 10.0}), row({"carry": 5.0, "official": 7.0})], True),
    ([row({"official": 0.0})], True),  # an official zero is official
])
def test_official_received(rows, received):
    assert compat.official_received(rows) is received


def test_fallback_official_is_explained_plus_basis_and_not_received():
    rows = [row({"carry": 10.0, "fx_gross": 2.0, "fx_gross_cbr": 3.0})]
    off, exp, _, _ = compat.run_totals(rows)
    assert (off, exp) == (pytest.approx(13.0), pytest.approx(12.0))
    assert compat.official_received(rows) is False


def brow(book, pnl):
    return {"fact": {"book_id": book}, "pnl": pnl}


def test_official_per_book_partial_day():
    """TDM#489: the official PnL comes per book. A day with it for a part of the books is ``partial``, not official:
    which books lack it, and its explained / official totals are over the books with it only, so the unexplained is
    never taken from the official PnL mixed with the fallback of the other books."""
    rows = [brow("A", {"carry": 10.0, "official": 12.0}), brow("A", {"carry": 4.0, "official": 3.0}),
            brow("B", {"carry": 5.0, "fx_gross": 1.0, "fx_gross_cbr": 2.0}), brow("C", {"carry": 7.0}),
            brow("D", None)]  # a book without PnL is in no count
    st = compat.official_state(rows)
    assert st["state"] == "partial" and (st["received"], st["books"]) == (1, 3) and st["missing"] == ["B", "C"]
    assert (st["pnlOfficial"], st["pnlExplained"]) == (pytest.approx(15.0), pytest.approx(14.0))
    assert st["pnlOfficial"] - st["pnlExplained"] == pytest.approx(1.0)  # the unexplained of book A only
    off, exp, _, _ = compat.run_totals(rows)  # the day's total stays the mix: official of A, fallback of B and C
    assert (off, exp) == (pytest.approx(15.0 + 7.0 + 7.0), pytest.approx(14.0 + 6.0 + 7.0))
    assert compat.official_received(rows) is False


@pytest.mark.parametrize(("rows", "state", "missing"), [
    ([brow("A", {"carry": 1.0, "official": 1.0}), brow("B", {"official": 0.0})], "all", []),
    ([brow("A", {"carry": 1.0}), brow("B", {"carry": 2.0})], "none", ["A", "B"]),
    ([brow("A", None)], "none", []),
])
def test_official_state_all_and_none(rows, state, missing):
    st = compat.official_state(rows)
    assert st["state"] == state and st["missing"] == missing
    assert compat.official_received(rows) is (state == "all")
    if state == "all":
        assert (st["pnlOfficial"], st["pnlExplained"]) == compat.run_totals(rows)[:2]
    else:
        assert st["pnlOfficial"] is None and st["pnlExplained"] is None
