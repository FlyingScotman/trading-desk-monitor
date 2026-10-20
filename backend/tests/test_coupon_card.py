"""R16: what the Desk Monitor's position card reads of a bond's coupon (``couponType``, ``coupon``, ``index``,
``margin``, ``extra.coupon_basis``) through the compat payload, for a fixed bond, a floater projected as index + margin
and a zero; and the pages served for revalidation, so that an update never runs the old page on the new data."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from store_rows import insert_run

from desk.api import create_app
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"
FIXED, FLOATER, ZERO = "RU000A0JXQ44", "RU000A109XR1", "SU26238RMFS4"  # the zero: an OFZ made one
CARD = ("instrType", "couponType", "coupon", "index", "margin")


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


def _payload(store_url, ctx) -> dict[str, dict]:
    out = {o.secid: o for o in value(ctx, [(s, 10.0) for s in (FIXED, FLOATER, ZERO)], position_ids=["A", "B", "C"])}
    t = out[FLOATER].terms
    bare = tuple(dataclasses.replace(c, amount_per_bond=None, rate_pct=None, status="unknown")
                 if c.pay_date > ctx.business_date else c for c in t.coupons)  # no published coupon: projected
    out[FLOATER].terms = dataclasses.replace(t, coupons=bare)
    out[ZERO].terms = dataclasses.replace(out[ZERO].terms, coupon_type="zero", coupons=())
    insert_run(store_url, ctx, [(p, "CORP", out[s]) for p, s in (("A", FIXED), ("B", FLOATER), ("C", ZERO))], None)
    desk = TestClient(create_app(store_url)).get(f"/api/v1/compat/desk?as_of={DATE}").json()
    return {p["id"]: p for p in desk["positions"]}


def test_card_coupon_fields(store_url, ctx):
    pos = _payload(store_url, ctx)
    card = {pid: (*(p.get(k) for k in CARD), p["extra"].get("coupon_basis")) for pid, p in pos.items()}
    assert card["A"] == ("BOND", "fix", 14.4, None, None, "rate")
    b = card["B"]
    assert b[:2] == ("BOND", "float") and b[2] == pytest.approx(14.0 + 1.75)
    assert b[3:] == ("КС", 1.75, "projected")  # the card: «КС + 1,75% (≈15,75%, прогноз)»
    assert card["C"] == ("BOND", "zero", 0.0, None, None, "zero")
    for p in pos.values():  # every key the card reads is there, a number or null, never a "null" string
        assert set(CARD) <= set(p) and all(p[k] != "null" for k in CARD)
        assert p["coupon"] is None or isinstance(p["coupon"], float)


def test_pages_are_revalidated(store_url):
    client = TestClient(create_app(store_url))
    for page in ("/desk-monitor.html", "/desk-util.js", "/"):
        r = client.get(page)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", page
        again = client.get(page, headers={"If-None-Match": r.headers["etag"]})
        assert again.status_code == 304 and again.headers["cache-control"] == "no-cache", page
    assert client.get("/desk-data.js").headers["cache-control"] == "no-store"  # the data: never cached
