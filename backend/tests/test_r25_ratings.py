"""R25: the desk's rating view of the hub's ``ratings`` (static 1.1+; 1.3 issuer scope and grades), the reference
facts (issuer names, groups, sectors, ratings) from the newest static of the cache, and where they show: the run's
issuer references and a bond's ``extra.rating``, window.DESK (positions, issuers, RATING_ORDER), the concept pages'
market, the scenario's rating rule, the instruments export. Synthetic static rows on the golden fixtures, and a
trimmed copy of the production hub's 1.2 static of 21.10.2026 (``fixtures/r25/live_ratings_20261021.json``)."""

from __future__ import annotations

import dataclasses
import gzip
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_market_history import DATE, FIX, FLOATER, OFZ, STATIC, USD_FACE, fixture_cache, write

from desk import VALUATION_VERSION, cli, store
from desk.api import create_app
from desk.marketdata import ratings, reference, static_facts
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare, value
from desk.publish import tdm_run
from desk.report import instruments
from desk.scenario.definition import normalize, own_spread_shock
from desk.scenario.taylor import risk_payload
from desk.settings import Settings

LIVE = json.loads((Path(__file__).parent / "fixtures" / "r25" / "live_ratings_20261021.json").read_text())
VTB, ALFA, RZD_BOND, AKRON_FLT, SELECTEL = "RU000A1034P7", "RU000A100GW5", "RU000A0JXQ44", FLOATER, "RU000A10GCT1"
FLT = "RU000A109XR1"  # Акрон, key-rate floater (test_market_history's FLOATER is the OFZ-PK)


# ---- the grade rule -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, grade", [
    ("AAA(RU)", "AAA"), ("AA-(RU)", "AA-"), ("ruAA+", "AA+"), ("ruBBB-", "BBB-"), ("A+.ru", "A+"), ("AA|ru|", "AA"),
    ("A-|ru|", "A-"), ("ruAAA.sf", "AAA"), ("AAA(ru.sf)", "AAA"), ("АА–(RU)", "AA-"), ("ruВВВ", "BBB"),
    ("ruB", "B"), ("ruCCC", "CCC"), ("D(RU)", "D"), ("Withdrawn", None), ("", None), (None, None),
    ("ESG-1", None), ("ruAAA(EXP)", "AAA"),
])
def test_grade(raw, grade):
    assert ratings.grade(raw) == grade


@pytest.mark.parametrize("g, b", [("AAA", "AAA"), ("AA+", "AA"), ("AA-", "AA"), ("A", "A"), ("BBB-", "BBB"),
                                  ("BB+", "≤BB"), ("B-", "≤BB"), ("D", "≤BB"), (None, "б/р"), ("x", "б/р")])
def test_bucket(g, b):
    assert ratings.bucket(g) == b


def row(secid, agency, rating, d="2026-06-01", scope="issue", issuer_id=None, scale="Национальная шкала", **kw):
    return {"scope": scope, "secid": secid if scope == "issue" else None, "issuer_id": issuer_id, "agency": agency,
            "scale": scale, "rating": rating, "outlook": kw.get("outlook"), "watch": None, "rating_date": d,
            "action": kw.get("action"), "source": kw.get("source", "cbonds"), **({"grade": kw["grade"]}
                                                                                 if "grade" in kw else {})}


def test_rating_book_rules():
    rows = [
        row("B1", "АКРА", "AA-(RU)", "2026-05-01"), row("B1", "Эксперт РА", "ruAA", "2026-04-01"),
        row("B1", "S&P Global Ratings", "BBB-", scale="Международная шкала"),  # not one of the desk's agencies
        row("B1", "АКРА", "AA(RU)", scale="Международная шкала АКРА"),          # international scale of ACRA
        row("B2", "Эксперт РА", "Withdrawn"), row("B2", "НКР", None, action="withdrawn", source="nkr"),
        row(None, "НРА", "A+|ru|", scope="issuer", issuer_id=7, scale="national_ru", source="nra", grade="A+",
            outlook="Стабильный"),
        row("B4", "НКР", "BBB.ru"), row("B5", "НКР", "BBB.ru"), row("B6", "АКРА", "A(RU)"),
        row("B7", "Эксперт РА", "ruAAA.sf", scale="Рейтинги надежности долг. инстр. структурного финансирования"),
    ]
    issuers = {"B1": 1, "B2": 2, "B3": 7, "B4": 4, "B5": 4, "B6": 4, "B8": 4, "B7": 9}
    book = ratings.RatingBook(rows, issuers)
    b1 = book.bond("B1")
    assert (b1["grade"], b1["agency"], b1["rating"], b1["basis"], b1["bucket"]) == ("AA", "Эксперт РА", "ruAA", "issue", "AA")
    assert [a[0] for a in b1["agencies"]] == ["Эксперт РА", "АКРА"]  # the international ones are left out
    assert book.bond("B2") is None and book.label(book.bond("B2")) == ratings.NOT_RATED  # withdrawn by both
    b3 = book.bond("B3")  # no issue rating: the issuer's (1.3 issuer scope)
    assert (b3["grade"], b3["agency"], b3["basis"], b3["outlook"]) == ("A+", "НРА", "issuer", "Стабильный")
    b8 = book.bond("B8")  # unrated bond of issuer 4: the issuer's level by its bonds (two BBB, one A: BBB)
    assert (b8["grade"], b8["basis"], b8["bonds"], b8["bonds_at_grade"]) == ("BBB", "issuer_bonds", 3, 2)
    assert book.issuer(4)["grade"] == "BBB"
    assert book.bond("B7")["sf"] and book.issuer(9) is None  # a structured-finance rating is not the issuer's level
    assert ratings.compact(book.bond("B7"))["sf"] is True
    empty = ratings.RatingBook([], {})
    assert not empty.has_data and empty.label(None) == ratings.NO_DATA


def test_live_rows_of_the_production_static():
    """The trimmed 1.2 static of 21.10.2026 (Cbonds rows: АКРА ``AA(RU)``, Эксперт РА ``ruA-``, НКР ``A+.ru``, НРА
    ``A|ru|``, ``Withdrawn``, ``ruAAA.sf``)."""
    assert LIVE["schema_version"] == "market_snapshot/1.2" and len(LIVE["ratings"]) == 10
    book = ratings.RatingBook(LIVE["ratings"], {i["secid"]: i["issuer_id"] for i in LIVE["instruments"]})
    by = {r["secid"]: r for r in LIVE["ratings"]}
    assert book.bond("RU000A0JXS83")["rating"] == "ruA-"            # АКРА withdrawn, Эксперт РА ruA-
    assert book.bond("RU000A0JX2Q3") is None                        # only a withdrawn rating
    b = book.bond("RU000A0ZZTK7")                                   # АКРА A+(RU), НРА A|ru|, Эксперт РА withdrawn
    assert (b["grade"], b["agency"]) == ("A+", "АКРА") and [a[0] for a in b["agencies"]] == ["АКРА", "НРА"]
    assert book.bond("RU000A1032P1")["sf"] and book.bond("RU000A107456")["grade"] == "A+"
    assert book.bond("RU000A103JK8")["bucket"] == "BBB"
    unrated = [i["secid"] for i in LIVE["instruments"] if i["issuer_id"] == 88]
    assert unrated and all(book.bond(s) is None for s in unrated)
    assert all(ratings.current(r) is None for r in by.values() if r["rating"] == "Withdrawn")
    cov = book.coverage(i["secid"] for i in LIVE["instruments"])
    assert cov["rows"] == 10 and cov["rated"] + cov["by_basis"].get("none", 0) == len(LIVE["instruments"])


# ---- reference facts from the newest static ------------------------------------------------------------------------


def static_doc() -> dict:
    with gzip.open(FIX / f"{STATIC}.json.gz") as f:
        return json.load(f)


def newer_static() -> dict:
    """The fixture's static as a 1.2 snapshot published later the same day, with issuer facts and ratings."""
    doc = static_doc()
    doc["schema_version"] = "market_snapshot/1.2"
    doc["cut_type"], doc["known_at"], doc["cut_ts"] = "change", "2026-10-16T17:30:00Z", "2026-10-16T17:30:00Z"
    for i in doc["data"]["issuers"]:
        i.update({"short_name": {269: "Банк ВТБ (ПАО)"}.get(i["issuer_id"]),
                  "sector": {269: "banks"}.get(i["issuer_id"]), "group_name": {269: "ВТБ"}.get(i["issuer_id"]),
                  "group_id": None, "is_state_owned": None, "sector_source": "curated" if i["issuer_id"] == 269 else None})
    doc["data"]["ratings"] = [
        row(VTB, "АКРА", "AAA(RU)", "2026-01-10", issuer_id=269), row(VTB, "Эксперт РА", "ruAA+", issuer_id=269),
        row(FLT, "НКР", "AA-.ru", issuer_id=1013),
        row(None, "НРА", "AA|ru|", scope="issuer", issuer_id=1013, scale="national_ru", source="nra"),
        row(ALFA, "Эксперт РА", "Withdrawn", issuer_id=465),
        row(RZD_BOND, "АКРА", "АА–(RU)", issuer_id=712),
    ]
    return doc


@pytest.fixture
def cache(tmp_path):
    c = fixture_cache(tmp_path)
    write(c, newer_static())
    return c


def test_newest_static_is_the_reference(cache):
    sd = SnapshotDir(cache)
    newest = reference.newest_static_id(sd)
    assert newest != STATIC and sd.manifest(newest)["known_at"] == "2026-10-16T17:30:00Z"
    ctx = prepare(sd, DATE)
    assert ctx.static.snapshot.id == STATIC  # the market names the morning static
    ref = reference.facts(sd, ctx.static)
    assert ref.snapshot_id == newest and ref.schema_version == "market_snapshot/1.2"
    assert ref.issuer(269).sector == "banks" and ref.issuer(1228).sector == "sovereign"  # OFZ: by the bond class
    assert ref.sector_source == {269: "curated", 1228: "bond_class"}
    assert ref.issuer(712).sector is None  # a corporate without a hub sector stays empty (never guessed)
    (cache.parent / "only").mkdir()
    assert reference.facts(SnapshotDir(fixture_cache(cache.parent / "only")), ctx.static).snapshot_id == STATIC


def test_run_content_rates_issuers_and_bonds(cache):
    sd = SnapshotDir(cache)
    ctx = prepare(sd, DATE)
    ctx = dataclasses.replace(ctx, reference=reference.facts(sd, ctx.static))
    reqs = [(VTB, 10.0), (FLT, 5.0), (USD_FACE, 3.0), (ALFA, 2.0), (RZD_BOND, 1.0), (OFZ, 4.0)]
    outs = value(ctx, reqs)
    c = tdm_run.run_content(ctx, [(f"P{i}", "CORP", o) for i, o in enumerate(outs)], None, {})
    iss = {r["issuer"]: r for r in c.reference["issuers"]}
    vtb = iss["Банк ВТБ (ПАО)"]
    assert (vtb["rating"], vtb["rating_info"]["agency"], vtb["sector"], vtb["issuer_group"]) == ("AAA", "АКРА", "Банки", "ВТБ")
    assert vtb["sector_source"] == "curated"
    akron = next(r for k, r in iss.items() if "Акрон" in k)
    assert akron["rating"] == "AA" and akron["rating_info"]["basis"] == "issuer"  # the 1.3 issuer rating
    alfa = next(r for k, r in iss.items() if "АЛЬФА" in k.upper())
    assert alfa["rating"] == ratings.NOT_RATED and "rating_info" not in alfa  # its only rating withdrawn
    minfin = next(r for r in iss.values() if r["is_gov"])
    assert minfin["sector"] == "Суверен" and minfin["rating"] is None  # the sovereign: no rating, not «б/р»
    rows = {r["instrument_id"]: r for r in c.rows}
    assert rows[FLT]["extra"]["rating"]["grade"] == "AA-" and rows[FLT]["extra"]["rating"]["basis"] == "issue"
    assert rows[USD_FACE]["extra"]["rating"]["grade"] == "AA"  # no issue rating: the issuer's
    assert rows[RZD_BOND]["extra"]["rating"]["grade"] == "AA-"  # Cyrillic look-alikes and an en dash
    assert "rating" not in rows[ALFA]["extra"] and "rating" not in rows[OFZ]["extra"]
    line = next(m for lv, m in c.log if m.startswith("справочник эмитентов и рейтинги"))
    assert "(market_snapshot/1.2) (новее статики рынка" in line and "рейтинг у 4 из 6 облигаций" in line
    assert VALUATION_VERSION >= 8


def test_without_ratings_the_hub_lacks_them(tmp_path):
    ctx = prepare(SnapshotDir(fixture_cache(tmp_path)), DATE)
    c = tdm_run.run_content(ctx, [("P1", "CORP", value(ctx, [(VTB, 1.0)])[0])], None, {})
    assert c.reference["issuers"][0]["rating"] == ratings.NO_DATA
    assert "строк рейтингов в статике нет" in next(m for _, m in c.log if m.startswith("справочник"))


def test_scenario_rule_matches_the_bonds_own_rating():
    norm = normalize({"spreads": {"rating": {"AA-": 50, "AA": 20, "б/р": 300}}})
    facts = [{"issuer": "A", "cs01": -1.0, "spread_bp": 100, "extra": {"rating": {"grade": "AA-"}}},
             {"issuer": "A", "cs01": -1.0, "spread_bp": 100, "extra": {}},
             {"issuer": "B", "cs01": -1.0, "spread_bp": 100, "extra": {}}]
    p = risk_payload({"run_id": 1, "business_date": DATE},
                [{"position_id": f"P{i}", "book_id": "X", "secid": "S", "fact": f, "pv_rub": 1.0, "quality": "exact"}
                 for i, f in enumerate(facts)],
                {"issuers": [{"issuer": "A", "rating": "AA"}, {"issuer": "B", "rating": "б/р"}], "curves": []})
    assert [x["rating"] for x in p["positions"]] == ["AA-", "AA", "б/р"]
    assert [own_spread_shock(norm, x) for x in p["positions"]] == [50.0, 20.0, 300.0]


def test_bond_facts_carry_the_rating_and_the_class_sector():
    facts = static_facts.bond_facts(newer_static()["data"], __import__("datetime").date.fromisoformat(DATE))
    assert facts[VTB]["rating"] == "AAA" and facts[VTB]["rating_info"]["agency"] == "АКРА"
    assert facts[VTB]["rating_bucket"] == "AAA" and facts[VTB]["sector_source"] == "curated"
    assert facts[SELECTEL]["rating"] == ratings.NOT_RATED and facts[SELECTEL]["rating_info"] is None
    assert facts[OFZ]["sector"] == "sovereign" and facts[OFZ]["sector_source"] == "bond_class"
    old = static_facts.bond_facts(static_doc()["data"], __import__("datetime").date.fromisoformat(DATE))
    assert old[VTB]["rating"] is None and old[VTB]["rating_bucket"] is None  # a static without ratings


def test_instruments_export_rating_columns(cache):
    static, market = instruments.find_snapshots(Path(cache))
    assert static.doc["schema_version"] == "market_snapshot/1.2"  # the newest static
    exp = instruments.build(static, market)
    by = {r["secid"]: r for r in exp.rows}
    assert by[VTB]["rating"] == "AAA" and by[VTB]["rating_agency"] == "АКРА" and by[VTB]["rating_basis"] == "issue"
    assert "Эксперт РА ruAA+" in by[VTB]["ratings_all"] and by[VTB]["rating_bucket"] == "AAA"
    assert by[ALFA]["rating"] == ratings.NOT_RATED and by[ALFA]["mon_issuer_group"] != by[VTB]["mon_issuer_group"]
    assert by[VTB]["mon_issuer_group"] == "ВТБ"


# ---- end to end: the store, window.DESK and the concept pages -----------------------------------------------------


@pytest.fixture
def desk(store_url, tmp_path, cache):
    settings = Settings(home=tmp_path / "home", snapshot_cache=cache)
    book = tmp_path / "book.csv"
    book.write_text(f"position_id,book_id,secid,quantity_bonds\nP1,CORP,{VTB},10\nP2,CORP,{FLT},5\n"
                    f"P3,CORP,{USD_FACE},3\nP4,CORP,{ALFA},2\nP5,OFZ,{OFZ},4\n", encoding="utf-8")
    lines: list[str] = []
    assert cli.eod_run(settings, DATE, store_url=store_url, book=str(book), out=lines.append), lines
    return TestClient(create_app(store_url))


def test_desk_data_positions_issuers_and_order(desk, store_url):
    with store.connect(store_url) as con:
        lin = con.execute("SELECT lineage FROM run").fetchone()[0]
        n = con.execute("SELECT count(*) FROM md_static_summary").fetchone()[0]
    assert lin["reference_snapshot_id"] != lin["static_snapshot_id"] and n == 2  # both statics in the read model
    d = desk.get("/api/v1/compat/desk").json()
    pos = {p["id"]: p for p in d["positions"]}
    assert pos["P1"]["rating"] == "AAA" and pos["P1"]["ratingInfo"]["agency"] == "АКРА"
    assert pos["P2"]["rating"] == "AA-" and pos["P3"]["rating"] == "AA"  # own issue rating; the issuer's
    assert pos["P4"]["rating"] == "б/р" and pos["P1"]["sector"] == "Банки"
    order = d["RATING_ORDER"]
    assert order.index("AAA") < order.index("AA") < order.index("AA-") < order.index("б/р")
    assert d["ISSUERS"]["Банк ВТБ"]["ratingInfo"]["rating"] == "AAA(RU)"


def test_concepts_market_has_ratings(desk):
    body = desk.get("/concepts-data.js").text
    x = json.loads(body[len("window.DESK_EXTRAS = "):-2])
    by = {b["id"]: b for b in x["universeRub"]}
    if VTB in by:
        assert by[VTB]["rating"] == "AAA" and by[VTB]["ratingAgency"] == "АКРА" and by[VTB]["sector"] == "Банки"
    assert x["referenceDate"] == DATE
    assert any(n.startswith("рейтинги — лучший из АКРА") for n in x["notes"])
    assert not any("рейтингов в данных хаба нет" in n for n in x["notes"])
