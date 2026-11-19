"""TDM#807: a report line of a spot type with a future date of a cross NDF (EUR/USD) and the screen's honesty about it.

The match compares a line with the leg of the deal in the line's own currency (``rules.ocp_spot.match``), not with the
USD leg: a line in EUR of a cross NDF whose EUR leg is the nominal is taken out of the base whatever its USD leg is. A line
that is not matched by its amount says what it was compared with (the nearest leg in its currency) and whether its
amount is that of a leg of ANOTHER currency (a line in the wrong currency block), never matching on it. A date on which
the report has no line of this kind at all, while the book has deals the line could be matched with, says so, and how many
of the deals were traded before the report's date (the report can hold them) and after it (it cannot). Synthetic rows,
invented numbers."""

from __future__ import annotations

from datetime import date

from test_ocp_spot_future_693 import B, line, no_curve, pub, report, run, terms, valued

from desk.rules import ocp_spot as S


def cross_terms(**kw):
    """A cross NDF EUR/USD, EUR bought: 800 000 EUR against 880 000 USD."""
    return terms("D1|NDF", "T-7", **kw)


def test_a_line_in_eur_of_a_cross_ndf_is_matched_on_the_eur_leg_whatever_the_usd_leg_is():
    # the USD leg is 880 000, the EUR leg 800 000; the line is in the EUR block and states the EUR nominal
    rep = [report("EUR", "1000000", [line("2026-12-18", "800000")])]
    r = run([no_curve("D1|NDF")], rep, [cross_terms()])
    x = pub(r)[("D1", "EUR")]
    assert [(o["position_id"], o["amount"]) for o in x["spot_future"]["out"]] == [("D1|NDF", 800_000)]
    assert x["spot_future"]["unmatched"] == [] and x["parts"]["base"] == 200_000
    assert "spot_future_unmatched" not in {g["code"] for g in r.gaps}
    # the same deal with the quote amount rounded as a rate gives it (the EUR leg is stated): the EUR line matches too
    r = run([no_curve("D1|NDF")], rep, [cross_terms(nq=880_000.4)])
    assert pub(r)[("D1", "EUR")]["spot_future"]["out"][0]["position_id"] == "D1|NDF"


def test_a_line_in_eur_that_states_the_usd_amount_is_not_matched_and_the_screen_says_so():
    # the report's EUR block holds a line of 880 000: the USD leg's amount, not the EUR leg's (800 000)
    rep = [report("EUR", "1000000", [line("2026-12-18", "880000")])]
    r = run([no_curve("D1|NDF")], rep, [cross_terms()])
    x = pub(r)[("D1", "EUR")]
    assert x["spot_future"]["out"] == [] and x["parts"]["base"] == 1_000_000
    (un,) = x["spot_future"]["unmatched"]
    assert un["why"] == "amount"
    assert un["near"] == {"position_id": "D1|NDF", "nominal": 800_000}
    assert un["other"] == {"position_id": "D1|NDF", "ccy": "USD", "nominal": -880_000}


def test_an_unmatched_amount_names_the_nearest_leg_and_no_other_leg_when_none_has_it():
    rep = [report("EUR", "1000000", [line("2026-12-18", "123456")])]
    x = pub(run([valued("D1|NDF", {"EUR": 790_000.0}, {"EUR": 800_000.0})], rep,
                [terms("D1|NDF", "T-1", quote="RUB", nq=80_000_000)]))[("D1", "EUR")]
    (un,) = x["spot_future"]["unmatched"]
    assert un["why"] == "amount" and un["near"] == {"position_id": "D1|NDF", "nominal": 800_000} and "other" not in un
    # the reasons that are not about the amount carry no comparison
    rep = [report("EUR", "1000000", [line("2026-12-28", "800000")])]
    x = pub(run([valued("D1|NDF", {"EUR": 790_000.0}, {"EUR": 800_000.0})], rep,
                [terms("D1|NDF", "T-1", quote="RUB", nq=80_000_000)]))[("D1", "EUR")]
    (un,) = x["spot_future"]["unmatched"]
    assert un["why"] == "date" and "near" not in un and "other" not in un


def test_the_nearest_leg_is_the_one_of_the_nearest_amount_among_the_deals_of_the_book():
    far = valued("D1|FAR", {"EUR": 1.0}, {"EUR": 5_000_000.0})
    near = valued("D1|NEAR", {"EUR": 1.0}, {"EUR": 810_000.0})
    r = S.match([S.SpotLine("EUR", date(2026, 12, 18), 800_000.0, "D1")], [
        S.Deal("D1|FAR", "D1", "ndf", "ndf", {"EUR": 5_000_000.0}, {}, (date(2026, 12, 18),)),
        S.Deal("D1|NEAR", "D1", "ndf", "ndf", {"EUR": 810_000.0, "USD": -800_000.0}, {}, (date(2026, 12, 18),))])
    assert far and near
    (u,) = r.unmatched
    assert u.why == "amount" and u.near == ("D1|NEAR", 810_000.0) and u.other == ("D1|NEAR", "USD", -800_000.0)


def terms_traded(trade_date, trade="T-8"):
    return {**terms("D1|NDF", trade, quote="RUB", nq=80_000_000), "trade_date": trade_date}


def test_a_date_with_no_line_says_how_many_deals_were_traded_before_and_after_the_reports_date():
    rep = [report("EUR", "1000000", None)]
    rep[0].pop("x_spot_future")
    # traded before B (the report of B can hold it)
    x = pub(run([no_curve("D1|NDF")], rep, [terms_traded("2026-10-14")]))[("D1", "EUR")]
    assert x["spot_future"]["none"] == {"deals": 1, "before": 1, "after": 0, "unknown": 0}
    assert x["spot_future"]["out"] == [] and x["spot_future"]["unmatched"] == [] and x["parts"]["base"] == 1_000_000
    # traded on B: in the report of B
    x = pub(run([no_curve("D1|NDF")], rep, [terms_traded(B.isoformat())]))[("D1", "EUR")]
    assert x["spot_future"]["none"]["before"] == 1
    # traded after B: the report cannot hold it
    x = pub(run([no_curve("D1|NDF")], rep, [terms_traded("2026-10-16")]))[("D1", "EUR")]
    assert x["spot_future"]["none"] == {"deals": 1, "before": 0, "after": 1, "unknown": 0}
    # no trade date in the terms
    t = terms_traded("2026-10-16")
    t.pop("trade_date")
    assert pub(run([no_curve("D1|NDF")], rep, [t]))[("D1", "EUR")]["spot_future"]["none"] == {
        "deals": 1, "before": 0, "after": 0, "unknown": 1}


def test_no_note_without_a_deal_with_a_leg_in_the_currency_and_none_when_the_report_has_lines():
    rep = [report("EUR", "1000000", None)]
    rep[0].pop("x_spot_future")
    # no deal of the book: nothing to say
    assert "spot_future" not in pub(run([], rep))[("D1", "EUR")]
    # a deal with no leg in EUR
    gbp = {**terms("D1|NDF", "T-9", base="GBP", quote="RUB", nb=1_000, nq=100_000), "trade_date": "2026-10-14"}
    assert "spot_future" not in pub(run([no_curve("D1|NDF", ccy="GBP", amount=1_000.0)], rep, [gbp]))[("D1", "EUR")]
    # the report has a line: the section is about it, there is no «none»
    rep = [report("EUR", "1000000", [line("2026-12-18", "800000")])]
    x = pub(run([no_curve("D1|NDF")], rep, [terms_traded("2026-10-14")]))[("D1", "EUR")]
    assert "none" not in x["spot_future"] and len(x["spot_future"]["out"]) == 1
