"""Слой входных данных: загрузчик CSV, рынок, add_reference_data, исторические сценарии, API."""
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

from tdm.assemble import assemble, resolve_run
from tdm.db import connect, init_db
from tdm.histscen import build
from tdm.loader import load_paths
from tdm.server import make_handler
from tdm.writer import Run

ROOT = Path(__file__).resolve().parent.parent


def bdays(n, end=date(2026, 10, 13)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


DATES = bdays(15)
SHOCK_DAY = DATES[9]   # в этот день RUB OFZ +150 б.п., рубль −5 %


def w(path, header, rows, delim=";"):
    with open(path, "w", encoding="utf-8") as f:
        f.write(delim.join(header) + "\n")
        for r in rows:
            f.write(delim.join(str(x) for x in r) + "\n")


def make_inputs(folder):
    p = Path(folder)
    w(p / "books.csv", ["book_id", "name", "desk"], [["OFZ", "ОФЗ", "Rates"], ["CORP", "Корпы", "Credit"]])
    w(p / "issuers.csv", ["issuer_id", "name", "issuer_group", "is_gov"],
      [["MINFIN", "Минфин РФ", "Российская Федерация", 1], ["X5", "X5", "X5", 0], ["SEG", "Сегежа", "АФК Система", 0]])
    w(p / "issuer_ratings.csv", ["issuer_id", "date", "agency", "rating"],
      [["MINFIN", DATES[0], "COMPOSITE", "AAA"], ["X5", DATES[0], "COMPOSITE", "AAA"], ["SEG", DATES[0], "COMPOSITE", "A"],
       ["SEG", DATES[5], "COMPOSITE", "BB"]])
    w(p / "curves.csv", ["curve_id", "name", "ccy", "kind"], [["RUB OFZ", "ОФЗ", "RUB", "bond"], ["USD SOFR", "SOFR", "USD", "swap"]])
    w(p / "instruments.csv", ["instrument_id", "name", "issuer_id", "asset_class", "segment", "ccy", "face_value", "maturity", "coupon_type"],
      [["SU26242", "ОФЗ 26242", "MINFIN", "ОФЗ-ПД", "Гос", "RUB", 1000, "2029-08-29", "fix"],
       ["X5-01", "X5 001Р-01", "X5", "Корп RUB фикс", "Корп", "RUB", 1000, "2028-01-01", "fix"],
       ["SEG-02", "Сегежа 003Р-02R", "SEG", "Корп RUB фикс", "Корп", "RUB", 1000, "2027-06-01", "fix"]])
    curves, fx, quotes, pos, fix = [], [], [], [], []
    for i, d in enumerate(DATES):
        jump = 1.5 if d >= SHOCK_DAY else 0.0
        for t, base in ((0.25, 14.0), (1, 13.6), (5, 13.1), (10, 13.4), (20, 13.6)):
            curves.append([d, "RUB OFZ", str(t).replace(".", ","), str(round(base + jump + 0.01 * i, 4)).replace(".", ",")])
            curves.append([d, "USD SOFR", str(t).replace(".", ","), "4,0"])
        fx.append([d, "USD", str(round(80 * (1.05 if d >= SHOCK_DAY else 1.0), 4)).replace(".", ",")])
        fix.append([d, "RUONIA", "14,3"])
        quotes.append([d, "X5-01", "98,5", str(150 + (30 if d >= SHOCK_DAY else 0))])
        quotes.append([d, "SEG-02", "90,1", str(700 + (200 if d >= SHOCK_DAY else 0))])
        pos.append([d, "OFZ", "OFZ|SU26242", "SU26242", 1000000])
        pos.append([d, "CORP", "CORP|X5-01", "X5-01", 500000])
    w(p / "md_curves_2026.csv", ["date", "curve_id", "tenor_years", "rate"], curves)
    w(p / "md_fx.csv", ["date", "ccy", "rate"], fx)
    w(p / "md_fixings.csv", ["date", "index_name", "value"], fix)
    w(p / "md_bond_quotes.csv", ["date", "instrument_id", "clean_price", "z_spread_bp"], quotes)
    w(p / "positions_all.csv", ["as_of", "book_id", "position_id", "instrument_id", "quantity"], pos)


def publish_run(db, as_of):
    with Run(db, as_of=as_of, run_type="EOD") as run:
        run.add_reference_data()
        run.add_positions([
            {"position_id": "OFZ|SU26242", "book_id": "OFZ", "instr_type": "BOND", "asset_class": "ОФЗ-ПД", "segment": "Гос",
             "name": "ОФЗ 26242", "issuer": "MINFIN", "ccy": "RUB", "maturity": "2029-08-29", "notional": 1e9, "mv": 9e8, "mv_rub": 9e8,
             "dv01": -250000, "cs01": 0, "risk": {"RUB OFZ": {"2Y": -100000, "3Y": -150000}}, "pnl": {"rates": -100, "official": -90}},
            {"position_id": "CORP|X5-01", "book_id": "CORP", "instr_type": "BOND", "asset_class": "Корп RUB фикс", "segment": "Корп",
             "name": "X5 001Р-01", "issuer": "X5", "ccy": "RUB", "maturity": "2028-01-01", "notional": 5e8, "mv": 4.9e8, "mv_rub": 4.9e8,
             "dv01": -60000, "cs01": -60000, "risk": {"RUB OFZ": {"1Y": -60000}}},
        ])
    return run


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t.sqlite")
        self.inp = Path(self.tmp.name) / "in"
        self.inp.mkdir()
        make_inputs(self.inp)
        self.reports = load_paths(self.db, [self.inp])

    def tearDown(self):
        self.tmp.cleanup()


class Loader(Base):
    def test_all_files_loaded(self):
        for r in self.reports:
            self.assertEqual(r["errors"], [], r["file"])
        con = connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM md_curve_point").fetchone()[0], len(DATES) * 10)
        self.assertEqual(con.execute("SELECT rate FROM md_curve_point WHERE date=? AND curve_id='USD SOFR' AND tenor_years=0.25", (DATES[0],)).fetchone()[0], 4.0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM data_load WHERE status='ok'").fetchone()[0], len(self.reports))

    def test_bad_row_rejects_whole_file(self):
        w(self.inp / "md_fx_bad.csv", ["date", "ccy", "rate"], [[DATES[0], "USD", "1"], ["2026-13-45", "USD", "x"]])
        rep = [r for r in load_paths(self.db, [self.inp / "md_fx_bad.csv"])][0]
        self.assertTrue(any("строка 3" in e for e in rep["errors"]))
        con = connect(self.db)
        self.assertNotEqual(con.execute("SELECT rate FROM md_fx WHERE date=?", (DATES[0],)).fetchone()[0], 1)

    def test_snapshot_replaces_book_slice(self):
        w(self.inp / "positions_fix.csv", ["as_of", "book_id", "position_id", "instrument_id", "quantity"],
          [[DATES[-1], "OFZ", "OFZ|SEG-02", "SEG-02", 10]])
        load_paths(self.db, [self.inp / "positions_fix.csv"])
        con = connect(self.db)
        rows = con.execute("SELECT book_id, position_id FROM pos_snapshot WHERE as_of=? ORDER BY 1", (DATES[-1],)).fetchall()
        self.assertEqual([tuple(r) for r in rows], [("CORP", "CORP|X5-01"), ("OFZ", "OFZ|SEG-02")])

    def test_unknown_file_and_missing_column(self):
        (self.inp / "whatever.csv").write_text("a;b\n1;2\n", encoding="utf-8")
        w(self.inp / "md_fx_nocol.csv", ["date", "ccy"], [[DATES[0], "USD"]])
        reps = {Path(r["file"]).name: r for r in load_paths(self.db, [self.inp / "whatever.csv", self.inp / "md_fx_nocol.csv"])}
        self.assertTrue(reps["whatever.csv"]["errors"])
        self.assertIn("rate", reps["md_fx_nocol.csv"]["errors"][0])


class FromReference(Base):
    def test_run_takes_market_from_store(self):
        run = publish_run(self.db, DATES[-1])
        self.assertEqual(run.status, "complete")
        con = connect(self.db, readonly=True)
        d = assemble(con, resolve_run(con))
        self.assertEqual(set(d["CURVES"]), {"RUB OFZ", "USD SOFR"})
        self.assertAlmostEqual(d["CURVE_MOVES"]["RUB OFZ"][5], 1.0, places=6)   # +0.01% в день
        self.assertAlmostEqual(d["FX"]["USD"], 84.0)
        self.assertEqual(d["INDICES"]["RUONIA"], 14.3)
        self.assertIn("Сегежа", d["ISSUERS"])                                   # имя, а не код
        self.assertEqual(d["ISSUERS"]["Сегежа"]["rating"], "BB")                 # рейтинг на дату
        self.assertEqual(d["positions"][0]["issuer"], "Минфин РФ")

    def test_official_pnl_from_store(self):
        w(self.inp / "pnl_official.csv", ["as_of", "book_id", "position_id", "pnl_rub"],
          [[DATES[-1], "CORP", "CORP|X5-01", 777], [DATES[-1], "OFZ", "OFZ|SU26242", 555], [DATES[-1], "OFZ", "", 1000]])
        load_paths(self.db, [self.inp / "pnl_official.csv"])
        with Run(self.db, as_of=DATES[-1]) as run:
            run.add_reference_data()
            run.add_positions([
                {"position_id": "CORP|X5-01", "book_id": "CORP", "instr_type": "BOND", "asset_class": "Корп", "segment": "Корп", "name": "X5",
                 "issuer": "X5", "ccy": "RUB", "notional": 1, "mv": 1, "mv_rub": 1, "dv01": 0, "pnl": {"rates": 700}},
                {"position_id": "OFZ|SU26242", "book_id": "OFZ", "instr_type": "BOND", "asset_class": "ОФЗ", "segment": "Гос", "name": "ОФЗ",
                 "issuer": "MINFIN", "ccy": "RUB", "notional": 1, "mv": 1, "mv_rub": 1, "dv01": 0, "pnl": {"rates": 500, "official": 510}}])
        con = connect(self.db, readonly=True)
        p = {x["id"]: x["pnl"] for x in assemble(con, resolve_run(con))["positions"]}
        self.assertEqual(p["CORP|X5-01"]["official"], 777)      # из pnl_official
        self.assertEqual(p["CORP|X5-01"]["unexplained"], 77)
        self.assertEqual(p["OFZ|SU26242"]["official"], 510)     # расчётный сервис важнее

    def test_shock_day_move(self):
        publish_run(self.db, SHOCK_DAY)
        con = connect(self.db, readonly=True)
        d = assemble(con, resolve_run(con))
        self.assertAlmostEqual(d["CURVE_MOVES"]["RUB OFZ"][3], 151.0, places=4)
        self.assertAlmostEqual(d["FX_MOVE_PCT"]["USD"], 5.0, places=6)


class Historical(Base):
    def test_worst_window_is_shock_day(self):
        publish_run(self.db, DATES[-1])
        made = build(self.db, horizons=(1, 5), top=2)
        one = [m for m in made if m["horizon"] == 1]
        self.assertEqual(one[0]["scenario_id"], f"hist1d-{SHOCK_DAY}")
        con = connect(self.db)
        sc = json.loads(con.execute("SELECT definition_json FROM scenario_def WHERE scenario_id=?", (f"hist1d-{SHOCK_DAY}",)).fetchone()[0])
        self.assertAlmostEqual(sc["curves"]["RUB OFZ"][5], 151.0, places=2)
        self.assertAlmostEqual(sc["fx"]["USD"], 5.0, places=2)
        self.assertEqual(sc["spreads"]["AAA"], 30)          # X5; суверен исключён
        self.assertEqual(sc["spreads"]["BB"], 200)          # Сегежа уже BB на эту дату
        five = [m for m in made if m["horizon"] == 5]
        self.assertEqual(len(five), 2)
        # и попадают в монитор
        d = assemble(connect(self.db, readonly=True), resolve_run(connect(self.db, readonly=True)))
        self.assertIn(f"hist1d-{SHOCK_DAY}", [s["id"] for s in d["SCENARIOS"]])

    def test_attach_full_reval_to_published_run(self):
        from tdm.writer import attach_scenario_results
        run = publish_run(self.db, DATES[-1])
        build(self.db, horizons=(1,), top=1)
        sid = f"hist1d-{SHOCK_DAY}"
        attach_scenario_results(self.db, run.run_id, sid, [{"position_id": "OFZ|SU26242", "rates": -5e6, "total": -5.1e6}])
        attach_scenario_results(self.db, run.run_id, sid, [{"position_id": "OFZ|SU26242", "rates": -4e6, "total": -4.1e6}])
        con = connect(self.db, readonly=True)
        r = assemble(con, resolve_run(con))["scenarioResults"][sid]["byPosition"]
        self.assertEqual(r["OFZ|SU26242"]["total"], -4.1e6)
        with self.assertRaises(ValueError):
            attach_scenario_results(self.db, 999, sid, [])

    def test_proxy_includes_second_order(self):
        from tdm.histscen import factor_shock, portfolio_sensitivities, proxy_pnl
        with Run(self.db, as_of=DATES[-1]) as run:
            run.add_reference_data()
            run.add_positions([{"position_id": "OFZ|SU26242", "book_id": "OFZ", "instr_type": "BOND", "asset_class": "ОФЗ-ПД",
                                "segment": "Гос", "name": "ОФЗ", "issuer": "MINFIN", "ccy": "RUB", "notional": 1e9, "mv": 1e9,
                                "mv_rub": 1e9, "dv01": -400000, "cs01": 0, "convexity": 25,
                                "risk": {"RUB OFZ": {"5Y": -400000}}}])
        con = connect(self.db, readonly=True)
        sens = portfolio_sensitivities(con)
        shock = {"curves": {"RUB OFZ": [200.0] * 11}, "fx": {}, "spreads": {}}
        lin = -400000 * 200
        self.assertAlmostEqual(proxy_pnl(shock, sens) - lin, 0.5 * 1e9 * 25 * (0.02 ** 2), delta=1)   # ½·MV·C·Δy²
        self.assertEqual(factor_shock("IR|RUB OFZ|5Y", shock), 200.0)

    def test_rebuild_replaces(self):
        build(self.db, horizons=(1,), top=3)
        build(self.db, horizons=(1,), top=1)
        con = connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM scenario_def WHERE kind='historical' AND active=1").fetchone()[0], 1)


class Api(Base):
    def test_endpoints(self):
        publish_run(self.db, DATES[-1])
        build(self.db, horizons=(1,), top=1)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db, ROOT / "prototypes"))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        get = lambda p: json.loads(urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_address[1]}/api/v1/{p}").read())  # noqa: E731
        try:
            self.assertEqual(len(get("books")["books"]), 2)
            self.assertEqual({x["issuer_id"]: x["rating"] for x in get(f"issuers?as_of={DATES[0]}")["issuers"]}["SEG"], "A")
            pos = get("positions")
            self.assertEqual(pos["as_of"], DATES[-1])
            self.assertEqual(len(pos["positions"]), 2)
            self.assertFalse(any(p["new_today"] for p in pos["positions"]))
            cur = get(f"market/curves?as_of={SHOCK_DAY}")
            self.assertAlmostEqual(cur["curves"]["RUB OFZ"]["move_bp"][4], 151.0, places=4)
            self.assertEqual(len(get("market/curves?curve=RUB%20OFZ")["series"]), len(DATES))
            self.assertEqual(len(get(f"market/fx?ccy=USD&from={DATES[5]}")["series"]), len(DATES) - 5)
            self.assertIn("X5", get("market/issuer-spreads")["issuers"])
            self.assertEqual(get("instruments/X5-01")["last_quote"]["date"], DATES[-1])
            cov = get("coverage")
            self.assertEqual(len(cov["days"]), len(DATES))
            self.assertEqual(cov["days"][-1]["eod_run"], 1)
            self.assertTrue(any(s["kind"] == "historical" for s in get("scenarios")["scenarios"]))
            with self.assertRaises(urllib.error.HTTPError):
                get("positions?as_of=13.10.2026")
        finally:
            httpd.shutdown()
            httpd.server_close()


class Migration(unittest.TestCase):
    def test_v1_database_gets_new_columns(self):
        with tempfile.TemporaryDirectory() as t:
            db = str(Path(t) / "old.sqlite")
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE run_issuer (run_id INTEGER, issuer TEXT, issuer_group TEXT, sector TEXT, rating TEXT, is_gov INTEGER, is_quasi INTEGER)")
            con.execute("CREATE TABLE scenario_def (scenario_id TEXT PRIMARY KEY, name TEXT, ord INTEGER, definition_json TEXT, active INTEGER, updated_at TEXT)")
            con.execute("CREATE TABLE position (run_id INTEGER, position_id TEXT, book_id TEXT)")   # до v4: без полей облигаций
            con.commit()
            con.close()
            con = init_db(db)
            cols = {r[1] for r in con.execute("PRAGMA table_info(run_issuer)")}
            self.assertIn("name", cols)
            self.assertIn("kind", {r[1] for r in con.execute("PRAGMA table_info(scenario_def)")})
            self.assertLessEqual({"instrument_id", "accrued", "coupon_freq", "yield_date", "spread_type", "spread_curve"},
                                 {r[1] for r in con.execute("PRAGMA table_info(position)")})
            self.assertEqual(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "5")


if __name__ == "__main__":
    unittest.main()
