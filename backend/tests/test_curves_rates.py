"""S4 rates inputs on the vendored fixture of 2026-10-15: RUONIA OIS curve, key-rate projection, fixings."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from rub_fixed_income import MarketSnapshot

from desk.curves import rates
from desk.marketdata.bundle import SnapshotDir

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
BD = date(2026, 10, 15)


@pytest.fixture(scope="module")
def snaps():
    sd = SnapshotDir(FIX)
    market = sd.market(BD.isoformat())
    return market, sd.get(market.static_snapshot_id)


@pytest.fixture(scope="module")
def ois(snaps):
    return rates.ruonia_ois(*snaps)


TENORS = ["1W", "2W", "1M", "2M", "3M", "6M", "9M", "1Y", *(f"{n}Y" for n in range(2, 11))]


def test_ruonia_ois_reprices_its_quotes_to_10y_on_provisional_calendars(ois):
    lin = ois.lineage
    assert (ois.entry.spec_id, lin["source"], lin["quote_set_id"]) == (rates.RUONIA_OIS_SPEC, "ratescalcs", 2188)
    assert lin["alternatives"] == [{"quote_set_id": 2133, "source": "otc_monitor"}]
    # The hub's calendars end 2027-12-31; the fixed-rule extension schedules every tenor to 10Y.
    assert lin["tenors_used"] == TENORS and lin["tenors_dropped"] == {}
    assert lin["calendar"]["valid_to"] == "2079-12-31" and lin["calendar"]["provisional_from"] == "2027-01-01"
    quotes = ois.entry.curve.quotes
    assert quotes[7].maturity == date(2027, 10, 18) and quotes[7].par_rate == pytest.approx(0.14415, abs=1e-12)
    # The entry keeps the real quotes; the built curve runs on to core's synthetic 20Y quote (TDM#499: core 0.42
    # OisTail, the par spread to the KBD at 10Y), where the calendars reach too.
    assert quotes[-1].maturity == date(2036, 10, 16) and lin["last_node"] == "2046-10-16"
    assert lin["tail"] == {"rule": "kbd", "model": "ois-tail-kbd-par-spread-v1", "tenors": ["15Y", "20Y"]}
    for q in quotes:
        assert abs(ois.curve.quote_par_rate(q) - q.par_rate) * 1e4 < 1e-7, q.tenor


def test_quote_set_falls_back_to_the_stale_mid_only_set(snaps):
    market, static = snaps
    doc = copy.deepcopy(market.doc)
    doc["data"]["curve_quote_sets"] = [q for q in doc["data"]["curve_quote_sets"] if q["source"] != "ratescalcs"]
    lin = rates.ruonia_ois(replace(market, doc=doc), static).lineage
    assert (lin["source"], lin["quote_basis"]) == ("otc_monitor", "mid_only")
    assert lin["stale_reasons"] == ["otc_monitor/ruonia_ois: mid-only quotes (off-hours mark)"]
    doc["data"]["curve_quote_sets"] = []
    with pytest.raises(rates.RatesInputError, match="no ruonia_ois quote set"):
        rates.ruonia_ois(replace(market, doc=doc), static)


def test_ruonia_publication_calendar_skips_the_year_end_day(snaps):
    cal, _ = rates.ruonia_calendar(snaps[1], BD)
    assert not cal.is_business_day(date(2026, 12, 30)) and cal.is_business_day(date(2026, 12, 29))
    joint, info = rates.ois_calendar(snaps[1], BD)
    assert info["valid_to"] == "2079-12-31" and not joint.is_business_day(date(2026, 12, 30))
    # Past the hub's range: fixed holidays and the RUONIA year-end day, by rule.
    assert not joint.is_business_day(date(2030, 1, 8)) and not joint.is_business_day(date(2031, 11, 4))
    assert not cal.is_business_day(date(2030, 12, 31)) and cal.is_business_day(date(2030, 12, 30))


def test_key_rate_projection_starts_at_the_key_rate_in_force(snaps, ois):
    kr = rates.key_rate_projection(*snaps, ois.curve)
    lin, curve = kr.lineage, kr.curve
    assert (kr.entry.spec_id, curve.curve_date, lin["current_rate"]) == (rates.KEY_RATE_PROJECTION_SPEC, BD, 0.14)
    assert lin["next_policy_effective_date"] == "2026-10-26" and lin["next_policy_estimated"]
    assert lin["tenors_used"] == TENORS[4:] and lin["max_repricing_error_bp"] < 1e-7

    def overnight(d):
        return (curve.discount_factor(d) / curve.discount_factor(d + timedelta(days=1)) - 1) * 365

    assert overnight(BD) == pytest.approx(0.14, abs=1e-12)
    assert overnight(date(2026, 10, 25)) == pytest.approx(0.14, abs=1e-12)
    assert overnight(date(2026, 10, 26)) == pytest.approx(lin["segments"][0]["rate"], abs=1e-12)


def test_fixings_are_point_in_time(snaps):
    market, static = snaps
    entries, lin = rates.fixing_entries(market, static)
    ruonia, key = (e.history for e in entries)
    assert (ruonia.latest_fixing_date, key.latest_fixing_date) == (date(2026, 10, 14), date(2026, 10, 14))
    assert ruonia.valuation_date == BD and ruonia.known_at_utc == "2026-10-16T15:42:45+00:00"
    assert ruonia.rate(date(2026, 10, 14)) == (pytest.approx(0.1391), False)
    assert lin["RUONIA"]["missing_business_days"] == []
    # Known on the morning of the 15th: the RUONIA of the 14th (published that day) is not yet known.
    entries, lin = rates.fixing_entries(market, static, datetime(2026, 10, 15, 10, tzinfo=UTC))
    ruonia = entries[0].history
    assert lin["RUONIA"]["unpublished_at_cut"] == ["2026-10-14"]
    assert ruonia.latest_fixing_date == date(2026, 10, 13) and ruonia.unpublished_business_days == (date(2026, 10, 14),)
    assert ruonia.rate(date(2026, 10, 14)) == (pytest.approx(0.1373), True)


def test_core_market_snapshot_accepts_the_entries(snaps, ois):
    market, static = snaps
    kr = rates.key_rate_projection(market, static, ois.curve)
    fixings, _ = rates.fixing_entries(market, static)
    snap = MarketSnapshot(market.id, BD, datetime.fromisoformat(market.doc["known_at"]),
                          curves=(ois.entry, kr.entry), fixings=fixings)
    assert snap.projection_curve("RUB_RUONIA_OIS", "ruonia") is ois.entry.curve
    assert snap.projection_curve("RUB_KEY_RATE_PROJ", "key_rate") is kr.curve
    assert snap.point_in_time_fixing("RUONIA").latest_fixing_date == date(2026, 10, 14)
    assert snap.point_in_time_fixing("KEY_RATE").rate(date(2026, 10, 14)) == (pytest.approx(0.14), False)
