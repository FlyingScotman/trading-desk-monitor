"""Тесты хранилища: python -m unittest discover -s tests"""
import copy
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from tdm.assemble import assemble, resolve_run
from tdm.db import connect
from tdm.importer import import_bundle
from tdm.server import make_handler
from tdm.writer import Run, ValidationError

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = json.loads((ROOT / "fixtures" / "demo_bundle.json").read_text(encoding="utf-8"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "t.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def desk(self, **kw):
        con = connect(self.db, readonly=True)
        try:
            run = resolve_run(con, **kw)
            return assemble(con, run) if run else None
        finally:
            con.close()


class RoundTrip(Base):
    def test_bundle_round_trip(self):
        rid, status, warns = import_bundle(self.db, BUNDLE)
        self.assertEqual(status, "complete")
        self.assertEqual(warns, [])
        d = self.desk()
        self.assertEqual(d["source"]["runId"], rid)
        self.assertEqual(len(d["positions"]), len(BUNDLE["positions"]))
        self.assertEqual(len(d["funding"]), len(BUNDLE["funding"]))
        self.assertAlmostEqual(sum(p["dv01"] for p in d["positions"]), sum(p["dv01"] for p in BUNDLE["positions"]), places=3)
        self.assertAlmostEqual(sum(p["pnl"]["official"] for p in d["positions"]), sum(p["pnl"]["official"] for p in BUNDLE["positions"]), places=3)
        for p in d["positions"]:  # инвариант explain (1.3: официальный — по курсам ЦБ)
            self.assertAlmostEqual(p["pnl"]["explainedCbr"] + p["pnl"]["unexplained"], p["pnl"]["official"], places=6)
            self.assertAlmostEqual(p["pnl"]["explained"] + p["pnl"]["fxBasis"], p["pnl"]["explainedCbr"], places=6)
            self.assertAlmostEqual(sum(sum(v) for v in p["risk"].values()), p["dv01"], delta=max(1.0, abs(p["dv01"]) * 1e-9))
        self.assertEqual(len(d["history"]), 22)
        self.assertEqual(d["history"][-1]["date"], BUNDLE["run"]["as_of"])
        self.assertEqual([s["id"] for s in d["SCENARIOS"]], [s["scenario_id"] for s in BUNDLE["scenarios"]])
        self.assertEqual(list(d["BUCKETS"]), [b["bucket"] for b in BUNDLE["buckets"]])

    def test_latest_run_wins_and_run_id_selects(self):
        r1, *_ = import_bundle(self.db, BUNDLE)
        b2 = copy.deepcopy(BUNDLE)
        b2["run"]["run_type"] = "INTRADAY"
        b2["positions"][0]["mv_rub"] += 1e6
        r2, *_ = import_bundle(self.db, b2)
        self.assertEqual(self.desk()["source"]["runId"], r2)
        self.assertEqual(self.desk(run_id=r1)["source"]["runId"], r1)
        self.assertEqual(self.desk(as_of=BUNDLE["run"]["as_of"])["source"]["runId"], r2)
        self.assertIsNone(self.desk(as_of="2000-01-01"))


class Validation(Base):
    def test_missing_fx_fails_and_is_invisible(self):
        bad = copy.deepcopy(BUNDLE)
        bad["fx"] = [x for x in bad["fx"] if x["ccy"] != "CNY"]
        with self.assertRaises(ValidationError):
            import_bundle(self.db, bad)
        self.assertIsNone(self.desk())
        con = connect(self.db)
        self.assertEqual(con.execute("SELECT status FROM calc_run").fetchone()[0], "failed")

    def test_unknown_book_rejected(self):
        bad = copy.deepcopy(BUNDLE)
        bad["positions"][0]["book_id"] = "NOPE"
        with self.assertRaises(Exception):
            import_bundle(self.db, bad)
        self.assertIsNone(self.desk())

    def test_loading_run_not_visible(self):
        import_bundle(self.db, BUNDLE)
        published = self.desk()["source"]["runId"]
        run = Run(self.db, as_of=BUNDLE["run"]["as_of"], run_type="INTRADAY")
        run.add_books(BUNDLE["books"])  # прогон ещё грузится
        self.assertEqual(self.desk()["source"]["runId"], published)
        run.fail("test")
        run.con.close()

    def test_missing_official_pnl_warns(self):
        b = copy.deepcopy(BUNDLE)
        b["positions"][0]["pnl"]["official"] = None
        _, status, warns = import_bundle(self.db, b)
        self.assertEqual(status, "complete")
        self.assertTrue(any("официального" in w for w in warns))
        p = self.desk()["positions"][0]
        self.assertEqual(p["pnl"]["unexplained"], 0)


class Gamma(Base):
    def test_gamma_stored_and_summarized(self):
        import_bundle(self.db, BUNDLE)
        d = self.desk()
        src = {p["position_id"]: p for p in BUNDLE["positions"]}
        n = sum(len(p.get("gamma", [])) for p in BUNDLE["positions"])
        self.assertGreater(n, 500)
        self.assertEqual(sum(len(p["gamma"]) for p in d["positions"]), n)
        bond = next(p for p in d["positions"] if p["instrType"] == "BOND" and p["gamma"] and p["convexity"] and p["cs01"])
        # гамма к параллельному сдвигу согласована с выпуклостью: MV × C × 1e-8
        self.assertAlmostEqual(bond["gammaIr"], bond["mvRub"] * bond["convexity"] * 1e-8, delta=abs(bond["gammaIr"]) * 1e-4)
        self.assertEqual(bond["gammaSource"], "matrix")
        self.assertTrue(all(a <= b for a, b, _ in bond["gamma"]))
        irs = next(p for p in d["positions"] if p["instrType"] == "IRS" and p["side"] == "PAY")
        self.assertLess(irs["gammaIr"], 0)                       # pay-fixed — отрицательная выпуклость
        self.assertIn(bond["id"], src)

    def test_pair_order_is_canonical(self):
        b = copy.deepcopy(BUNDLE)
        p = next(x for x in b["positions"] if x.get("gamma"))
        p["gamma"] = [[y, x, v] for x, y, v in p["gamma"]]       # переставленные пары
        import_bundle(self.db, b)
        con = connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM position_gamma WHERE factor_1 > factor_2").fetchone()[0], 0)

    def test_bad_factor_fails_run(self):
        b = copy.deepcopy(BUNDLE)
        b["positions"][0]["gamma"] = [["IR|NO CURVE|5Y", "IR|NO CURVE|5Y", 1.0], ["FOO", "CS", 1.0]]
        with self.assertRaises(ValidationError):
            import_bundle(self.db, b)
        self.assertIsNone(self.desk())

    def test_conflicting_duplicate_pair_rejected(self):
        b = copy.deepcopy(BUNDLE)
        b["positions"][0]["gamma"] = [["CS", "CS", 1.0], ["CS", "CS", 2.0]]
        with self.assertRaises(ValueError):
            import_bundle(self.db, b)


class ScenarioResults(Base):
    def test_full_reval_passed_through(self):
        b = copy.deepcopy(BUNDLE)
        b["scenario_results"] = [{"scenario_id": "keyhike", "positions": [
            {"position_id": p["position_id"], "rates": -1.0, "total": -1.0} for p in b["positions"]], "funding_residual": {"FXB": 5.0}}]
        import_bundle(self.db, b)
        r = self.desk()["scenarioResults"]["keyhike"]
        self.assertEqual(len(r["byPosition"]), len(b["positions"]))
        self.assertEqual(r["fundingFxByBook"], {"FXB": 5.0})


class Http(Base):
    def test_endpoints(self):
        import_bundle(self.db, BUNDLE)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.db, ROOT / "prototypes"))
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        get = lambda p: urllib.request.urlopen(base + p).read().decode("utf-8")  # noqa: E731
        try:
            self.assertEqual(json.loads(get("/api/v1/health"))["status"], "ok")
            self.assertEqual(len(json.loads(get("/api/v1/runs"))["runs"]), 1)
            self.assertTrue(get("/desk-data.js").startswith("window.DESK = {"))
            self.assertIn("DESK_MISSING", get("/desk-data.js?as_of=2000-01-01"))
            self.assertEqual(len(json.loads(get("/api/v1/desk"))["positions"]), len(BUNDLE["positions"]))
            self.assertIn("Trading Desk Monitor", get("/"))
            with self.assertRaises(urllib.error.HTTPError):
                get("/../tdm/db.py")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
