"""Contract ``market_snapshot/1.1`` (R9 continued: HS1-HS13 on the hub, D2, D3, D7, D9 and HS2 on the desk), on the
hub's 1.1 golden fixtures (``fixtures/mdhub_v1_1``) and synthetic snapshots built from them: the issuer's short name,
group, state ownership and sector; ratings on one desk scale; the payment-day face of ``face_value_date``; the Latin
name; alive bonds MOEX no longer trades; the instruments export; a full ``desk eod`` on the fixtures."""

from __future__ import annotations

import copy
import dataclasses
import gzip
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from desk import cli
from desk.api import compat
from desk.inbox import static_index
from desk.marketdata import ratings
from desk.marketdata.bundle import SnapshotDir, parse_bundle
from desk.orchestrator import prepare, value
from desk.publish import tdm_run
from desk.report import instruments
from desk.valuation.bonds import Rejected, face_on, fixed_schedule
from desk.valuation.fallback import CarriedSpread

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1_1"
FIX_10 = Path(__file__).parent / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"
STATIC = "3a1d780c5364d29368cf3a7d93efb1ed5ab3b2b26c6bae14f78b34cec4355680"
MARKET = "36f706803b3d6e8f3c62f22564651d97e635f782d7c5cb00bf8cd24c6384d15c"
AMORTIZER = "RU000A107KV4"  # face 800 after the 50 redeemed on the business date (face_value_date 2026-10-15)
RZD, VTB, ALFA = "RU000A0JXQ44", "RU000A1034P7", "RU000A100GW5"
SIBN = "RU000A0JWRE5"  # Gazprom Neft BO-01, due 2046: alive, MOEX no longer trades it (the audit's "SIBN ...")


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


def _doc(sid: str) -> dict:
    return json.loads(gzip.decompress((FIX / f"{sid}.json.gz").read_bytes()))


def _write(folder: Path, doc: dict) -> str:
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    sid = hashlib.sha256(raw).hexdigest()
    (folder / f"{sid}.json.gz").write_bytes(gzip.compress(raw, mtime=0))
    return sid


def _pair(folder: Path, static: dict) -> str:
    """A static snapshot and the golden market re-pointed at it, written to ``folder``; the static id."""
    sid = _write(folder, static)
    market = _doc(MARKET)
    market["static_snapshot_id"] = sid
    _write(folder, market)
    return sid


# ---- the vendored fixtures

def test_fixtures_are_the_hubs_golden_bundles():
    idx = json.loads((FIX / "golden_index.json").read_text(encoding="utf-8"))
    assert idx["schema_version"] == "market_snapshot/1.1"
    assert {f["snapshot_id"] for f in idx["fixtures"].values()} == {STATIC, MARKET}
    for sid in (STATIC, MARKET):
        body = (FIX / f"{sid}.json.gz").read_bytes()
        assert hashlib.sha256(gzip.decompress(body)).hexdigest() == sid
        assert parse_bundle(body, sid).doc["schema_version"] == "market_snapshot/1.1"
    assert AMORTIZER in idx["instruments"]


# ---- D2, D3: issuer name, group, state ownership, sector

def test_issuer_reference_from_the_hub(ctx):
    ref = {s: tdm_run.issuer_reference(ctx.static, t)[1] for s, t in ctx.static.bonds.items()}
    vtb, rzd, alfa = ref[VTB], ref[RZD], ref[ALFA]
    assert (vtb["name"], vtb["issuer_group"], vtb["sector"], vtb["is_quasi"]) == ("Банк ВТБ", "ВТБ", "Банки", False)
    assert (rzd["name"], rzd["issuer_group"], rzd["sector"], rzd["is_quasi"]) == ("РЖД", "РЖД", "Транспорт", True)
    assert (rzd["group_id"], rzd["sector_source"]) == ("rzd", "curated")
    assert (alfa["name"], alfa["issuer_group"]) == ("Альфа-Банк", "Альфа-Банк")  # no group: the issuer itself
    assert ref[AMORTIZER]["sector"] == "нет данных"  # the hub has no sector for СибАвтоТранс
    assert ref["SU26238RMFS4"]["sector"] == "Суверенные" and ref["SU26238RMFS4"]["is_gov"]
    assert ref["RU000A109XR1"]["sector"] == "Химия и удобрения"
    assert all(r["rating"] == "нет данных" for r in ref.values())  # the ratings dataset is empty (Cbonds 401)
    assert tdm_run.sector_label("unknown_code") == "unknown_code" and tdm_run.sector_label(None) == "нет данных"


def test_issuer_display_fallback_chain(ctx):
    t = ctx.static.bonds[VTB]
    iss = ctx.static.issuer_of(t)
    view = copy.copy(ctx.static)
    view.issuers = {**ctx.static.issuers, iss.issuer_id: dataclasses.replace(iss, short_name=None)}
    assert tdm_run.issuer_reference(view, t)[1]["name"] == "Банк ВТБ"  # the legal name, cleaned
    view.issuers[iss.issuer_id] = dataclasses.replace(iss, short_name=None, name=None)
    key, rec = tdm_run.issuer_reference(view, t)
    assert rec["name"] == rec["issuer_group"] == "ВТБ" and key == f"issuer:{iss.issuer_id}"  # the group's


def test_a_1_0_snapshot_keeps_its_display():
    ctx10 = prepare(SnapshotDir(FIX_10), DATE)
    rec = tdm_run.issuer_reference(ctx10.static, ctx10.static.bonds[VTB])[1]
    assert (rec["name"], rec["issuer_group"], rec["sector"], rec["rating"], rec["is_quasi"]) == \
        ("Банк ВТБ", "Банк ВТБ", "нет данных", "нет данных", False)
    assert ctx10.static.ratings is None


# ---- D9: ratings

@pytest.mark.parametrize("verbatim, grade", [
    ("ruAAA", "AAA"), ("AAA(RU)", "AAA"), ("AAA.ru", "AAA"), ("AAA|ru|", "AAA"), ("ruA-", "A-"), ("BBB-(RU)", "BBB-"),
    ("AA+(rus)", "AA+"), ("Aa1.ru", "AA+"), ("Baa3.ru", "BBB-"), ("ruCCC", "CCC"), ("ruRD", "RD"), ("ruD", "D"),
    ("Withdrawn", None), ("", None), (None, None),
])
def test_desk_grade(verbatim, grade):
    assert ratings.desk_grade(verbatim) == grade


def test_rating_order():
    got = sorted(["без рейтинга", "ruBBB", "AAA", "нет данных", "A+", "AA-(RU)", "x"], key=ratings.rating_rank)
    assert got == ["AAA", "AA-(RU)", "A+", "ruBBB", "x", "без рейтинга", "нет данных"]
    assert compat.RATINGS[:4] == ["AAA", "AA+", "AA", "AA-"] and compat.RATINGS[-1] == "D"


def _rating(scope, agency, rating, *, secid=None, issuer_id=None, scale="", action=None, d="2026-09-01"):
    return {"scope": scope, "secid": secid, "issuer_id": issuer_id, "agency": agency, "scale": scale, "rating": rating,
            "outlook": None, "watch": None, "rating_date": d, "action": action, "source": "cbonds"}


@pytest.fixture(scope="module")
def rated(tmp_path_factory):
    doc = _doc(STATIC)
    doc["data"]["ratings"] = [
        _rating("issuer", "АКРА", "AAA(RU)", issuer_id=269),
        _rating("issue", "Эксперт РА", "ruA", secid=VTB),  # the subordinated issue: its own rating wins
        _rating("issuer", "НКР", "AA+.ru", issuer_id=465),
        _rating("issuer", "НРА", "AAA|ru|", issuer_id=465, action="withdrawn"),  # withdrawn: not used
        _rating("issuer", "Fitch", "BBB-", issuer_id=1013, scale="international scale"),  # not national
        _rating("issue", "НРА", "AA-|ru|", secid="RU000A109XR1"),
        _rating("issue", "Эксперт РА", "ruA+", secid="RU000A10GCT1", d="2026-08-01"),
        _rating("issue", "АКРА", "A(RU)", secid="RU000A10GCT1"),
        _rating("issuer", "Эксперт РА", "ruAA", issuer_id=712, scale="национальная шкала РФ"),
    ]
    folder = tmp_path_factory.mktemp("rated")
    _pair(folder, doc)
    return prepare(SnapshotDir(folder), DATE)


def test_position_rating_issue_else_issuer(rated):
    pr = {s: ratings.position_rating(rated.static, t) for s, t in rated.static.bonds.items()}
    assert (pr[VTB].grade, pr[VTB].agency, pr[VTB].rating, pr[VTB].scope) == ("A", "Эксперт РА", "ruA", "issue")
    assert (pr[ALFA].grade, pr[ALFA].agency) == ("AA+", "НКР")
    assert pr["RU000A109XR1"].grade == "AA-" and pr["RU000A10BRM5"].grade == "без рейтинга"  # Fitch international
    assert (pr["RU000A10GCT1"].grade, pr["RU000A10GCT1"].agency) == ("A+", "Эксперт РА")  # the better of two
    assert pr[RZD].grade == "AA" and pr["SU26238RMFS4"].grade == "без рейтинга"  # nothing inferred for the sovereign


def test_ratings_in_the_run_and_the_desk_order(rated):
    outs = value(rated, [(s, 10.0) for s in sorted(rated.static.bonds)])
    c = tdm_run.run_content(rated, [(f"P{i}", "CORP", o) for i, o in enumerate(outs)], None, {})
    rows = {r["instrument_id"]: r for r in c.rows}
    issuers = {i["issuer"]: i for i in c.reference["issuers"]}
    assert rows[VTB]["rating"] == "A" and rows[VTB]["extra"]["rating"] == {
        "agency": "Эксперт РА", "rating": "ruA", "scope": "issue", "date": "2026-09-01"}
    assert issuers[rows[VTB]["issuer"]]["rating"] == "AAA"  # the issuer's own
    assert issuers[rows["RU000A109XR1"]["issuer"]]["rating"] == "AA-"  # no issuer rating: the best of its bonds'
    assert issuers[rows["SU26238RMFS4"]["issuer"]]["rating"] == "без рейтинга"
    assert "rating" not in rows["SU26238RMFS4"]["extra"]


# ---- D7: the face on a payment day (HS11)

def test_face_on_follows_face_value_date(ctx):
    t = ctx.static.bonds[AMORTIZER]
    assert (t.face_value, t.face_value_date) == (800, date(2026, 10, 15))  # already after today's 50
    assert face_on(t, date(2026, 10, 15)) == 800 and face_on(t, date(2026, 10, 14)) == 850
    assert face_on(t, date(2027, 1, 14)) == 725 and face_on(t, date(2026, 7, 16)) == 850
    s = fixed_schedule(t, date(2026, 10, 15))
    assert s.current_face == 800.0  # the future redemptions (800) add up
    before = dataclasses.replace(t, face_value=850, face_value_date=date(2026, 7, 16))  # built before the deduction
    assert face_on(before, date(2026, 10, 15)) == 800 and face_on(before, date(2026, 10, 14)) == 850
    # without face_value_date (a 1.0 snapshot) the observed rule stays: the static face is before today's redemption
    old = dataclasses.replace(t, face_value_date=None)
    assert face_on(old, date(2026, 10, 15)) == 750
    with pytest.raises(Rejected, match="redemption_incomplete"):
        fixed_schedule(old, date(2026, 10, 15))


def test_payment_day_amortizer_is_model_priced_on_its_face(ctx):
    carried = {AMORTIZER: CarriedSpread(AMORTIZER, 300.0, ctx.business_date - timedelta(days=1), "test")}
    o = value(ctx, [(AMORTIZER, 10.0)], carried_spreads=carried)[0]
    assert o.reason == "no_quote" and o.valuation.quality == "model_price"
    assert o.valuation.face_rub == pytest.approx(800.0)
    row = tdm_run.run_content(ctx, [("P1", "CORP", o)], None, {}).rows[0]
    assert row["notional"] == pytest.approx(8000.0)


# ---- HS12, HS7: Latin name, frequency from the schedule

def test_lat_name_and_frequency_on_the_row(ctx):
    o = value(ctx, [(RZD, 1.0)])[0]
    t = ctx.static.bonds[RZD]
    row = tdm_run.run_content(ctx, [("P1", "CORP", o)], None, {}).rows[0]
    assert row["extra"]["lat_name"] == "RZhD BO-001P-02R" and row["name"] == t.name
    plain = dataclasses.replace(t, coupon_frequency=None, coupons=())  # no MOEX frequency, no schedule of ours
    o.terms = plain
    row = tdm_run.run_content(ctx, [("P1", "CORP", o)], None, {}).rows[0]
    assert row["coupon_freq"] == 2 and row["extra"]["coupon_freq_source"] == "schedule"  # the hub's


# ---- HS2: alive bonds MOEX no longer trades

def _with_not_traded(folder: Path) -> str:
    """The golden static plus SIBN: RZD's terms under another ISIN, status delisted, label not_traded, no board."""
    doc = _doc(STATIC)
    d = doc["data"]
    raw = copy.deepcopy(next(i for i in d["instruments"] if i["secid"] == RZD))
    raw.update(secid=SIBN, isin=SIBN, status="delisted", boards=[], primary_board=None, lat_name="Gazprom Neft BO-1",
               labels=[*raw["labels"], "not_traded"], name="Газпром нефть БО-01", short_name="ГазпромН01",
               regnumber="4B02-01-00146-A")
    d["instruments"].append(raw)
    for ds in ("coupon_periods", "redemptions", "offers"):
        d[ds] += [{**r, "secid": SIBN} for r in d[ds] if r["secid"] == RZD]
    d["id_map"].append({"isin": SIBN, "isin_shared_with": [], "regnumber": raw["regnumber"], "secid": SIBN})
    return _pair(folder, doc)


def test_a_not_traded_bond_resolves_and_is_valued(tmp_path):
    from desk.store import PositionInputs
    from desk.valuation.positions import merge, plan

    _with_not_traded(tmp_path)
    ctx = prepare(SnapshotDir(tmp_path), DATE)
    rows = [("B1", "CORP", None, 10.0)]
    inp = PositionInputs(rows, [], {"CORP": date(2026, 10, 15)}, datetime.now(UTC), "0" * 64, {
        "B1": {"asset_class": "bond", "ccy": "RUB", "source_instrument_name": "SIBN 2046", "isin": SIBN,
               "face_outstanding_ccy": 10000.0, "mv_rub": 1e4, "x": {"x_issuer": 'ПАО "Газпром нефть"'}}}, {})
    p = plan(ctx, inp)
    assert p.requests and p.requests[0][0] == SIBN  # by ISIN against instruments, no longer «не сопоставлено»
    positions = merge(p, value(ctx, p.requests, position_ids=p.position_ids))
    o = positions[0][2]
    assert o.reason == "not_traded" and "no market quote" in o.detail
    assert o.valuation is not None and o.valuation.quality in ("model_price", "par_proxy")
    assert any(a.startswith("not exact: not_traded") for a in o.valuation.assumptions)
    c = tdm_run.run_content(ctx, positions, None, {}, inp)
    row = c.rows[0]
    assert row["name"] == "Газпром нефть БО-01" and row["instr_type"] == "BOND" and row["issuer"] == 'ОАО "РЖД"'
    assert row["extra"]["not_exact_reason"] == "not_traded"
    # with a carried spread (a later run) it is a model price, not a placeholder
    carried = {SIBN: CarriedSpread(SIBN, 150.0, ctx.business_date - timedelta(days=1), "test")}
    o = value(ctx, [(SIBN, 10.0)], carried_spreads=carried)[0]
    assert (o.reason, o.valuation.quality) == ("not_traded", "model_price")


def test_the_unmapped_list_shrinks(tmp_path):
    rows = [(None, {"asset_class": "bond", "isin": SIBN}), (None, {"asset_class": "bond", "isin": "RU000A0JWRF2"})]
    before = static_index.unmapped_bonds(FIX, rows, date(2026, 10, 15))
    assert before["count"] == 2
    _with_not_traded(tmp_path)
    after = static_index.unmapped_bonds(tmp_path, rows, date(2026, 10, 15))
    assert after["count"] == 1 and after["isins"] == [{"isin": "RU000A0JWRF2", "reason": "not_in_static"}]


# ---- the instruments export

def test_instruments_export_new_columns():
    static, market = instruments.find_snapshots(FIX, DATE)
    ex = instruments.build(static, market)
    r = {x["secid"]: x for x in ex.rows}
    assert len(ex.rows) == 11 and all(x["lat_name"] for x in ex.rows)
    assert (r[VTB]["issuer_group"], r[VTB]["mon_issuer_group"], r[VTB]["mon_issuer"]) == ("ВТБ", "ВТБ", "Банк ВТБ")
    assert (r[RZD]["issuer_is_state_owned"], r[RZD]["sector_source"], r[RZD]["mon_sector"]) == (True, "curated",
                                                                                                 "Транспорт")
    assert r[AMORTIZER]["face_value_date"] == date(2026, 10, 15) and r[RZD]["coupon_frequency_schedule"] == 2
    assert r[VTB]["rating"] is None and r[VTB]["mon_rating"] == "нет данных" and r[VTB]["okved"] is None
    assert r[AMORTIZER]["core_static"] == "ok"  # D7: the payment-day face adds up
    s = instruments.summary(ex.rows)["fill"]["_total"]
    assert (s["lat_name"], s["issuer_group"], s["face_value_date"], s["rating"], s["okved"]) == (11, 2, 10, 0, 0)
    assert s["coupon_frequency_schedule"] == 11 and s["sector_source"] == 10
    keys = [k for k, _ in instruments.COLUMNS]
    assert all(set(keys) <= set(x) for x in ex.rows)
    assert "issuer_group" in instruments.summary_text(instruments.summary(ex.rows))


def test_instruments_export_not_traded_flag(tmp_path):
    _with_not_traded(tmp_path)
    static, market = instruments.find_snapshots(tmp_path, DATE)
    r = {x["secid"]: x for x in instruments.build(static, market).rows}
    assert r[SIBN]["status"] == "delisted" and "not_traded" in r[SIBN]["dq_flags"]
    assert "not_traded" not in r[RZD]["dq_flags"]


# ---- a full desk eod on the 1.1 fixtures, and what the pages get

def test_desk_eod_on_the_1_1_fixtures(store_url, tmp_path, monkeypatch, capsys, ctx):
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    monkeypatch.setenv("DESK_STORE_URL", store_url)
    csv = tmp_path / "book.csv"
    csv.write_text("position_id,book_id,secid,quantity_bonds\n" + "".join(
        f"P{i:02d},{'OFZ' if s.startswith('SU') else 'CORP'},{s},1000\n" for i, s in enumerate(sorted(ctx.static.bonds))),
        encoding="utf-8")
    assert cli.main(["eod", "--date", DATE, "--snapshots", str(FIX), "--book", str(csv), "--report-dir", ""]) == 0
    assert "desk run 1: complete" in capsys.readouterr().out
    with psycopg.connect(store_url, row_factory=dict_row, autocommit=True) as con:
        desk = compat.assemble(con, compat.resolve_run(con))
    pos = {p["instrumentId"]: p for p in desk["positions"]}
    assert len(pos) == 11 and not any(p.get("notValued") for p in desk["positions"])
    assert (pos[VTB]["issuer"], pos[VTB]["issuerGroup"], pos[VTB]["sector"]) == ("Банк ВТБ", "ВТБ", "Банки")
    assert desk["ISSUERS"]["РЖД"] == {"group": "РЖД", "sector": "Транспорт", "rating": "нет данных", "quasi": True}
    assert pos[RZD]["extra"]["lat_name"] == "RZhD BO-001P-02R" and pos[RZD]["rating"] == "нет данных"
    assert desk["RATING_ORDER"] == ["нет данных"]
