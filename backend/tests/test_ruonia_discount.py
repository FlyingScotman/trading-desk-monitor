"""RUB bonds discounted on RUONIA OIS (the owner's decision of 20.10) on a cut of another day, intraday (R11) as the
EOD: the cut's OIS quotes of its own day give the RUONIA curve; without them the run goes on, on the KBD, flagged.
The cut is the 2026-10-15 fixture moved to 16.10 (``test_intraday.cut_doc``). Synthetic data only."""

from __future__ import annotations

import shutil

import pytest
from test_intraday import FIX, FRI, STATIC, cut_doc
from test_market_history import write

from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value

OFZ, CORP, KEY = "SU26238RMFS4", "RU000A0JXQ44", "RU000A109XR1"


@pytest.mark.parametrize("own_day_quotes", [True, False])
def test_intraday_cut_discounts_on_ruonia(tmp_path, own_day_quotes):
    shutil.copy(FIX / f"{STATIC}.json.gz", tmp_path)
    doc = cut_doc(FRI, "intraday", "2026-10-16T09:30:00+00:00", 1)
    if own_day_quotes:
        for qs in doc["data"]["curve_quote_sets"]:
            qs["snapshot_date"] = FRI.isoformat()
    write(tmp_path, doc)
    ctx = prepare(SnapshotDir(tmp_path), FRI.isoformat(), "intraday")
    outs = value(ctx, [(s, 1000.0) for s in (OFZ, CORP, KEY)], fallback=False)
    for o in outs:
        v = o.valuation
        if not own_day_quotes and o.secid == KEY:  # no key-rate projection either: core refuses, the ladder values it
            assert v is None and o.reason == "core_missing_market_data"
            continue
        assert v is not None and v.price_basis == "last", (o.secid, o.reason, o.detail)
        if own_day_quotes:
            assert v.discount_curve_id == "RUB_RUONIA_OIS" and "RUB OFZ" not in v.risk()
            assert not any("KBD" in a for a in v.assumptions)
        else:
            assert (v.discount_curve_id, set(v.risk())) == ("RUB_KBD", {"RUB OFZ"})
            assert v.quality == "assumed" and any("no RUONIA OIS curve in the market of 2026-10-16" in a
                                                  for a in v.assumptions)
    assert ("curve_error" in ctx.rates) is not own_day_quotes
