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
