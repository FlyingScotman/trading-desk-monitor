"""TDM#764: a bond whose redemption schedule does not add up to its face (RUS-30: seven future redemptions of 0.01 against
a face of 0.035, the hub's label ``future_redemptions_differ_from_face``) is valued at its price and has no yield,
duration or risk: MOEX computed its effective yield (53.83 % at a close yield of 6.79 %) and duration (505 days, about 635 on the
right schedule) on the same wrong schedule, and the owner's rule is no substitute (neither the close yield nor the time
to the horizon), a data problem is shown, with its reason, not covered."""

from __future__ import annotations

import copy
import math
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from desk.api.concepts import risk_table
from desk.marketdata.adapter import MarketView, Redemption, bond_currency
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, prepare, value
from desk.publish import tdm_run
from desk.valuation.bonds import (
    MANUAL_OVERRIDE_LABEL,
    ROUNDED_LABEL,
    Rejected,
    fixed_schedule,
)
from desk.valuation.fallback import (
    BROKEN_SCHEDULE_TEXT,
    METHOD,
    REDEMPTIONS_LABEL,
    FallbackContext,
    approximate,
    yield_pnl,
)

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
Q = 1000.0
AMORT = "RU000A10BRM5"  # an amortizing bond with a complete schedule, USD face (as RUS-30)
FRN = "RU000A109XR1"  # a floater
RZD = "RU000A0JXQ44"  # a rouble bond valued exactly by core
EFFECTIVE, CLOSE, WAPRICE = "53.83", "6.79", "6.91"


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), "2026-10-15")


@pytest.fixture(scope="module")
def exact(ctx):
    return {o.secid: o for o in value(ctx, [(s, Q) for s in sorted(ctx.static.bonds)])}


def _case(o, reason="no_price", *, labelled=True, label=REDEMPTIONS_LABEL, effective=EFFECTIVE, close=CLOSE, waprice=WAPRICE, broken=False):
    """The bond's outcome with the analytics given; ``labelled``: the hub's label; ``broken``: its future redemptions
    are seven times a hundredth of the face past the face (the sum exceeds it, as RUS-30's)."""
    t, bd = o.terms, o.quote.trade_date
    if broken:
        step = Decimal("0.01")
        t = replace(t, redemptions=tuple(Redemption(bd + timedelta(days=30 * i), "amortization", step)
                                         for i in range(1, 8)), face_value=Decimal("0.035"))
    if labelled:
        t = replace(t, labels=(*t.labels, label))
    sa = {**o.quote.source_analytics, "effective_yield_pct": effective, "yield_close_pct": close,
          "yield_at_waprice_pct": waprice}
    return Outcome(o.secid, t, replace(o.quote, source_analytics=sa), reason=reason)


def _fb(ctx):
    return FallbackContext(ctx, history=[])


LABEL, INCOMPLETE = "bond_redemptions_label", "bond_redemptions_incomplete"


def _not_computed(v, code):
    """No yield, duration or risk, the reason said in the assumptions and in ``extra.risk_missing``."""
    assert math.isnan(v.ytm_pct) and math.isnan(v.macaulay_years) and math.isnan(v.modified_duration)
    assert v.dv01_rub == 0.0 and v.spread01_rub == 0.0 and not any(v.buckets.values()) and not any(
        x for bs in v.curve_buckets.values() for x in bs.values()) and math.isnan(v.z_spread_bp)
    text = v.extra["risk_missing"]["text"]  # TDM#768: the reason of the monitor's own calculation may follow the text
    assert text.startswith(BROKEN_SCHEDULE_TEXT[code]) and text in v.assumptions
    assert v.extra["risk_missing"] == {"reason": code, "text": text}


def test_a_bond_with_the_label_has_no_yield_duration_or_risk_and_says_why(ctx, exact):
    o = exact[AMORT]
    assert bond_currency(o.terms) == "USD" and o.quote.source_analytics.get("duration_days")
    v = approximate(ctx, _case(o), Q, _fb(ctx))
    assert v.quality == "market_value"
    _not_computed(v, LABEL)
    assert "метка хаба future_redemptions_differ_from_face" in v.extra["risk_missing"]["text"]
    assert "доходность, дюрация и DV01 не считаются" in v.extra["risk_missing"]["text"]


@pytest.mark.parametrize("close,waprice", [(CLOSE, WAPRICE), (None, WAPRICE), (None, None)])
def test_no_substitute_whatever_other_yield_the_source_has(ctx, exact, close, waprice):
    v = approximate(ctx, _case(exact[AMORT], close=close, waprice=waprice), Q, _fb(ctx))
    _not_computed(v, LABEL)


def test_a_rejection_for_an_incomplete_schedule_without_the_label_says_it_neutrally(ctx, exact):
    v = approximate(ctx, _case(exact[AMORT], "redemption_incomplete", labelled=False), Q, _fb(ctx))
    _not_computed(v, INCOMPLETE)
    text = v.extra["risk_missing"]["text"]
    assert "redemption_incomplete" in text and REDEMPTIONS_LABEL not in text


@pytest.mark.parametrize("reason", ["no_price", "no_quote", "coupon_not_known"])
def test_the_label_decides_whatever_the_reason_of_the_rejection(ctx, exact, reason):
    _not_computed(approximate(ctx, _case(exact[AMORT], reason), Q, _fb(ctx)), LABEL)


def test_a_bond_without_the_label_or_the_rejection_has_its_own_yield(ctx, exact):
    """TDM#619: not the source's (the 53.83 % of the case), the monitor's own from the schedule and the price."""
    v = approximate(ctx, _case(exact[AMORT], labelled=False), Q, _fb(ctx))
    assert v.ytm_pct == pytest.approx(9.89, abs=0.05) and "risk_missing" not in v.extra
    assert not any("redemption" in a and "не считаются" in a for a in v.assumptions)
    assert v.dv01_rub != 0.0 and math.isfinite(v.modified_duration) and v.buckets


def test_a_normal_amortizing_bond_is_not_touched(ctx, exact):
    o = exact[AMORT]
    assert REDEMPTIONS_LABEL not in o.terms.labels and o.reason != "redemption_incomplete"
    sa = {**o.quote.source_analytics, "effective_yield_pct": "9.8989"}
    v = approximate(ctx, Outcome(o.secid, o.terms, replace(o.quote, source_analytics=sa), reason="no_price"), Q, _fb(ctx))
    assert v.ytm_pct == pytest.approx(9.8989, abs=0.03) and "risk_missing" not in v.extra and v.dv01_rub != 0.0
    assert math.isfinite(v.macaulay_years)  # TDM#619: the monitor's own, a few bp from MOEX's right figure


def test_the_value_at_the_price_does_not_change(ctx, exact):
    plain = approximate(ctx, _case(exact[AMORT], labelled=False), Q, _fb(ctx))
    broken = approximate(ctx, _case(exact[AMORT]), Q, _fb(ctx))
    for f in ("pv_rub", "dirty_rub", "clean_pct", "accrued_rub", "face_rub", "quality", "price_basis", "horizon_date"):
        assert getattr(broken, f) == getattr(plain, f), f


def test_the_core_rejects_a_schedule_that_exceeds_the_face_and_the_ladder_follows(ctx, exact):
    """The case itself: future redemptions of 0.07 against a face of 0.035 are refused, the ladder has no yield."""
    o = _case(exact[AMORT], labelled=False, broken=True)
    with pytest.raises(Rejected) as err:
        fixed_schedule(o.terms, ctx.business_date)
    assert err.value.reason == "redemption_incomplete"
    _not_computed(approximate(ctx, replace(o, reason=err.value.reason), Q, _fb(ctx)), INCOMPLETE)


def test_a_foreign_bonds_pnl_has_no_carry_and_the_total_does_not_move(ctx, exact):
    """``yield_pnl`` carries at the D-1 ytm_pct; a bond without one has none: the whole PnL of the day is in other (the
    dirty price move), as for a RUB bond, the total is the same as with a yield."""
    o, bd = exact[AMORT], ctx.business_date

    def pair(**kw):
        cur = approximate(ctx, _case(o, **kw), Q, _fb(ctx))
        prev = replace(cur, valuation_date=bd - timedelta(1), settlement=bd - timedelta(1),
                       dirty_rub=cur.dirty_rub * 0.998, pv_rub=cur.pv_rub * 0.998)
        return yield_pnl(prev, cur, o.terms, ctx, ctx)

    broken, plain = pair(), pair(labelled=False)
    assert plain.method == "desk-flash-yield-v1" and plain.carry != 0.0
    assert broken.method == METHOD and broken.carry == 0.0 and broken.other == pytest.approx(broken.total)
    assert broken.total == pytest.approx(plain.total) and broken.v0 == pytest.approx(plain.v0)


def test_a_redeemed_bond_with_the_label_has_no_yield_and_no_note(ctx, exact):
    o, bd = exact[AMORT], ctx.business_date
    c = _case(o)
    t = replace(c.terms, maturity_date=bd, redemptions=(Redemption(bd, "maturity", c.terms.face_value),), offers=())
    v = approximate(ctx, replace(c, terms=t), Q, _fb(ctx))
    assert v.pv_rub == 0.0 and math.isnan(v.ytm_pct)
    assert "risk_missing" not in v.extra and BROKEN_SCHEDULE_TEXT[LABEL] not in v.assumptions


def test_a_rouble_floater_with_the_label_has_no_yield_or_risk_either(ctx, exact):
    o = exact[FRN]
    assert o.terms.coupon_type == "floating" and bond_currency(o.terms) == "RUB"
    v = approximate(ctx, _case(o), Q, _fb(ctx))
    assert v.quality in ("market_value", "stale_price")
    _not_computed(v, LABEL)
    plain = approximate(ctx, _case(o, labelled=False), Q, _fb(ctx))
    assert (v.pv_rub, v.dirty_rub) == (plain.pv_rub, plain.dirty_rub) and plain.dv01_rub != 0.0


def test_the_row_has_no_yield_or_risk_and_the_risk_table_names_the_reason(ctx, exact):
    o = exact[AMORT]
    v = approximate(ctx, _case(o), Q, _fb(ctx))
    out = replace(_case(o), valuation=v)
    c = tdm_run.run_content(ctx, [("P1", "MIXED", out)], None, {})
    (row,) = c.rows
    assert row["ytm"] is None and row["mdur"] is None and row["dv01"] == 0.0 and row["extra"]["quality"] == "market_value"
    assert not any(x for bs in row["risk"].values() for x in bs.values())
    assert BROKEN_SCHEDULE_TEXT[LABEL] in row["extra"]["assumptions"]  # the card's list
    assert row["extra"]["risk_missing"] == v.extra["risk_missing"] == {
        "reason": LABEL, "text": BROKEN_SCHEDULE_TEXT[LABEL]}  # TDM#764: the row carries it, as a derivative's (#616)
    fx = {"USD": ctx.market.fx("USD")["moex_19"]}
    t = risk_table([{"position_id": "P1", "fact": row}], fx)
    assert t["total"]["n_risk_missing"] == 1
    (m,) = [x for r in t["rows"] if r["level"] == "kind" and r["ccy"] == "USD" for x in r["risk_missing"]]
    assert m["reason"] == LABEL and m["text"] == BROKEN_SCHEDULE_TEXT[LABEL] and m["n"] == 1
    assert "не считаются" in m["text"] and "метка хаба future_redemptions_differ_from_face" in m["text"]


def test_a_bond_without_the_reason_has_no_risk_missing_in_its_row(ctx, exact):
    o = exact[AMORT]
    out = replace(_case(o, labelled=False), valuation=approximate(ctx, _case(o, labelled=False), Q, _fb(ctx)))
    (row,) = tdm_run.run_content(ctx, [("P1", "MIXED", out)], None, {}).rows
    assert "risk_missing" not in row["extra"]


def _off_by(o, amount):
    """The bond's terms with its last redemption ``amount`` larger: the schedule is off the face by less than the 0.01 of
    ``fixed_schedule`` (the hub's own tolerance is 0.005, so it labels it)."""
    t = o.terms
    last = max(t.redemptions, key=lambda r: r.date)
    return replace(t, redemptions=tuple(replace(r, value_per_bond=r.value_per_bond + amount) if r is last else r
                                        for r in t.redemptions))


def test_the_hubs_label_refuses_the_exact_path_though_the_sum_is_within_the_cores_tolerance(ctx, exact, monkeypatch):
    """The hub labels a bond off by more than 0.005, ``fixed_schedule`` refuses one off by more than 0.01: a bond with the
    label and 0.007 off went through core and got a yield, duration and risk of its wrong schedule."""
    o = exact[RZD]
    assert o.valuation.quality == "exact"
    off = _off_by(o, Decimal("0.007"))
    fixed_schedule(off, ctx.business_date)  # within the tolerance: valid without the label
    labelled = replace(off, labels=(*off.labels, REDEMPTIONS_LABEL))
    with pytest.raises(Rejected, match="redemption_incomplete: hub label future_redemptions_differ_from_face"):
        fixed_schedule(labelled, ctx.business_date)
    monkeypatch.setitem(ctx.static.bonds, RZD, off)
    assert value(ctx, [(RZD, Q)])[0].valuation.quality == "exact"
    monkeypatch.setitem(ctx.static.bonds, RZD, labelled)
    (out,) = value(ctx, [(RZD, Q)])
    v = out.valuation
    assert out.reason == "redemption_incomplete" and v.quality in ("market_value", "stale_price")
    _not_computed(v, LABEL)
    assert v.pv_rub > 0


@pytest.mark.parametrize("labels,code,named", [
    ((REDEMPTIONS_LABEL,), LABEL, REDEMPTIONS_LABEL),
    ((ROUNDED_LABEL,), "bond_redemptions_rounded", ROUNDED_LABEL),
    # contract 1.10: the rounded one always comes with the other, and is the one said
    ((REDEMPTIONS_LABEL, ROUNDED_LABEL), "bond_redemptions_rounded", ROUNDED_LABEL),
    ((ROUNDED_LABEL, REDEMPTIONS_LABEL), "bond_redemptions_rounded", ROUNDED_LABEL)])
def test_both_hub_labels_of_a_broken_schedule_say_so(ctx, exact, labels, code, named):
    c = _case(exact[AMORT], labelled=False)
    v = approximate(ctx, replace(c, terms=replace(c.terms, labels=(*c.terms.labels, *labels))), Q, _fb(ctx))
    _not_computed(v, code)
    assert named in v.extra["risk_missing"]["text"]
    with pytest.raises(Rejected, match=f"hub label {named}"):
        fixed_schedule(replace(exact[RZD].terms, labels=labels), ctx.business_date)


def test_a_manual_override_of_the_hub_is_not_a_broken_schedule(ctx, exact):
    """The hub takes the label off and says ``redemptions_manual_override``: the exact path is valid, the ladder uses what
    it did before."""
    o = exact[RZD]
    t = replace(o.terms, labels=(*o.terms.labels, MANUAL_OVERRIDE_LABEL))
    assert fixed_schedule(t, ctx.business_date).horizon_date
    v = approximate(ctx, _case(exact[AMORT], label=MANUAL_OVERRIDE_LABEL), Q, _fb(ctx))
    assert v.ytm_pct == pytest.approx(9.89, abs=0.05) and "risk_missing" not in v.extra and v.dv01_rub != 0.0


def test_a_bond_with_the_label_and_no_price_has_no_model_price_or_risk(ctx, exact):
    """The model price needs the fixed schedule, which the label refuses: nothing is modelled for such a bond, at par."""
    o = exact[RZD]
    t = replace(o.terms, labels=(*o.terms.labels, REDEMPTIONS_LABEL))
    v = approximate(ctx, Outcome(RZD, t, None, reason="no_quote"), Q, _fb(ctx))
    assert v.quality == "par_proxy" and v.dv01_rub == 0.0 and math.isnan(v.ytm_pct)
    assert v.extra["risk_missing"]["reason"] == LABEL
    plain = approximate(ctx, Outcome(RZD, o.terms, None, reason="no_quote"), Q, _fb(ctx))
    assert plain.quality in ("model_price", "par_proxy") and "risk_missing" not in plain.extra


def _unreliable(o, fields, reasons=("moex_schedule_rounded",)):
    return replace(o.quote, source_analytics_unreliable={"fields": list(fields), "reasons": list(reasons)})


def test_the_unreliable_fields_the_hub_names_are_not_used_and_a_bond_without_a_schedule_to_compute_from_has_no_risk(ctx, exact):
    """A floater has no fixed schedule to compute a yield from (TDM#768 computes it for a complete fixed one): MOEX's
    fields the hub lists are not used, and it has none."""
    o = exact[FRN]
    code = "bond_source_analytics_unreliable"
    for fields in (["yield_close_pct", "effective_yield_pct", "duration_days", "zspread_bp"], ["duration_days"],
                   ["effective_yield_pct"]):
        out = Outcome(o.secid, o.terms, _unreliable(_case(o, labelled=False), fields), reason="no_price")
        v = approximate(ctx, out, Q, _fb(ctx))
        _not_computed(v, code)
        assert "посчитаны по искажённому графику погашений: не используются" in v.extra["risk_missing"]["text"]
    plain = approximate(ctx, _case(o, labelled=False), Q, _fb(ctx))
    assert (v.pv_rub, v.dirty_rub) == (plain.pv_rub, plain.dirty_rub) and plain.dv01_rub != 0.0


def test_the_unreliable_fields_of_a_bond_with_a_complete_schedule_are_not_used_and_its_own_yield_is(ctx, exact):
    """TDM#768: the amortizer's schedule is complete: the yield and duration are the monitor's (tests/test_fallback_own_yield_768.py)."""
    o = exact[AMORT]
    terms = replace(o.terms, day_count="ACT/365F", day_count_source="cbonds")  # the fixtures state none (TDM#768: needed)
    out = Outcome(o.secid, terms, _unreliable(_case(o, labelled=False), ["effective_yield_pct", "duration_days"]),
                  reason="no_price")
    v = approximate(ctx, out, Q, _fb(ctx))
    assert "risk_missing" not in v.extra and math.isfinite(v.ytm_pct) and v.dv01_rub != 0.0
    assert v.ytm_pct != pytest.approx(53.83) and v.ytm_pct == pytest.approx(9.89, abs=0.05)


@pytest.mark.parametrize("unreliable", [None, {"fields": [], "reasons": []}, {"fields": ["zspread_bp", "gspread_bp"]}])
def test_nothing_unreliable_or_fields_the_ladder_does_not_read_change_nothing(ctx, exact, unreliable):
    c = _case(exact[AMORT], labelled=False)
    q = replace(c.quote, source_analytics_unreliable=unreliable)
    v = approximate(ctx, replace(c, quote=q), Q, _fb(ctx))
    assert v.ytm_pct == pytest.approx(9.89, abs=0.05) and "risk_missing" not in v.extra and v.dv01_rub != 0.0


def test_the_market_view_reads_the_unreliable_fields_and_a_hub_without_them_as_before(ctx):
    snap = copy.deepcopy(ctx.market.snapshot)
    rows = snap.doc["data"]["bond_quotes"]
    assert all("source_analytics_unreliable" not in r for r in rows)  # a hub before 1.10
    old = MarketView(snap)
    assert all(q.source_analytics_unreliable is None for q in old.quotes.values())
    rows[0]["source_analytics_unreliable"] = {"fields": ["yield_close_pct", "duration_days"], "reasons": ["rounded"]}
    rows[1]["source_analytics_unreliable"] = None
    new = MarketView(snap)
    assert new.quotes[(rows[0]["secid"], rows[0]["board"])].source_analytics_unreliable == {
        "fields": ["yield_close_pct", "duration_days"], "reasons": ["rounded"]}
    assert new.quotes[(rows[1]["secid"], rows[1]["board"])].source_analytics_unreliable is None
    # an intraday cut's quote (no EOD quote of its board): the same field
    snap.doc["data"]["bond_quotes_intraday"] = [{
        "secid": rows[0]["secid"], "board": "INTRADAY_TEST", "ts": "2026-10-15T12:00:00+03:00", "close_price_pct": "100",
        "source_analytics": {}, "source_analytics_unreliable": {"fields": ["duration_days"], "reasons": ["rounded"]}}, {
        "secid": rows[0]["secid"], "board": "INTRADAY_OLD", "ts": "2026-10-15T12:00:00+03:00", "close_price_pct": "100"}]
    cut = MarketView(snap)
    assert cut.quotes[(rows[0]["secid"], "INTRADAY_TEST")].source_analytics_unreliable == {
        "fields": ["duration_days"], "reasons": ["rounded"]}
    assert cut.quotes[(rows[0]["secid"], "INTRADAY_OLD")].source_analytics_unreliable is None
