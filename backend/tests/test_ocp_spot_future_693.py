"""TDM#693: a report line of a spot type with a future value date (the adapter's ``x_spot_future``) that is the nominal of
an FX derivative the monitor knows (a forward, an NDF, a cross NDF, a future) is taken out of the base of the published
OCP (``report_base_plus_day``) and the deal's own exposure stands in its place: its PV when valued, else the legs of its
terms at the discount factor 1 with the reason. An unmatched line stays in the base. Synthetic rows, invented numbers."""

from __future__ import annotations

import math
from datetime import date

import pytest

from desk.rules import ocp as O
from desk.rules import ocp_spot as S

B = date(2026, 10, 15)  # Thursday: the report's date and the run's
BOOKS = {"D1": {"book_id": "D1", "book_type": "DERIVS", "entity": "ENT1", "ocp_to": None},
         "D2": {"book_id": "D2", "book_type": "DERIVS", "entity": "ENT1", "ocp_to": None}}
POLICY = O.OcpPolicy(report_components=frozenset(set(O.COMPONENTS) - {"options"}))  # NDFs and futures are in the report
UNITS = [{"scope": {"entity": "ENT1", "book_id": "D1"}, "set_id": 11, "valid_date": B, "row_count": 2,
          "file_name": "ocp_20261015.csv", "inbox_file_sha256": "f15"}]


def line(value_date, amount, src="R5", pair="EUR/USD"):
    return {"src": src, "value_date": value_date, "pair": pair, "amount_ccy": amount}


def report(ccy, base, spot, kinds=None, book="D1"):
    return {"entity": "ENT1", "book_id": book, "ccy": ccy, "ocp_ccy": base, "_set_id": 11, "_src": "L2",
            "derivatives_ccy": {"ndf": "0"} if kinds is None else kinds, "x_spot_future": spot}


def terms(pid, trade, base="EUR", quote="USD", nb=800_000, nq=880_000, vd="2026-12-18", fixing="2026-12-16",
          side="buy"):
    return {"trade_id": trade, "position_id": pid, "product_kind": "fx_forward", "book_id": "D1", "ndf": True,
            "base_ccy": base, "quote_ccy": quote, "side": side, "notional_base": nb, "notional_quote": nq,
            "value_date": vd, "fixing_date": fixing}


def valued(pid, delta, nominal):
    """An NDF core valued: its PV per currency (``fx_delta``), its legs' nominal (``ocp_amount``)."""
    return {"position_id": pid, "book_id": "D1", "instr_type": "FX", "fx_delta": delta,
            "extra": {"core": {"product": "fx_ndf", "trades": [pid]}, "ocp_amount": nominal,
                      "legs": [{"value_date": "2026-12-18", "fixing_date": "2026-12-16"}]}}


def no_curve(pid, ccy="EUR", amount=800_000.0):
    """The same NDF core did not value (no curve of a currency): the report's rule, the notional as its FX delta."""
    return {"position_id": pid, "book_id": "D1", "instr_type": "FX", "ccy": ccy, "fx_delta": {ccy: amount},
            "extra": {"fx_delta_basis": "notional (no trade terms: undiscounted)", "not_exact_reason": "no_foreign_curve",
                      "risk_missing": {"reason": "no_curve", "ccy": ccy, "text": "нет данных: нет кривой EUR"},
                      "reported": {"asset_class": "fx", "value_date": "2026-12-18"}}}


def run(rows, rep, trade_terms=(), policy=POLICY, on=B):
    return O.compute_ocp(list(rows), BOOKS, on, terms=list(trade_terms), report_rows=rep, report_units=UNITS,
                         policy=policy)


def pub(r):
    return {(x["book_id"], x["ccy"]): x for x in r.published}


def parts_sum(x):
    assert math.fsum(x["parts"].values()) == pytest.approx(x["amount"], abs=1e-6)
    for p, v in x["parts"].items():
        c = (x["contributions"] or {}).get(p)
        got = math.fsum([*(i["amount"] for i in c["items"]), (c["rest"] or {}).get("amount", 0.0)]) if c else 0.0
        assert got == pytest.approx(v, abs=1e-6), p


def test_a_valued_deal_is_counted_once_by_its_exposure_not_its_nominal():
    # the line is 4 days before the settlement and 2 business days before the fixing of the deal
    rep = [report("EUR", "1000000", [line("2026-12-14", "800000")])]
    r = run([valued("D1|NDF", {"EUR": 790_000.0}, {"EUR": 800_000.0})], rep, [terms("D1|NDF", "T-1", quote="RUB",
                                                                                      nq=80_000_000)])
    x = pub(r)[("D1", "EUR")]
    assert x["parts"]["base"] == 200_000 and x["parts"]["derivatives"] == 790_000 and x["amount"] == 990_000
    out = x["spot_future"]["out"]
    assert [(o["position_id"], o["trade_ids"], o["amount"], o["days"]) for o in out] == [("D1|NDF", ["T-1"], 800_000, 2)]
    assert x["spot_future"]["unmatched"] == [] and x["spot_future"]["df1"] == []
    base = x["contributions"]["base"]["items"]
    assert [i["kind"] for i in base] == ["report", "report_spot_future"] and base[1]["amount"] == -800_000
    parts_sum(x)
    # the report as a whole is unchanged: the reconciliation sees what the adapter gave
    assert {(c["book_id"], c["ccy"]): c for c in r.comparisons}[("D1", "EUR")]["reported"] == 1_000_000


def cross_valued(pid="D1|NDF"):
    """A cross NDF EUR/USD core valued: its PV per currency, its legs' nominal."""
    return valued(pid, {"EUR": 790_000.0, "USD": -870_000.0}, {"EUR": 800_000.0, "USD": -880_000.0})


def test_without_a_curve_only_the_matched_leg_is_replaced_and_the_other_is_said():
    # a cross NDF EUR/USD, EUR bought: the report has the EUR nominal as a spot-type line, no line for the USD leg
    rep = [report("EUR", "1000000", [line("2026-12-22", "800000")])]
    r = run([no_curve("D1|NDF")], rep, [terms("D1|NDF", "T-2")])
    p = pub(r)
    eur = p[("D1", "EUR")]
    assert eur["parts"]["base"] == 200_000 and eur["parts"]["derivatives"] == 800_000 and eur["amount"] == 1_000_000
    item = eur["contributions"]["derivatives"]["items"][0]
    assert item["marker"] == "нет кривой EUR: дисконт-фактор 1" and item["basis"] == "nominal_df1"
    assert eur["spot_future"]["df1"] == [{"position_id": "D1|NDF", "trade_ids": ["T-2"], "amount": 800_000,
                                          "note": "нет кривой EUR: дисконт-фактор 1"}]
    assert [o["position_id"] for o in eur["spot_future"]["out"]] == ["D1|NDF"]
    assert ("D1", "USD") not in p  # the USD leg is as before the rule: no line of it in the report, none from the row
    parts_sum(eur)
    partial = next(g for g in r.gaps if g["code"] == "spot_future_partial")
    assert "T-2" in partial["items"][0] and "USD" in partial["items"][0] and "spot_future_unmatched" not in {
        g["code"] for g in r.gaps}


def test_without_a_curve_both_legs_matched_each_stands_at_discount_factor_one():
    rep = [report("EUR", "1000000", [line("2026-12-22", "800000")]),
           report("USD", "-880000", [line("2026-12-22", "-880000", "R6")])]
    r = run([no_curve("D1|NDF")], rep, [terms("D1|NDF", "T-2")])
    p = pub(r)
    eur, usd = p[("D1", "EUR")], p[("D1", "USD")]
    assert eur["amount"] == 1_000_000 and usd["amount"] == -880_000
    assert usd["parts"] == {"base": 0, "derivatives": -880_000, "bonds": 0, "trades": 0, "outside": 0}
    assert usd["spot_future"]["df1"][0]["amount"] == -880_000 and usd["spot_future"]["out"][0]["src"] == "R6"
    for x in (eur, usd):
        parts_sum(x)
    assert "spot_future_partial" not in {g["code"] for g in r.gaps}


def test_the_second_currency_in_the_base_as_an_ordinary_row_is_not_counted_twice():
    # the USD leg stands in the report as an ordinary line (no spot-type line): it is not replaced, not doubled
    rep = [report("EUR", "1000000", [line("2026-12-22", "800000")]), report("USD", "-880000", None)]
    rep[1].pop("x_spot_future")
    r = run([no_curve("D1|NDF")], rep, [terms("D1|NDF", "T-2")])
    p = pub(r)
    assert p[("D1", "USD")]["amount"] == -880_000 and p[("D1", "USD")]["parts"]["derivatives"] == 0
    assert p[("D1", "EUR")]["amount"] == 1_000_000
    assert "spot_future_partial" in {g["code"] for g in r.gaps}


def test_valued_cross_with_one_leg_matched_the_other_leg_is_as_before():
    # kind apart (the usual way): both legs' PV from the positions; the EUR line is out; the USD base is as it was
    both = [report("EUR", "1000000", [line("2026-12-22", "800000")]), report("USD", "-880000", None)]
    both[1].pop("x_spot_future")
    without = [{k: v for k, v in x.items() if k != "x_spot_future"} for x in both]
    t = [terms("D1|NDF", "T-5")]
    now, was = pub(run([cross_valued()], both, t)), pub(run([cross_valued()], without, t))
    assert now[("D1", "USD")]["amount"] == was[("D1", "USD")]["amount"]
    assert now[("D1", "EUR")]["amount"] == was[("D1", "EUR")]["amount"] - 800_000
    # the kind not apart: the matched EUR leg gets its PV here, the USD leg is not added (its nominal is in the base)
    both[0]["derivatives_ccy"] = both[1]["derivatives_ccy"] = {"fx_forward": "0"}
    now = pub(run([cross_valued()], both, t))
    assert now[("D1", "EUR")]["parts"] == {"base": 200_000, "derivatives": 790_000, "bonds": 0, "trades": 0, "outside": 0}
    assert now[("D1", "USD")]["amount"] == -880_000 and now[("D1", "USD")]["parts"]["derivatives"] == 0


def test_valued_cross_with_both_legs_matched_each_leg_once():
    rep = [report("EUR", "1000000", [line("2026-12-22", "800000")]),
           report("USD", "-880000", [line("2026-12-22", "-880000", "R6")])]
    p = pub(run([cross_valued()], rep, [terms("D1|NDF", "T-5")]))
    assert p[("D1", "EUR")]["amount"] == 200_000 + 790_000 and p[("D1", "USD")]["amount"] == -870_000
    for x in p.values():
        parts_sum(x)


def test_the_cross_leg_uses_the_rate_of_the_terms_when_the_quote_notional_is_absent():
    t = terms("D1|NDF", "T-3", nq=None)
    t["contract_rate"] = 1.1
    rep = [report("EUR", "1000000", [line("2026-12-18", "800000")]),
           report("USD", "-880000", [line("2026-12-18", "-880000.5", "R6")])]
    usd = pub(run([no_curve("D1|NDF")], rep, [t]))[("D1", "USD")]
    assert usd["amount"] == pytest.approx(-880_000)     # the leg is the rate's: 1e-4 of it is within the tolerance


def test_a_sold_leg_has_the_opposite_signs():
    rep = [report("EUR", "-1000000", [line("2026-12-18", "-800000")]),
           report("USD", "880000", [line("2026-12-18", "880000", "R6")])]
    r = pub(run([no_curve("D1|NDF", amount=-800_000.0)], rep, [terms("D1|NDF", "T-4", side="sell")]))
    assert r[("D1", "EUR")]["parts"] == {"base": -200_000, "derivatives": -800_000, "bonds": 0, "trades": 0, "outside": 0}
    assert r[("D1", "USD")]["amount"] == 880_000


def test_an_unmatched_line_stays_in_the_base_and_is_listed():
    rep = [report("EUR", "1000000", [line("2026-12-18", "123456")])]
    r = run([valued("D1|NDF", {"EUR": 790_000.0}, {"EUR": 800_000.0})], rep, [terms("D1|NDF", "T-1", quote="RUB",
                                                                                      nq=80_000_000)])
    x = pub(r)[("D1", "EUR")]
    assert x["parts"]["base"] == 1_000_000 and x["parts"]["derivatives"] == 790_000
    assert x["spot_future"]["out"] == [] and [(u["amount"], u["why"]) for u in x["spot_future"]["unmatched"]] == [
        (123_456, "amount")]
    assert "spot_future_unmatched" in {g["code"] for g in r.gaps}
    # no deal in the book at all
    x = pub(run([], rep))[("D1", "EUR")]
    assert x["parts"]["base"] == 1_000_000 and x["spot_future"]["unmatched"][0]["why"] == "no_deal"


def test_a_line_far_from_the_deal_in_time_or_of_the_other_sign_is_not_matched():
    for spot, why in ((line("2026-12-28", "800000"), "date"), (line("2026-12-18", "-800000"), "sign")):
        rep = [report("EUR", "1000000", [spot])]
        x = pub(run([valued("D1|NDF", {"EUR": 790_000.0}, {"EUR": 800_000.0})], rep,
                    [terms("D1|NDF", "T-1", quote="RUB", nq=80_000_000)]))[("D1", "EUR")]
        assert x["parts"]["base"] == 1_000_000 and x["spot_future"]["unmatched"][0]["why"] == why


def test_two_deals_one_line_takes_the_nearest_date_and_says_the_other_fits_too():
    rep = [report("EUR", "1000000", [line("2026-12-17", "800000")])]
    rows = [valued("D1|A", {"EUR": 790_000.0}, {"EUR": 800_000.0}), valued("D1|B", {"EUR": 780_000.0}, {"EUR": 800_000.0})]
    ts = [terms("D1|A", "T-A", quote="RUB", nq=1, vd="2026-12-21", fixing="2026-12-21"),
          terms("D1|B", "T-B", quote="RUB", nq=1, vd="2026-12-18", fixing="2026-12-17")]
    r = run(rows, rep, ts)
    out = pub(r)[("D1", "EUR")]["spot_future"]["out"]
    assert [(o["position_id"], o["days"], o["others"]) for o in out] == [("D1|B", 0, ["D1|A"])]
    assert "spot_future_ambiguous" in {g["code"] for g in r.gaps}


def test_two_identical_deals_on_two_dates_take_a_line_each():
    rep = [report("EUR", "1600000", [line("2026-12-18", "800000", "R5"), line("2026-12-30", "800000", "R6")])]
    rows = [valued("D1|A", {"EUR": 790_000.0}, {"EUR": 800_000.0}), valued("D1|B", {"EUR": 780_000.0}, {"EUR": 800_000.0})]
    ts = [terms("D1|A", "T-A", quote="RUB", nq=1, vd="2026-12-18", fixing="2026-12-18"),
          terms("D1|B", "T-B", quote="RUB", nq=1, vd="2026-12-30", fixing="2026-12-30")]
    x = pub(run(rows, rep, ts))[("D1", "EUR")]
    assert sorted((o["src"], o["position_id"]) for o in x["spot_future"]["out"]) == [("R5", "D1|A"), ("R6", "D1|B")]
    assert x["parts"]["base"] == 0


def test_a_deal_of_the_other_book_is_not_matched():
    other = valued("D2|NDF", {"EUR": 790_000.0}, {"EUR": 800_000.0})
    other["book_id"] = "D2"
    x = pub(run([other], [report("EUR", "1000000", [line("2026-12-18", "800000")])],
                [{**terms("D2|NDF", "T-1", quote="RUB", nq=1), "book_id": "D2"}]))[("D1", "EUR")]
    assert x["parts"]["base"] == 1_000_000 and x["spot_future"]["unmatched"][0]["why"] == "no_deal"


def test_a_kind_the_report_does_not_give_apart_is_added_for_a_matched_deal():
    # no derivatives_ccy at all (the file gives no kind apart): the PV was not added, the line is out: add the PV
    rep = [report("EUR", "1000000", [line("2026-12-18", "800000")], kinds={"fx_forward": "0"})]
    x = pub(run([valued("D1|NDF", {"EUR": 790_000.0}, {"EUR": 800_000.0})], rep,
                [terms("D1|NDF", "T-1", quote="RUB", nq=80_000_000)]))[("D1", "EUR")]
    assert x["parts"]["base"] == 200_000 and x["parts"]["derivatives"] == 790_000
    assert x["contributions"]["derivatives"]["items"][0]["note"] == "сопоставлена со строкой отчёта спот-типа"
    parts_sum(x)


def test_an_ordinary_spot_and_a_report_without_the_field_are_untouched():
    rows = [valued("D1|NDF", {"EUR": 790_000.0}, {"EUR": 800_000.0})]
    ts = [terms("D1|NDF", "T-1", quote="RUB", nq=80_000_000)]
    rep = [{k: v for k, v in report("EUR", "1000000", None).items() if k != "x_spot_future"}]
    x = pub(run(rows, rep, ts))[("D1", "EUR")]
    assert x["parts"]["base"] == 1_000_000 and "spot_future" not in x
    assert [i["kind"] for i in x["contributions"]["base"]["items"]] == ["report"]


def test_the_field_may_sit_under_x_and_a_bad_entry_is_left_out():
    row = report("EUR", "1000000", None)
    del row["x_spot_future"]
    row["x"] = {"x_spot_future": [line("2026-12-18", "800000"), {"value_date": "bad", "amount_ccy": "1"}, "junk"]}
    x = pub(run([valued("D1|NDF", {"EUR": 790_000.0}, {"EUR": 800_000.0})], [row],
                [terms("D1|NDF", "T-1", quote="RUB", nq=80_000_000)]))[("D1", "EUR")]
    assert x["parts"]["base"] == 200_000


# ---- the matching alone -------------------------------------------------------------------------------------------


def deal(pid="P1", legs=None, dates=(date(2026, 12, 18),), book="D1"):
    legs = {"EUR": 800_000.0, "USD": -880_000.0} if legs is None else legs
    return S.Deal(pid, book, "ndf", "ndf", legs, legs, tuple(dates))


def sl(amount, d=date(2026, 12, 18), ccy="EUR", book="D1"):
    return S.SpotLine(ccy, d, amount, book)


def test_the_amount_tolerance_is_relative_1e6_or_one_unit():
    assert S.match([sl(800_000.5)], [deal()]).matched and S.match([sl(800_000 + 0.7)], [deal()]).matched
    assert not S.match([sl(800_002.0)], [deal()]).matched
    big = {"EUR": 1_000_000_000.0}
    assert S.match([sl(1_000_000_000.9)], [deal(legs=big)]).matched          # within 1e-6 of a large nominal
    assert not S.match([sl(1_000_001_100.0)], [deal(legs=big)]).matched


def test_the_date_window_is_five_business_days_either_way():
    near = [date(2026, 12, 25), date(2026, 12, 11)]  # 5 business days after / before 2026-12-18
    assert all(S.match([sl(800_000, d)], [deal()]).matched for d in near)
    assert not S.match([sl(800_000, date(2026, 12, 28))], [deal()]).matched
    def cal(d):  # a holiday in between
        return d.weekday() < 5 and d != date(2026, 12, 24)

    assert S.match([sl(800_000, date(2026, 12, 28))], [deal()], cal).matched and \
        not S.match([sl(800_000, date(2026, 12, 29))], [deal()], cal).matched


def test_the_second_currency_leg_matches_too_and_a_leg_is_taken_once():
    res = S.match([sl(-880_000, ccy="USD")], [deal()])
    assert [(m.line.ccy, m.deal.position_id) for m in res.matched] == [("USD", "P1")]
    two = S.match([sl(800_000), sl(800_000)], [deal()])
    assert len(two.matched) == 1 and [u.why for u in two.unmatched] == ["taken"]


def test_a_line_without_a_book_may_match_a_deal_of_any_allowed_book():
    res = S.match([sl(800_000, book=None)], [deal(book="D2")])
    assert [m.deal.book_id for m in res.matched] == ["D2"]


def test_spot_lines_read_the_adapters_field():
    got = S.spot_lines([line("2026-12-18", "800000"), {"value_date": "2026-12-18"}, 5, line("x", "1")], ccy="EUR",
                       book_id="D1")
    assert got == [S.SpotLine("EUR", date(2026, 12, 18), 800_000.0, "D1", "EUR/USD", "R5")]
    assert S.spot_lines(None, ccy="EUR", book_id=None) == [] and S.spot_lines("x", ccy="EUR", book_id=None) == []
    assert S.SpotLine.of(got[0].as_dict()) == got[0]


def test_the_published_line_keeps_the_field_through_the_api_model():
    from desk.api.models import OcpPublishedLine

    rep = [report("EUR", "1000000", [line("2026-12-22", "800000")])]
    x = pub(run([no_curve("D1|NDF")], rep, [terms("D1|NDF", "T-2")]))[("D1", "EUR")]
    m = OcpPublishedLine(**x)
    assert m.spot_future and m.spot_future["df1"][0]["note"] == "нет кривой EUR: дисконт-фактор 1"
    assert OcpPublishedLine(**{k: v for k, v in x.items() if k != "spot_future"}).spot_future is None


def test_a_line_left_without_a_deal_moves_the_others_if_that_frees_one():
    # L1 is 0 days from A and 1 day from B; L2 only 5 days from A: the greedy pass gives A to L1 and leaves L2
    a = deal("A", dates=(date(2026, 12, 18),))
    b = deal("B", dates=(date(2026, 12, 17),))
    res = S.match([sl(800_000, date(2026, 12, 18)), sl(800_000, date(2026, 12, 25))], [a, b])
    assert [(m.line.value_date, m.deal.position_id) for m in res.matched] == [
        (date(2026, 12, 18), "B"), (date(2026, 12, 25), "A")] and res.unmatched == []
    # what stays: nothing to move, a line stays unmatched as taken; the result does not depend on the order of deals
    one = S.match([sl(800_000, date(2026, 12, 18)), sl(800_000, date(2026, 12, 25))], [a])
    assert [m.deal.position_id for m in one.matched] == ["A"] and [u.why for u in one.unmatched] == ["taken"]
    flip = S.match([sl(800_000, date(2026, 12, 18)), sl(800_000, date(2026, 12, 25))], [b, a])
    assert [(m.line.value_date, m.deal.position_id) for m in flip.matched] == [
        (date(2026, 12, 18), "B"), (date(2026, 12, 25), "A")]


def test_a_leg_the_rate_gave_has_a_wider_tolerance_than_one_the_terms_state():
    stated, by_rate = deal(), S.Deal("P1", "D1", "ndf", "ndf", {"EUR": 800_000.0}, {"EUR": 800_000.0},
                                     (date(2026, 12, 18),), rate_legs=frozenset({"EUR"}))
    off = 800_000 * 1.00005
    assert not S.match([sl(off)], [stated]).matched and S.match([sl(off)], [by_rate]).matched
    assert not S.match([sl(800_000 * 1.0002)], [by_rate]).matched


def test_an_entity_level_row_without_a_book_matches_a_deal_of_either_book():
    units = [{"scope": {"entity": "ENT1"}, "set_id": 21, "valid_date": B, "row_count": 1,
              "file_name": "ocp_20261015.csv", "inbox_file_sha256": "f21"}]
    row = {"entity": "ENT1", "ccy": "EUR", "ocp_ccy": "1000000", "_set_id": 21, "_src": "L2",
           "derivatives_ccy": {"ndf": "0"}, "x_spot_future": [line("2026-12-18", "800000")]}
    d1 = valued("D1|NDF", {"EUR": 790_000.0}, {"EUR": 800_000.0})
    d1["extra"]["legs"] = [{"value_date": "2026-12-21"}]
    d2 = {**valued("D2|NDF", {"EUR": 780_000.0}, {"EUR": 800_000.0}), "book_id": "D2"}
    ts = [terms("D1|NDF", "T-1", quote="RUB", nq=1, vd="2026-12-21", fixing="2026-12-21"),
          {**terms("D2|NDF", "T-2", quote="RUB", nq=1), "book_id": "D2"}]  # D2's deal is on the line's date
    r = O.compute_ocp([d1, d2], BOOKS, B, terms=ts, report_rows=[row], report_units=units, policy=POLICY)
    x = next(x for x in r.published if x["ccy"] == "EUR")
    assert x["book_id"] is None and x["books"] == ["D1", "D2"]
    out = x["spot_future"]["out"]
    assert [(o["position_id"], o["others"]) for o in out] == [("D2|NDF", ["D1|NDF"])]
    assert x["parts"]["base"] == 200_000 and x["parts"]["derivatives"] == 790_000 + 780_000   # both PVs, as before
    parts_sum(x)
