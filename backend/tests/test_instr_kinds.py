"""One rule for the instrument kind (TDM#345): ``api/concepts.instr_kind`` (the currency x type table),
``publish/summary.kind_of`` (the summaries' group and kind) and the monitors' ``lib/real.js`` ``kindOf`` /
``lib/instr.js`` ``INSTR.kindOf`` agree on every ``instr_type``. The cases are shared with the JS side
(``tests/fixtures/instr_kinds.json``, checked again by ``tests/js/instr_kinds.test.js``): the group of ``kind_of`` is the
group of the table's kind (but for an unmapped bond: BOND_NA, OTH), an OTC row without any product group is DER
(``kind_of``'s DER OTC, the table's ``OTC``), never UNCLASSIFIED in OTH. A REPO (product group ``repo``, valued or
not) is ``REPO`` in the group MM, money market and funding, both ways (the owner's decision in TDM#361). An OTC row with
a product group the table does not know (a new one) stays UNCLASSIFIED: a known difference with ``kind_of``
(``knownMismatch``, TDM#367)."""
import json
from pathlib import Path

import pytest

from desk.api import concepts, concepts_run
from desk.publish import summary

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "instr_kinds.json"
DOC = json.loads(FIXTURE.read_text(encoding="utf-8"))
CASES = DOC["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_three_classifications_agree(case):
    fact = case["fact"]
    assert concepts.instr_kind(fact) == case["table"]
    group = concepts.KIND_GROUP[case["table"]]
    if case.get("unvalued"):
        return
    assert summary.kind_of(fact) == concepts.kind_of(fact) == tuple(case["kindOf"])
    if case.get("bondNa"):  # an unmapped bond: OTH in the table, its issuer's group in kind_of
        assert group == "OTH" and case["kindOf"][0] == "CORP"
    elif case.get("knownMismatch"):
        # an OTC row with a product group the table does not know (a new one): UNCLASSIFIED in the table, DER OTC in
        # kind_of -- a known difference (TDM#367); a REPO is not one (MM both ways, TDM#361)
        assert case["knownMismatch"] == "#367" and fact["instr_type"] == "OTC"
        assert concepts.product_group(fact["extra"]["reported"]) not in summary.MONEY_MARKET
        assert (case["table"], group, tuple(case["kindOf"])) == ("UNCLASSIFIED", "OTH", ("DER", "OTC"))
    else:
        assert group == case["kindOf"][0], "the table's group is kind_of's"


def test_every_instr_type_and_product_group_is_a_case():
    seen = {c["fact"].get("instr_type") for c in CASES if not c.get("unvalued")}
    assert seen == set(DOC["instrTypes"])
    assert set(concepts.LEGACY_KIND) | set(summary.LEGACY_DER) | {"BOND", "FX", "OTC"} <= seen
    pgs = {concepts.product_group((c["fact"].get("extra") or {}).get("reported")) for c in CASES}
    assert set(concepts.PRODUCT_KIND) <= pgs
    assert {c["table"] for c in CASES} >= {"OTC", "UNCLASSIFIED", "CCS", "BOND_NA", "REPO"}
    # a REPO is money market and funding both ways, valued or not (TDM#361)
    assert {c["table"] for c in CASES if c.get("unvalued") and "repo" in c["name"]} == {"REPO"}
    for pg, kind in summary.MONEY_MARKET.items():
        assert concepts.PRODUCT_KIND[pg] == kind and concepts.KIND_GROUP[kind] == "MM"
        assert summary.kind_of({"instr_type": "OTC", "extra": {"reported": {"x_product_group": pg}}}) == ("MM", kind)
    # every legacy swap is in DER both ways (TDM#336), its kind named apart: XCCY is the table's CCS
    for it, kind in concepts.LEGACY_KIND.items():
        assert concepts.KIND_GROUP[kind] == "DER" == summary.kind_of({"instr_type": it})[0]


@pytest.mark.parametrize("case", [c for c in CASES if c.get("unvalued")], ids=lambda c: c["name"])
def test_an_unvalued_row_is_what_the_server_sends(case):
    rep = case["fact"]["extra"]["reported"]
    r = {"position_id": "P", "book_id": "B", "secid": None, "reason": "x", "quantity": None}
    row = concepts_run._not_valued(r, {}, rep, None)
    want = case["row"]
    assert (row["assetClass"], row["segment"], row["instrType"], row["notValued"]) == (
        want["assetClass"], want["segment"], None, True)
    assert row["extra"]["reported"] == want["extra"]["reported"]


def test_the_js_rows_are_the_facts():
    """A valued case's JS row is its fact under the server's names (concepts_run: instrType, segment, ...)."""
    for c in CASES:
        if c.get("unvalued"):
            continue
        f, row = c["fact"], c["row"]
        assert (row["instrType"], row["segment"], row["assetClass"], row["couponType"]) == (
            f.get("instr_type"), f.get("segment"), f.get("asset_class"), f.get("coupon_type")), c["name"]
        assert row.get("extra") == f.get("extra"), c["name"]
