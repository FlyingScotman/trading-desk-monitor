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

import pytest
from test_ocp_spot_future_693 import BOOKS, POLICY, UNITS, B, line, no_curve, pub, report, run, terms, valued

from desk.rules import ocp as O
from desk.rules import ocp_spot as S


def cross_terms(**kw):
    """A cross NDF EUR/USD, EUR bought: 800 000 EUR against 880 000 USD."""
    return terms("D1|NDF", "T-7", **kw)


@pytest.mark.parametrize("in_report", [True, False])
def test_mirrored_ndf_terms_and_spot_lines_stay_in_their_own_books(in_report):
    rows, ts, rep, units = [], [], [], []
    for n, (book, sign, side) in enumerate((("D1", 1, "buy"), ("D2", -1, "sell")), start=11):
        pid = f"{book}|NDF"
        row = valued(pid, {"EUR": sign * 790_000, "USD": -sign * 870_000}, {})
        row["extra"]["core"]["trades"] = ["T-7"] if n == 11 else []
        if n == 12:
            row["extra"]["reported"] = {"deal_id": "T-7"}
        rows.append({**row, "book_id": book})
        ts.append({**terms(pid, "T-7", side=side), "book_id": book})
        units.append({**UNITS[0], "scope": {"entity": "ENT1", "book_id": book}, "set_id": n})
        for ccy, nominal in (("EUR", sign * 800_000), ("USD", -sign * 880_000)):
            rep.append({**report(ccy, str(nominal + 200_000), [line("2026-12-18", str(nominal))], book=book),
                        "_set_id": n})

    policy = POLICY if in_report else O.OcpPolicy(report_components=POLICY.report_components - {"ndf"})
    linked = O.terms_by_position(rows, ts)
    deals = {d.book_id: d for d in O.spot_deals(rows, BOOKS, linked, B, policy)}
    published = pub(O.compute_ocp(rows, BOOKS, B, terms=ts, report_rows=rep, report_units=units, policy=policy))
    for book, sign in (("D1", 1), ("D2", -1)):
        assert deals[book].legs == {"EUR": sign * 800_000, "USD": -sign * 880_000}
        for ccy, pv in (("EUR", sign * 790_000), ("USD", -sign * 870_000)):
            x = published[(book, ccy)]
            assert x["spot_future"]["unmatched"] == []
            assert [(o["position_id"], o["amount"]) for o in x["spot_future"]["out"]] == [
                (f"{book}|NDF", deals[book].legs[ccy])]
            assert x["parts"]["base"] == 200_000
            assert x["parts"]["derivatives"] == (pv if in_report else 0)
            assert x["parts"]["outside"] == (0 if in_report else pv)
            assert x["amount"] == 200_000 + pv


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
    assert un["near"] == {"position_id": "D1|NDF", "nominal": 800_000, "source": "terms"}
    assert un["other"] == {"position_id": "D1|NDF", "ccy": "USD", "nominal": -880_000, "source": "terms"}


def test_an_unmatched_amount_names_the_nearest_leg_and_no_other_leg_when_none_has_it():
    rep = [report("EUR", "1000000", [line("2026-12-18", "123456")])]
    x = pub(run([valued("D1|NDF", {"EUR": 790_000.0}, {"EUR": 800_000.0})], rep,
                [terms("D1|NDF", "T-1", quote="RUB", nq=80_000_000)]))[("D1", "EUR")]
    (un,) = x["spot_future"]["unmatched"]
    assert un["why"] == "amount" and un["near"] == {"position_id": "D1|NDF", "nominal": 800_000, "source": "terms"} and "other" not in un
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
    assert u.why == "amount" and u.near == ("D1|NEAR", 810_000.0, "terms") and u.other == ("D1|NEAR", "USD", -800_000.0, "terms")


def test_the_source_of_a_legs_amount_is_named_terms_position_nominal_or_fx_delta():
    # a cross NDF with no terms: the position's nominal (ocp_amount), else its FX delta (a spot equivalent, not a nominal)
    rep = [report("EUR", "1000000", [line("2026-12-18", "123456")])]
    for row, src in ((valued("D1|NDF", {"EUR": 790_000.0}, {"EUR": 800_000.0}), "ocp_amount"),
                     (no_curve("D1|NDF"), "fx_delta")):
        (un,) = pub(run([row], rep))[("D1", "EUR")]["spot_future"]["unmatched"]
        assert un["why"] == "amount" and un["near"]["source"] == src, src
    (un,) = pub(run([no_curve("D1|NDF")], rep, [cross_terms()]))[("D1", "EUR")]["spot_future"]["unmatched"]
    assert un["near"]["source"] == "terms"


def terms_traded(trade_date, trade="T-8"):
    return {**terms("D1|NDF", trade, quote="RUB", nq=80_000_000), "trade_date": trade_date}


def no_line_report():
    rep = [report("EUR", "1000000", None)]
    rep[0].pop("x_spot_future")
    return rep


def test_a_file_without_the_field_says_so_for_deals_the_report_can_hold_and_advises_a_re_read():
    rep = no_line_report()
    # traded before B, or on B (the report of B can hold it)
    for d in ("2026-10-14", B.isoformat()):
        x = pub(run([no_curve("D1|NDF")], rep, [terms_traded(d)]))[("D1", "EUR")]
        assert x["spot_future"]["none"] == {"deals": 1, "before": 1, "after": 0, "unknown": 0, "field": False}, d
        assert x["spot_future"]["out"] == [] and x["spot_future"]["unmatched"] == [] and x["parts"]["base"] == 1_000_000
    # no trade date in the terms: unknown, said
    t = terms_traded("2026-10-16")
    t.pop("trade_date")
    assert pub(run([no_curve("D1|NDF")], rep, [t]))[("D1", "EUR")]["spot_future"]["none"] == {
        "deals": 1, "before": 0, "after": 0, "unknown": 1, "field": False}


def test_a_deal_traded_after_the_reports_date_is_no_reason_for_a_note():
    # the report of B cannot hold it: nothing is said (not a double count)
    x = pub(run([no_curve("D1|NDF")], no_line_report(), [terms_traded("2026-10-16")]))[("D1", "EUR")]
    assert "spot_future" not in x


def test_a_file_with_the_field_in_another_books_row_says_it_without_the_advice_flag():
    # D2's row of the same file has a spot-type line: the adapter gives such lines, D1 has none
    units = [*UNITS, {"scope": {"entity": "ENT1", "book_id": "D2"}, "set_id": 12, "valid_date": B, "row_count": 1,
                      "file_name": "ocp_20261015.csv", "inbox_file_sha256": "f15"}]
    d2 = {**report("EUR", "5", [line("2026-12-18", "123456")], book="D2"), "_set_id": 12}
    r = O.compute_ocp([no_curve("D1|NDF")], BOOKS, B, terms=[terms_traded("2026-10-14")],
                      report_rows=[*no_line_report(), d2], report_units=units, policy=POLICY)
    assert {c["book_id"]: c["spot_field"] for c in r.comparisons} == {"D1": True, "D2": True}
    assert pub(r)[("D1", "EUR")]["spot_future"]["none"] == {"deals": 1, "before": 1, "after": 0, "unknown": 0,
                                                            "field": True}
    # another file (another sha): not the same adapter's answer for this one
    units2 = [UNITS[0], {**units[1], "inbox_file_sha256": "g15"}]
    r = O.compute_ocp([no_curve("D1|NDF")], BOOKS, B, terms=[terms_traded("2026-10-14")],
                      report_rows=[*no_line_report(), d2], report_units=units2, policy=POLICY)
    assert pub(r)[("D1", "EUR")]["spot_future"]["none"]["field"] is False


def test_no_note_without_a_deal_with_a_leg_in_the_currency_and_none_when_the_report_has_lines():
    rep = no_line_report()
    # no deal of the book: nothing to say
    assert "spot_future" not in pub(run([], rep))[("D1", "EUR")]
    # a deal with no leg in EUR
    gbp = {**terms("D1|NDF", "T-9", base="GBP", quote="RUB", nb=1_000, nq=100_000), "trade_date": "2026-10-14"}
    assert "spot_future" not in pub(run([no_curve("D1|NDF", ccy="GBP", amount=1_000.0)], rep, [gbp]))[("D1", "EUR")]
    # the report has a line: the section is about it, there is no «none»
    rep = [report("EUR", "1000000", [line("2026-12-18", "800000")])]
    x = pub(run([no_curve("D1|NDF")], rep, [terms_traded("2026-10-14")]))[("D1", "EUR")]
    assert "none" not in x["spot_future"] and len(x["spot_future"]["out"]) == 1
