"""Облигации: поля 1.2, котировки за день в window.DESK, история выпуска /api/v1/bonds/history."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from tdm.api import handle
from tdm.assemble import assemble, resolve_run
from tdm.db import connect
from tdm.importer import import_bundle, load_demo_market
from tdm.writer import ValidationError

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = json.loads((ROOT / "fixtures" / "demo_bundle.json").read_text(encoding="utf-8"))
MARKET = ROOT / "fixtures" / "demo_market.json.gz"


def interp(ts, rs, t):
    if t <= ts[0]:
        return rs[0]
    for i in range(1, len(ts)):
        if t <= ts[i]:
            w = (t - ts[i - 1]) / (ts[i] - ts[i - 1])
            return rs[i - 1] * (1 - w) + rs[i] * w
    return rs[-1]


class Bonds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "b.sqlite")
        rid, status, _ = import_bundle(cls.db, BUNDLE)
        assert status == "complete"
        cls.counts = load_demo_market(cls.db, MARKET)
        cls.con = connect(cls.db, readonly=True)
        cls.desk = assemble(cls.con, resolve_run(cls.con))
        cls.bonds = {p["name"]: p for p in cls.desk["positions"] if p["instrType"] == "BOND"}

    @classmethod
    def tearDownClass(cls):
        cls.con.close()
        cls.tmp.cleanup()

    def hist(self, **q):
        return handle(self.con, "bonds/history", {k: str(v) for k, v in q.items()})

    def test_fields_and_day_quote(self):
        p = self.bonds["МТС 002Р-08"]
        self.assertEqual(p["yieldDate"], "2027-11-24")
        self.assertEqual((p["spreadType"], p["spreadCurve"], p["couponFreq"]), ("G", "RUB OFZ", 4))
        q = p["quote"]
        self.assertEqual(q["date"], self.desk["asOf"])
        self.assertAlmostEqual(q["ytm"], round(p["ytm"], 4), places=6)          # котировка дня = уровень прогона
        self.assertAlmostEqual(q["zSpread"] - q["prevZSpread"], 2.0, places=6)    # изменение спреда за день = SPREAD_MOVES
        self.assertIsNone(next(x for x in self.desk["positions"] if x["instrType"] == "IRS").get("quote"))

    def test_history_last_point_matches_run(self):
        p = self.bonds["Сегежа 003Р-02R"]
        h = self.hist(isin=p["isin"], curve="RUB OFZ", to=self.desk["asOf"])
        s = h["series"]
        self.assertEqual(len(s), self.counts["dates"])
        self.assertEqual(s[-1]["date"], self.desk["asOf"])
        self.assertAlmostEqual(s[-1]["ytm"], p["ytm"], places=3)
        self.assertAlmostEqual(s[-1]["clean_price"], p["price"], places=3)
        # спред к кривой = доходность − ставка кривой на сроке до погашения
        pts = self.desk["CURVES"]["RUB OFZ"]["pts"]
        self.assertAlmostEqual(s[-1]["curve_rate"], interp(self.desk["PILLARS"], pts, s[-1]["tenor_y"]), places=3)
        self.assertAlmostEqual(s[-1]["spread_to_curve_bp"], (s[-1]["ytm"] - s[-1]["curve_rate"]) * 100, places=1)
        self.assertEqual(s[-1]["face_amount"], p["notional"])
        self.assertIsNone(h["spread_to_curve_note"])

    def test_offer_shortens_tenor(self):
        p = self.bonds["МТС 002Р-08"]
        s = self.hist(instrument_id=p["instrumentId"], curve="RUB OFZ")["series"]
        self.assertAlmostEqual(s[-1]["tenor_y"], 1.1143, places=3)          # до оферты 24.11.2027, а не до 2029
        self.assertAlmostEqual(s[0]["tenor_y"] - s[-1]["tenor_y"], 1.0, delta=0.01)

    def test_floater_and_currency_mismatch(self):
        fl = self.hist(isin=self.bonds["ОФЗ 29010"]["isin"], curve="RUB OFZ")
        self.assertIn("флоатер", fl["spread_to_curve_note"])
        self.assertTrue(all(r["curve_rate"] is None for r in fl["series"]))
        usd = self.hist(isin=self.bonds["Газпром ЗО27-1-Д"]["isin"], curve="RUB OFZ")
        self.assertIn("валюта", usd["spread_to_curve_note"])
        ok = self.hist(isin=self.bonds["Газпром ЗО27-1-Д"]["isin"], curve="USD SOFR")
        self.assertIsNotNone(ok["series"][-1]["spread_to_curve_bp"])

    def test_position_history_zero_before_purchase(self):
        s = self.hist(isin=self.bonds["X5 Финанс 003Р-04"]["isin"], curve="RUB OFZ")["series"]   # куплена сегодня
        self.assertEqual(s[-2]["face_amount"], 0)
        self.assertGreater(s[-1]["face_amount"], 0)

    def test_window_and_errors(self):
        p = self.bonds["ОФЗ 26244"]
        s = self.hist(isin=p["isin"], curve="RUB OFZ", **{"from": "2026-10-01"})["series"]
        self.assertEqual(s[0]["date"], "2026-10-01")
        with self.assertRaises(ValueError):
            handle(self.con, "bonds/history", {})
        with self.assertRaises(ValueError):
            handle(self.con, "bonds/history", {"isin": p["isin"], "from": "01.10.2026"})
        self.assertEqual(self.hist(isin="RU0000000000")["series"], [])


class BondValidation(unittest.TestCase):
    def test_bad_spread_type_and_yield_date_fail(self):
        with tempfile.TemporaryDirectory() as t:
            db = str(Path(t) / "v.sqlite")
            b = copy.deepcopy(BUNDLE)
            b["positions"][0]["spread_type"] = "ZZ"
            b["positions"][1]["yield_date"] = "24.11.2027"
            with self.assertRaises(ValidationError) as cm:
                import_bundle(db, b)
            self.assertIn("spread_type", str(cm.exception))
            self.assertIn("yield_date", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
