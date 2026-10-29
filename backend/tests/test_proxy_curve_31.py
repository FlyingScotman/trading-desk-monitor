"""TDM#31: a date without the RUONIA curve. A RUB bond normally on RUONIA OIS is discounted on the KBD (its spread and
risk on ``RUB OFZ``, the proxy): its row says so (``extra.curve_proxy``, ``publish.tdm_run.curve_proxy``), an OFZ on
the KBD by rule does not. A position without PnL for a change of discount curve (``discount_curve_changed`` of a
stored run) is shown with its curves, not as a bare code (``pnl.reasons.no_pnl_reason_text``).

On the 1.0 fixture of 15.10.2026 with its RUONIA OIS quotes dropped (``test_rates_inputs_r14.cache_without``).
Synthetic data only."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_rates_inputs_r14 import cache_without

from desk.api import compat, concepts_run, pnl_period
from desk.marketdata.adapter import bond_currency
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value
from desk.pnl import reasons
from desk.publish import tdm_run
from desk.valuation.bonds import OFZ_KBD_CLASSES

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"
Q = 1000.0
CHANGED = "RUB RUONIA -> RUB OFZ"


def _rows(ctx):
    outs = value(ctx, [(s, Q) for s in sorted(ctx.static.bonds)])
    return [(o, tdm_run.position_row(ctx, "B", o.secid, o, "x")) for o in outs if o.valuation is not None]


@pytest.fixture(scope="module")
def no_ois(tmp_path_factory):
    return _rows(prepare(SnapshotDir(cache_without(tmp_path_factory.mktemp("no_ois"), "ruonia_ois")), DATE))


@pytest.fixture(scope="module")
def with_ois():
    return _rows(prepare(SnapshotDir(FIX), DATE))


def test_a_date_without_ruonia_marks_the_bonds_on_the_proxy(no_ois):
    marked = {o.secid for o, r in no_ois if "curve_proxy" in r["extra"]}
    rub = [(o, r) for o, r in no_ois if bond_currency(o.terms) == "RUB"]
    assert marked
    for o, r in rub:
        on_rule = o.terms.bond_class in OFZ_KBD_CLASSES
        if on_rule:  # an OFZ on the KBD every day: not a proxy
            assert o.secid not in marked
        elif o.secid in marked:
            assert r["extra"]["curve_proxy"] == {"curve": "RUB OFZ", "instead_of": "RUB RUONIA",
                                                 "reason": "no_ruonia_curve", "text": reasons.PROXY_CURVE_TEXT}
            assert r["spread_curve"] in ("RUB OFZ", None) and "RUB OFZ" in r["risk"]
    # every RUB bond not on the KBD by rule whose discount or risk is on RUB OFZ is marked: core's and the ladder's
    expected = {o.secid for o, r in rub if o.terms.bond_class not in OFZ_KBD_CLASSES and "RUB OFZ" in r["risk"]
                and (o.valuation.discount_curve_id in ("RUB_KBD", None))}
    assert marked == expected
    ruonia = {o.secid for o, _ in rub if (o.terms.floater_formula or {}).get("benchmark") == "RUONIA"}
    assert ruonia and ruonia <= marked, "a RUONIA floater the ladder values on the KBD is marked too"
    assert reasons.PROXY_CURVE_TEXT == "на прокси-кривой RUB OFZ: кривой RUONIA за дату нет"


def test_a_date_with_ruonia_marks_nothing(with_ois):
    assert not [o.secid for o, r in with_ois if "curve_proxy" in r["extra"]]


def test_a_change_of_discount_curve_is_said_with_its_curves():
    assert reasons.no_pnl_text("discount_curve_changed") == "смена кривой дисконтирования"
    text = reasons.no_pnl_reason_text("discount_curve_changed", CHANGED)
    assert text.startswith("смена кривой дисконтирования: вчера RUB RUONIA, сегодня RUB OFZ")
    assert "сегодня кривой RUONIA не было" in text and "прокси-кривой RUB OFZ" in text
    back = reasons.no_pnl_reason_text("discount_curve_changed", "RUB OFZ -> RUB RUONIA")
    assert back.startswith("смена кривой дисконтирования: вчера RUB OFZ, сегодня RUB RUONIA")
    assert "вчера кривой RUONIA не было" in back
    # another code: its text alone, the detail stays in the run log
    assert reasons.no_pnl_reason_text("quantity_changed", "1 -> 2") == reasons.NO_PNL_TEXT["quantity_changed"]
    assert reasons.no_pnl_reason_text("discount_curve_changed") == "смена кривой дисконтирования"


def test_the_pages_get_the_text_of_a_stored_row():
    stored = {"reason": "discount_curve_changed", "detail": CHANGED}
    want = reasons.no_pnl_reason_text("discount_curve_changed", CHANGED)
    assert concepts_run.no_pnl(*concepts_run._no_pnl_code(stored, None))["reasonText"] == want
    assert compat.no_pnl("discount_curve_changed", CHANGED)["reasonText"] == want
    assert pnl_period.no_pnl_why(stored) == want and pnl_period.no_pnl_why({}) == "причина не указана"
    assert concepts_run.no_pnl(*concepts_run._no_pnl_code(None, None))["reasonText"] == \
        reasons.NO_PNL_TEXT[reasons.RUN_NO_PREV_MARKET]
