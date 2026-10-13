"""Контракт 1.3: миграция v4 → v5, новые наборы, фондирование по позициям и остаткам, правила ставок,
официальный PnL на T+1, проверки публикации, форма window.DESK как в prototypes/data.js, совместимость с 1.2."""
import copy
import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tdm import server
from tdm.api import handle
from tdm.assemble import assemble, resolve_run
from tdm.db import connect, init_db, now_iso
from tdm.importer import import_bundle
from tdm.loader import load_paths
from tdm.writer import Run, ValidationError, attach_official_pnl, resolve_funding_rule

ROOT = Path(__file__).resolve().parent.parent
BUNDLE = json.loads((ROOT / "fixtures" / "demo_bundle.json").read_text(encoding="utf-8"))
AS_OF = BUNDLE["run"]["as_of"]


def w(path, header, rows):
    with open(path, "w", encoding="utf-8") as f:
        f.write(";".join(header) + "\n")
        for r in rows:
            f.write(";".join("" if x is None else str(x) for x in r) + "\n")


def to_v12(b):
    """Пакет 1.3 → форма 1.2: без новых полей; фондирование — только сделки книги без привязки к бумагам."""
    b = copy.deepcopy(b)
    b["contract_version"] = "1.2"
    for k in ("prev_date", "accrual_days", "jtd_recovery", "ofz_spread_move"):
        b["run"].pop(k, None)
    for x in b["books"]:
        for k in ("book_type", "entity", "ocp_to"):
            x.pop(k)
    for x in b["fx"]:
        for k in ("rate_cbr", "move_pct_cbr", "rate_cbr_next"):
            x.pop(k)
    for p in b["positions"]:
        for k in ("ofz_spr01", "accrual_only", "purchase_date", "purchase_price", "purchase_yield", "contract_rate", "extra"):
            p.pop(k, None)
        for k in ("funding", "fx_gross_cbr", "fx_funding_cbr"):
            p["pnl"].pop(k)
        p["pnl"]["fx_funding"] = 0
    b["funding"] = [{k: f[k] for k in ("deal_id", "book_id", "type", "ccy", "amount", "rate_expr", "rate", "start_date", "maturity",
                                       "counterparty", "collateral")} | {"pnl": {k: f["pnl"][k] for k in ("carry", "fx_gross", "fx_allocated")}}
                    for f in b["funding"] if not f["position_id"]]
    for k in ("funding_rules", "ocp_transfers", "carry_scenarios", "cbr_meetings"):
        b.pop(k)
    return b


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = str(self.dir / "t.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def desk(self, **kw):
        con = connect(self.db, readonly=True)
        try:
            return assemble(con, resolve_run(con, **kw))
        finally:
            con.close()


# ---------------------------------------------------------------------- миграция
class MigrationV4(Base):
    def test_v4_database_upgraded_and_old_run_readable(self):
        con = sqlite3.connect(self.db)
        con.executescript((ROOT / "tests" / "fixtures" / "schema_v4.sql").read_text(encoding="utf-8"))
        ts = now_iso()
        con.execute("INSERT INTO meta VALUES ('schema_version', '4')")
        con.execute("INSERT INTO calc_run (run_id, as_of, run_type, status, contract_version, started_at, published_at)"
                    " VALUES (1, '2026-10-12', 'EOD', 'complete', '1.2', ?, ?)", (ts, ts))
        con.execute("INSERT INTO run_book (run_id, book_id, name, desk) VALUES (1, 'OFZ', 'ОФЗ', 'Rates'), (1, 'FXB', 'FX', 'FX')")
        con.execute("INSERT INTO run_issuer (run_id, issuer, issuer_group) VALUES (1, 'Минфин РФ', 'РФ')")
        con.execute("INSERT INTO mkt_fx (run_id, ccy, rate, move_pct) VALUES (1, 'RUB', 1, 0), (1, 'USD', 80, 1)")
        con.execute("INSERT INTO position (run_id, position_id, book_id, instr_type, asset_class, segment, name, issuer, ccy, notional, mv, mv_rub, dv01)"
                    " VALUES (1, 'P1', 'OFZ', 'BOND', 'ОФЗ-ПД', 'Гос', 'ОФЗ', 'Минфин РФ', 'RUB', 1e9, 9e8, 9e8, -1000)")
        con.execute("INSERT INTO position_pnl (run_id, position_id, carry, fx_gross, official) VALUES (1, 'P1', 100, 0, 130)")
        con.execute("INSERT INTO funding_deal (run_id, deal_id, book_id, type, ccy, amount, rate, maturity)"
                    " VALUES (1, 'R1', 'FXB', 'РЕПО', 'USD', -1e6, 5, '2026-10-13')")
        con.execute("INSERT INTO funding_pnl (run_id, deal_id, carry, fx_gross, fx_allocated) VALUES (1, 'R1', -10, -800, 0)")
        con.commit()
        con.close()
        for _ in range(2):   # идемпотентно
            init_db(self.db).close()
        con = connect(self.db)
        cols = lambda t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")}  # noqa: E731
        self.assertEqual(con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "5")
        self.assertLessEqual({"book_type", "entity", "ocp_to"}, cols("run_book"))
        self.assertLessEqual({"rate_cbr", "move_pct_cbr", "rate_cbr_next"}, cols("mkt_fx"))
        self.assertLessEqual({"ofz_spr01", "accrual_only", "purchase_date", "purchase_price", "purchase_yield", "contract_rate", "ocp_transfer"}, cols("position"))
        self.assertLessEqual({"funding", "fx_gross_cbr", "fx_funding_cbr"}, cols("position_pnl"))
        self.assertLessEqual({"kind", "position_id", "index_name", "spread_bp", "fixed_rate", "benchmark", "day_basis"}, cols("funding_deal"))
        self.assertLessEqual({"jtd_recovery", "prev_date", "accrual_days", "official_at"}, cols("calc_run"))
        self.assertLessEqual({"book_type", "entity", "ocp_to"}, cols("ref_book"))
        self.assertLessEqual({"purchase_date", "contract_rate"}, cols("pos_snapshot"))
        self.assertIn("purpose", cols("ref_otc_deal"))
        self.assertLessEqual({"carry", "funding", "fx"}, cols("pnl_official"))
        self.assertLessEqual({"call_date", "is_subord"}, cols("ref_instrument"))
        for t in ("funding_rule", "run_ocp_transfer", "carry_scenario_def", "ref_cbr_meeting", "ref_funding_rate", "cash_snapshot", "md_fx_cbr"):
            self.assertTrue(con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone(), t)
        self.assertEqual(con.execute("SELECT kind FROM funding_deal").fetchone()[0], "DEAL")
        con.close()
        d = self.desk()   # прогон 1.2 собирается в форму 1.3 со значениями по умолчанию
        self.assertEqual(d["BOOKS"]["OFZ"]["bookType"], "TRADING")
        self.assertEqual(d["BOOKS"]["FXB"]["bookType"], "DERIVS")
        self.assertEqual(d["FX_CBR"], d["FX"])
        self.assertEqual(d["prevDate"], "2026-10-09")
        self.assertEqual(d["accrualDays"], 3)
        p = d["positions"][0]["pnl"]
        self.assertEqual((p["fxBasis"], p["funding"], p["explainedCbr"], p["unexplained"]), (0, 0, 100, 30))
        f = d["funding"][0]
        self.assertEqual((f["kind"], f["positionId"], f["pnl"]["inPosition"], f["pnl"]["fxCbr"]), ("DEAL", None, False, -800))
        self.assertEqual(d["fundingPnl"], {"carry": -10, "fx": -800, "fxCbr": -800})
        self.assertEqual([s["id"] for s in d["CARRY_SCENARIOS"]], ["flat"])
        self.assertTrue(d["official"]["received"])


# ---------------------------------------------------------------------- входной слой
class Datasets13(Base):
    def test_new_datasets_load(self):
        inp = self.dir / "in"
        inp.mkdir()
        w(inp / "books.csv", ["book_id", "name", "desk", "book_type", "entity", "ocp_to"],
          [["CORP_FX", "Корпы вал.", "FX Credit", "TRADING", "Головной банк", "FXB"], ["FXB", "FX", "FX", "DERIVS", "Головной банк", ""]])
        w(inp / "funding_rates.csv", ["book_type", "desk", "book_id", "entity", "instrument_id", "ccy", "index_name", "spread_bp", "fixed_rate", "note"],
          [["TRADING", "", "", "", "", "RUB", "КС", 30, "", "спред деска"], ["TRADING", "", "", "", "", "USD", "", "", 5, "hard ccy"],
           ["", "", "SECUR", "", "", "RUB", "КС", 50, "", ""]])
        w(inp / "cash_balances_2026-10-13.csv", ["as_of", "book_id", "ccy", "balance"],
          [[AS_OF, "FXB", "RUB", 3e9], [AS_OF, "FXB", "USD", 5e6]])
        w(inp / "md_fx_cbr.csv", ["date", "ccy", "rate", "set_date"],
          [["2026-10-10", "USD", 80.5, "2026-10-09"], ["2026-10-13", "USD", 81.12, "2026-10-12"], ["2026-10-14", "USD", 81.47, AS_OF]])
        w(inp / "cbr_meetings.csv", ["date", "effective_date"], [["2026-10-23", "2026-10-26"], ["2026-12-18", ""]])
        w(inp / "positions.csv", ["as_of", "book_id", "position_id", "instrument_id", "face_amount", "purchase_date", "purchase_price",
                                  "purchase_yield", "contract_rate"], [[AS_OF, "HTM", "HTM|SU26240", "SU26240", 3e9, "2024-03-12", 74.2, 12.1, 11.85]])
        w(inp / "otc_deals.csv", ["deal_id", "instr_type", "book_id", "trade_date", "maturity", "ccy", "notional", "side", "fx_rate", "purpose"],
          [["OCP-1", "FX", "CORP_FX", AS_OF, "2026-10-14", "USD", -1.1e6, "SELL", 81.47, "OCP_TRANSFER"]])
        w(inp / "pnl_official.csv", ["as_of", "book_id", "position_id", "pnl_rub", "carry", "funding", "fx"], [[AS_OF, "OFZ", "P1", 100, 60, -20, 0]])
        w(inp / "instruments.csv", ["instrument_id", "name", "issuer_id", "asset_class", "segment", "ccy", "face_value", "maturity", "coupon_type",
                                    "call_date", "is_subord"], [["X1", "Банк суборд", "B", "Корп RUB фикс", "Корп", "CHF", 1000, "2099-01-01", "fix", "2028-01-01", 1]])
        reps = load_paths(self.db, [inp])
        for r in reps:
            self.assertEqual(r["errors"], [], r["file"])
        con = connect(self.db)
        self.assertEqual(con.execute("SELECT book_type, entity, ocp_to FROM ref_book WHERE book_id='CORP_FX'").fetchone()[:], ("TRADING", "Головной банк", "FXB"))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM ref_funding_rate WHERE book_type='' AND book_id='SECUR'").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT SUM(balance) FROM cash_snapshot").fetchone()[0], 3e9 + 5e6)
        self.assertEqual(con.execute("SELECT effective_date FROM ref_cbr_meeting WHERE date='2026-12-18'").fetchone()[0], None)
        self.assertEqual(con.execute("SELECT contract_rate FROM pos_snapshot").fetchone()[0], 11.85)
        self.assertEqual(con.execute("SELECT purpose FROM ref_otc_deal").fetchone()[0], "OCP_TRANSFER")
        self.assertEqual(con.execute("SELECT carry, funding, fx FROM pnl_official").fetchone()[:], (60, -20, 0))
        self.assertEqual(con.execute("SELECT call_date, is_subord FROM ref_instrument").fetchone()[:], ("2028-01-01", 1))
        # курсы ЦБ по дате действия: на выходные и на понедельник — курс, установленный в пятницу
        api = lambda r, **q: handle(con, r, q)  # noqa: E731
        self.assertEqual(api("market/fx-cbr", as_of="2026-10-12")["fx"]["USD"]["rate"], 80.5)
        fx = api("market/fx-cbr", as_of=AS_OF)["fx"]["USD"]
        self.assertEqual((fx["rate"], fx["next"]), (81.12, 81.47))
        self.assertAlmostEqual(fx["move_pct"], (81.12 / 80.5 - 1) * 100)
        self.assertEqual(api("cbr-meetings")["meetings"][1], {"date": "2026-12-18", "effective": "2026-12-21"})   # следующий рабочий день
        self.assertEqual(len(api("funding-rules")["rules"]), 3)
        self.assertIsNone(api("funding-rules")["rules"][0]["desk"])
        self.assertEqual(len(api("cash-balances", book="FXB")["balances"]), 2)
        cov = api("coverage")
        self.assertIn("cash_balances", cov["datasets"])
        self.assertNotIn("2026-10-10", [x["date"] for x in cov["days"]])   # курс ЦБ на субботу не добавляет день
        # справочник ставок заменяется файлом целиком
        w(inp / "funding_rates.csv", ["ccy", "index_name"], [["CNY", "RUSFAR CNY"]])
        load_paths(self.db, [inp / "funding_rates.csv"])
        self.assertEqual([tuple(r) for r in con.execute("SELECT ccy, book_type FROM ref_funding_rate")], [("CNY", "")])
        con.close()

    def test_bad_rows_rejected(self):
        w(self.dir / "funding_rates.csv", ["book_type", "ccy", "index_name", "fixed_rate"], [["TRADING", "RUB", "", ""], ["BOND", "RUB", "КС", ""]])
        w(self.dir / "books.csv", ["book_id", "name", "desk", "book_type"], [["X", "X", "D", "FUND"]])
        reps = {Path(r["file"]).name: r for r in load_paths(self.db, [self.dir / "funding_rates.csv", self.dir / "books.csv"])}
        errs = reps["funding_rates.csv"]["errors"]
        self.assertTrue(any("index_name или fixed_rate" in e for e in errs) and any("book_type" in e for e in errs))
        self.assertTrue(reps["books.csv"]["errors"])


# ---------------------------------------------------------------------- справочник ставок
class Rules(unittest.TestCase):
    RULES = [
        {"book_type": "TRADING", "ccy": "RUB", "index_name": "КС", "spread_bp": 30, "note": "режим"},
        {"desk": "Credit", "ccy": "RUB", "index_name": "КС", "spread_bp": 40, "note": "деск"},
        {"entity": "Дочерний банк", "ccy": "RUB", "index_name": "КС", "spread_bp": 45, "note": "юрлицо"},
        {"book_id": "SECUR", "ccy": "RUB", "index_name": "КС", "spread_bp": 50, "note": "книга"},
        {"instrument_id": "RU000A1", "ccy": "RUB", "index_name": "КС", "spread_bp": 10, "note": "бумага"},
        {"entity": "Дочерний банк", "instrument_id": "ИЦБ 2Р-12R", "ccy": "RUB", "index_name": "КС", "spread_bp": 20, "note": "юрлицо × бумага"},
        {"book_type": "TRADING", "ccy": "USD", "fixed_rate": 5.0, "note": "hard ccy"},
    ]

    def pick(self, book, ccy="RUB", pos=None):
        r = resolve_funding_rule(self.RULES, book, ccy, pos)
        return r and r["note"]

    def test_priority(self):
        b = {"book_id": "OFZ", "book_type": "TRADING", "desk": "Rates", "entity": "Головной банк"}
        self.assertEqual(self.pick(b), "режим")                                         # 1
        self.assertEqual(self.pick(dict(b, desk="Credit")), "деск")                      # 2 > 1
        self.assertEqual(self.pick(dict(b, desk="Credit", entity="Дочерний банк")), "юрлицо")   # 4 > 2
        sec = {"book_id": "SECUR", "book_type": "TRADING", "desk": "Credit", "entity": "Дочерний банк"}
        self.assertEqual(self.pick(sec), "книга")                                        # 8 > 4
        self.assertEqual(self.pick(sec, pos={"isin": "RU000A1"}), "бумага")              # 16 > 8
        self.assertEqual(self.pick(sec, pos={"name": "ИЦБ 2Р-12R"}), "юрлицо × бумага")  # 20 > 8
        self.assertEqual(self.pick(b, pos={"name": "ИЦБ 2Р-12R"}), "режим")             # бумага, но юрлицо другое
        self.assertEqual(self.pick(b, "USD"), "hard ccy")
        self.assertIsNone(self.pick(b, "CNY"))
        self.assertIsNone(self.pick(dict(b, book_type="DERIVS")))


# ---------------------------------------------------------------------- фондирование
def bond(pid, book, ccy, mv, notional=None, accrued=0.0, **kw):
    return dict({"position_id": pid, "book_id": book, "instr_type": "BOND", "asset_class": "x", "segment": "Корп", "name": pid,
                 "issuer": "I", "ccy": ccy, "maturity": "2030-01-01", "notional": notional or mv, "mv": mv, "mv_rub": mv, "dv01": 0,
                 "accrued": accrued, "pnl": {"carry": 1.0}}, **kw)


class Derivation(Base):
    """Понедельник 2026-10-12: начисление за сб, вс, пн (3 дня); новая КС действует с воскресенья."""
    MON, FRI = "2026-10-12", "2026-10-09"

    def setUp(self):
        super().setUp()
        con = init_db(self.db)
        ts = now_iso()
        with con:
            con.executemany("INSERT INTO md_fixing (date, index_name, value, updated_at) VALUES (?,?,?,?)",
                            [("2026-09-15", "КС", 16.0, ts), ("2026-10-11", "КС", 17.0, ts),          # КС по дате вступления в силу
                             ("2026-10-09", "SOFR", 4.0, ts), ("2026-10-12", "SOFR", 4.4, ts),
                             ("2026-10-09", "RUONIA", 15.0, ts)])
            con.executemany("INSERT INTO cash_snapshot (as_of, book_id, ccy, balance, updated_at) VALUES (?,?,?,?,?)",
                            [(self.MON, "FXB", "USD", 1e6, ts), (self.MON, "FXB", "RUB", -1e9, ts)])
            con.execute("INSERT INTO fund_snapshot (as_of, deal_id, book_id, type, ccy, amount, rate, maturity, updated_at)"
                        " VALUES (?, 'REPO-1', 'OFZ', 'РЕПО', 'RUB', 5e8, 15.5, '2026-10-19', ?)", (self.MON, ts))
        con.close()

    def publish(self, positions, rules=None):
        with Run(self.db, as_of=self.MON, contract_version="1.3") as run:
            run.add_books([{"book_id": "OFZ", "name": "ОФЗ", "desk": "Rates", "book_type": "TRADING", "entity": "E"},
                           {"book_id": "FXB", "name": "FX", "desk": "FX", "book_type": "DERIVS", "entity": "E"},
                           {"book_id": "HTM", "name": "HTM", "desk": "Rates", "book_type": "HTM", "entity": "E"}])
            run.add_issuers([{"issuer": "I", "issuer_group": "I"}])
            run.add_curves([])
            run.add_fx([{"ccy": "USD", "rate": 80.0, "move_pct": 1.0, "rate_cbr": 79.0, "move_pct_cbr": 0.5, "rate_cbr_next": 80.2}])
            run.add_indices([{"index_name": "КС", "value": 17.0}, {"index_name": "SOFR", "value": 4.4}, {"index_name": "RUONIA", "value": 15.0}])
            run.add_funding_rules(rules if rules is not None else [
                {"book_type": "TRADING", "ccy": "RUB", "index_name": "КС", "spread_bp": 30},
                {"book_type": "TRADING", "ccy": "USD", "fixed_rate": 5.0}])
            run.add_positions(positions)
            self.made = run.derive_funding()
        return run

    def lines(self):
        return {f["id"]: f for f in self.desk()["funding"]}

    def test_pos_daily_rates_monday_and_ks_change(self):
        run = self.publish([bond("R1", "OFZ", "RUB", 1.01e9, 1e9, accrued=1.0)])
        self.assertEqual((run.prev_date, run.accrual_days), (self.FRI, 3))
        f = self.lines()["POS|R1"]
        self.assertAlmostEqual(f["amount"], -1e9)                                  # −чистая MV: 1.01e9 − 1e9 × 1 %
        expected = -1e9 * (16.3 + 17.3 + 17.3) / 100 / 365                          # сб — старая КС, вс и пн — новая
        self.assertAlmostEqual(f["pnl"]["carry"], expected, places=4)
        self.assertEqual((f["rate"], f["rateExpr"], f["benchmark"], f["dayBasis"], f["kind"], f["positionId"]), (17.3, "КС + 0.30%", "КС", 365, "POS", "R1"))
        p = self.desk()["positions"][0]["pnl"]
        self.assertAlmostEqual(p["funding"], expected, places=4)
        self.assertAlmostEqual(p["explained"], 1.0 + expected, places=4)
        d = self.desk()   # фондирование бумаги — в её PnL, в итог книги идёт только РЕПО (строка без position_id)
        self.assertAlmostEqual(d["fundingPnlByBook"]["OFZ"]["carry"], self.lines()["REPO-1"]["pnl"]["carry"], places=4)
        self.assertAlmostEqual(d["fundingPnlByBook"]["OFZ"]["inPositions"], expected, places=4)

    def test_pos_hard_ccy_interest_in_rub_at_cbr(self):
        self.publish([bond("U1", "OFZ", "USD", 1e6)])
        f = self.lines()["POS|U1"]
        self.assertAlmostEqual(f["pnl"]["carry"], -1e6 * 79.0 * 5.0 * 3 / 100 / 365, places=6)   # процент в RUB по курсу ЦБ
        gross = -1e6 * 80.0 * 0.01 / 1.01
        self.assertAlmostEqual(f["pnl"]["fxGross"], gross, places=6)
        self.assertAlmostEqual(f["pnl"]["fxAllocated"], gross, places=6)
        self.assertAlmostEqual(f["pnl"]["fxGrossCbr"], -1e6 * 79.0 * 0.005 / 1.005, places=6)
        self.assertEqual((f["pnl"]["fx"], f["pnl"]["inPosition"], f["fxDelta"]), (0, True, {"USD": -1e6}))
        p = self.desk()["positions"][0]["pnl"]
        self.assertAlmostEqual(p["fxFunding"], gross, places=6)                    # fx_funding бумаги = fx_allocated строки
        self.assertAlmostEqual(p["fxFundingCbr"], f["pnl"]["fxGrossCbr"], places=6)

    def test_cash_basis_360_vs_365_and_repo(self):
        self.publish([bond("R1", "OFZ", "RUB", 1e9)])
        ln = self.lines()
        usd, rub = ln["CASH|FXB|USD"], ln["CASH|FXB|RUB"]
        self.assertEqual((usd["dayBasis"], usd["index"], rub["dayBasis"], rub["index"]), (360, "SOFR", 365, "RUONIA"))
        self.assertAlmostEqual(usd["pnl"]["carry"], 1e6 * 80.0 * (4.0 + 4.0 + 4.4) / 100 / 360, places=6)   # рыночный курс, SOFR по дням
        self.assertAlmostEqual(rub["pnl"]["carry"], -1e9 * 15.0 * 3 / 100 / 365, places=4)
        self.assertAlmostEqual(usd["pnl"]["fx"], 1e6 * 80.0 * 0.01 / 1.01, places=6)   # FX остатка — на уровне книги
        self.assertAlmostEqual(usd["pnl"]["fxCbr"], 1e6 * 79.0 * 0.005 / 1.005, places=6)
        repo = ln["REPO-1"]
        self.assertEqual((repo["kind"], repo["positionId"], repo["book"]), ("REPO", None, "OFZ"))
        self.assertAlmostEqual(repo["pnl"]["carry"], 5e8 * 15.5 * 3 / 100 / 365, places=4)
        d = self.desk()
        self.assertAlmostEqual(d["fundingPnl"]["carry"], usd["pnl"]["carry"] + rub["pnl"]["carry"] + repo["pnl"]["carry"], places=4)
        self.assertEqual(self.made, {"POS": 1, "CASH": 2, "REPO": 1})

    def test_htm_contract_rate_on_purchase_price(self):
        self.publish([bond("H1", "HTM", "RUB", 8e8, 1e9, accrual_only=True, purchase_date="2024-03-12", purchase_price=90.0,
                           purchase_yield=12.0, contract_rate=11.0)])
        f = self.lines()["HTM|H1"]
        self.assertAlmostEqual(f["amount"], -9e8)
        self.assertAlmostEqual(f["pnl"]["carry"], -9e8 * 11.0 * 3 / 100 / 365, places=4)
        self.assertEqual((f["kind"], f["fixed"], f["start"], f["maturity"], f["fxDelta"]), ("HTM", 11.0, "2024-03-12", "2030-01-01", {}))
        p = self.desk()["positions"][0]
        self.assertTrue(p["accrualOnly"])
        self.assertEqual((p["purchasePrice"], p["contractRate"]), (90.0, 11.0))
        self.assertAlmostEqual(p["pnl"]["funding"], f["pnl"]["carry"], places=6)

    def test_missing_rule_warns(self):
        run = self.publish([bond("C1", "OFZ", "USD", 1e6)], rules=[])
        self.assertTrue(any("нет правила" in x for x in run.warnings))
        self.assertNotIn("POS|C1", self.lines())



class DerivationDemo(Base):
    def test_matches_prototype_on_demo(self):
        """Строки, построенные tdm, совпадают со строками data.js (демо-пакет) — POS, HTM, CASH, REPO."""
        con = init_db(self.db)
        with con:
            for f in BUNDLE["funding"]:
                if f["kind"] == "CASH":
                    con.execute("INSERT INTO cash_snapshot (as_of, book_id, ccy, balance, updated_at) VALUES (?,?,?,?,?)",
                                (AS_OF, f["book_id"], f["ccy"], f["amount"], now_iso()))
                if f["kind"] == "REPO":
                    con.execute("INSERT INTO fund_snapshot (as_of, deal_id, book_id, type, ccy, amount, index_name, spread_bp, rate, start_date,"
                                " maturity, counterparty_id, collateral, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (AS_OF, f["deal_id"], f["book_id"], f["type"], f["ccy"], f["amount"], f["index_name"], f["spread_bp"], f["rate"],
                                 f["start_date"], f["maturity"], f["counterparty"], f["collateral"], now_iso()))
        con.close()
        b = copy.deepcopy(BUNDLE)
        b["funding"] = []
        b.pop("ocp_transfers")
        b["run"]["derive_funding"] = True
        for p in b["positions"]:
            p["pnl"].update(funding=0, fx_funding=0, fx_funding_cbr=None)
        _, status, warns = import_bundle(self.db, b)
        self.assertEqual((status, warns), ("complete", []))
        d = self.desk()
        got = {(f["positionId"] or f["book"] + "|" + f["ccy"] + "|" + f["kind"]): f for f in d["funding"]}
        self.assertEqual(len(got), len(BUNDLE["funding"]))
        for f in BUNDLE["funding"]:
            g = got[f["position_id"] or f["book_id"] + "|" + f["ccy"] + "|" + f["kind"]]
            self.assertEqual((g["kind"], g["rateExpr"], g["benchmark"], g["dayBasis"]), (f["kind"], f["rate_expr"], f["benchmark"], f["day_basis"]))
            for a, e in ((g["amount"], f["amount"]), (g["rate"], f["rate"]), (g["pnl"]["carry"], f["pnl"]["carry"]),
                         (g["pnl"]["fxGross"], f["pnl"]["fx_gross"]), (g["pnl"]["fxAllocated"], f["pnl"]["fx_allocated"])):
                self.assertAlmostEqual(a, e, delta=1e-3)
        src = {p["position_id"]: p["pnl"] for p in BUNDLE["positions"]}
        for p in d["positions"]:
            self.assertAlmostEqual(p["pnl"]["funding"], src[p["id"]]["funding"], delta=1e-3)
            self.assertAlmostEqual(p["pnl"]["fxFunding"], src[p["id"]]["fx_funding"], delta=1e-3)
        ocp = {(t["from"], t["ccy"]): t for t in d["OCP_TRANSFERS"]}
        for t in BUNDLE["ocp_transfers"]:   # сводка передачи ОВП, построенная из сделок передачи
            self.assertAlmostEqual(ocp[(t["from"], t["ccy"])]["amount"], t["amount"], delta=1e-6)
            self.assertAlmostEqual(ocp[(t["from"], t["ccy"])]["before"], t["before"], delta=1.0)
            self.assertEqual(ocp[(t["from"], t["ccy"])]["rate"], t["rate"])


# ---------------------------------------------------------------------- официальный PnL на T+1
class OfficialT1(Base):
    def test_attach_to_published_run(self):
        b = copy.deepcopy(BUNDLE)
        for p in b["positions"]:
            p["pnl"]["official"] = None
        rid, status, warns = import_bundle(self.db, b)
        self.assertEqual(status, "complete")
        d0 = self.desk()
        self.assertFalse(d0["official"]["received"])
        self.assertTrue(all(p["pnl"]["official"] is None and p["pnl"]["unexplained"] == 0 for p in d0["positions"]))
        con = connect(self.db, readonly=True)
        body0 = server._desk_json(con, resolve_run(con))
        con.close()
        src = {p["position_id"]: p["pnl"]["official"] for p in BUNDLE["positions"]}
        w(self.dir / "pnl_official.csv", ["as_of", "book_id", "position_id", "pnl_rub"],
          [[AS_OF, p["book_id"], p["position_id"], src[p["position_id"]]] for p in BUNDLE["positions"]] + [[AS_OF, "OFZ", "GONE", 1], [AS_OF, "OFZ", "", 5]])
        self.assertEqual(load_paths(self.db, [self.dir / "pnl_official.csv"])[0]["errors"], [])
        out = io.StringIO()
        from tdm.cli import main
        with redirect_stdout(out):
            main(["--db", self.db, "attach-official", "--as-of", AS_OF])
        self.assertIn(f"прогон {rid}", out.getvalue())
        con = connect(self.db, readonly=True)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM calc_run").fetchone()[0], 1)   # нового прогона нет
        self.assertNotEqual(server._desk_json(con, resolve_run(con)), body0)                 # кэш сервера сброшен
        hist = con.execute("SELECT pnl_official, run_id FROM desk_history WHERE as_of=?", (AS_OF,)).fetchone()
        con.close()
        d = self.desk()
        self.assertTrue(d["official"]["received"])
        self.assertIsNotNone(d["official"]["receivedAt"])
        for p in d["positions"]:
            self.assertAlmostEqual(p["pnl"]["official"], src[p["id"]], places=6)
            self.assertAlmostEqual(p["pnl"]["unexplained"], src[p["id"]] - p["pnl"]["explainedCbr"], places=6)
        self.assertEqual(hist[1], rid)
        self.assertAlmostEqual(hist[0], d["history"][-1]["pnlOfficial"], places=3)
        r = attach_official_pnl(self.db, run_id=rid, overwrite=False)
        self.assertEqual((r["updated"], r["missing"]), (0, ["GONE"]))
        with self.assertRaises(ValueError):
            attach_official_pnl(self.db, as_of="2000-01-01")


# ---------------------------------------------------------------------- проверки публикации
class Checks13(Base):
    def imp(self, b):
        return import_bundle(self.db, b)

    def test_pos_line_without_position_or_wrong_book_type(self):
        b = copy.deepcopy(BUNDLE)
        b["funding"][0]["position_id"] = "NOPE"
        with self.assertRaises(ValidationError) as cm:
            self.imp(b)
        self.assertIn("нет позиции", str(cm.exception))
        b = copy.deepcopy(BUNDLE)
        irs = next(p for p in b["positions"] if p["instr_type"] == "IRS")
        b["funding"][0]["position_id"] = irs["position_id"]                    # бумага книги DERIVS
        with self.assertRaises(ValidationError) as cm:
            self.imp(b)
        self.assertIn("ожидается TRADING", str(cm.exception))

    def test_htm_book_has_no_risk(self):
        b = copy.deepcopy(BUNDLE)
        next(p for p in b["positions"] if p["book_id"] == "HTM")["risk"] = {"RUB RUONIA": {"5Y": -1000}}
        with self.assertRaises(ValidationError) as cm:
            self.imp(b)
        self.assertIn("HTM", str(cm.exception))

    def test_bad_kind_and_book_type(self):
        b = copy.deepcopy(BUNDLE)
        b["funding"][-1]["kind"] = "LOAN"
        b["books"][0]["book_type"] = "BANKING"
        with self.assertRaises(ValidationError) as cm:
            self.imp(b)
        self.assertIn("kind='LOAN'", str(cm.exception))
        self.assertIn("book_type='BANKING'", str(cm.exception))

    def test_fx_funding_mismatch_and_ocp_residual_warn(self):
        b = copy.deepcopy(BUNDLE)
        line = next(f for f in b["funding"] if f["kind"] == "POS" and f["ccy"] == "USD")
        next(p for p in b["positions"] if p["position_id"] == line["position_id"])["pnl"]["fx_funding"] += 5.0
        b["positions"] = [p for p in b["positions"] if not (p.get("extra") or {}).get("ocp_transfer")]   # ОВП не передана
        _, status, warns = self.imp(b)
        self.assertEqual(status, "complete")
        self.assertTrue(any(line["position_id"] in x and "fx_allocated" in x for x in warns))
        self.assertTrue(any("CORP_FX" in x and "ОВП USD" in x for x in warns))

    def test_demo_has_no_warnings(self):
        _, status, warns = self.imp(BUNDLE)
        self.assertEqual((status, warns), ("complete", []))


# ---------------------------------------------------------------------- форма window.DESK = prototypes/data.js
@unittest.skipUnless(shutil.which("node"), "node не установлен")
class PrototypeParity(Base):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(ROOT / "tools" / "export_demo_bundle.js"), "--desk"], capture_output=True, text=True, encoding="utf-8", check=True)
        cls.ref = json.loads(out.stdout)

    def test_desk_matches_data_js(self):
        import_bundle(self.db, BUNDLE)
        a, r = self.desk(), self.ref
        self.assertLessEqual(set(r) - {"demo"}, set(a))
        pid = lambda i: f"P{int(i):04d}"  # noqa: E731
        rp, ap = {pid(p["id"]): p for p in r["positions"]}, {p["id"]: p for p in a["positions"]}
        rf, af = {f"F{int(f['id']):04d}": f for f in r["funding"]}, {f["id"]: f for f in a["funding"]}
        self.assertEqual(set(rp), set(ap))
        self.assertEqual(set(rf), set(af))
        self.assertEqual(list(r["BOOKS"]), list(a["BOOKS"]))
        self.assertEqual(r["BOOKS"], a["BOOKS"])
        for k in rp:
            self.assertLessEqual(set(rp[k]) - {"id"}, set(ap[k]), k)
            self.assertLessEqual(set(rp[k]["pnl"]), set(ap[k]["pnl"]), k)
        for k in rf:
            self.assertLessEqual(set(rf[k]), set(af[k]), k)
            self.assertLessEqual(set(rf[k]["pnl"]), set(af[k]["pnl"]), k)
            self.assertEqual(af[k]["positionId"], None if rf[k]["positionId"] is None else pid(rf[k]["positionId"]))
        for key in ("FX", "FX_CBR", "FX_CBR_MOVE_PCT", "FX_CBR_NEXT", "FUNDING_RULES", "OCP_TRANSFERS", "CBR_MEETINGS", "CARRY_SCENARIOS",
                    "CARRY_HORIZONS", "prevDate", "accrualDays", "JTD_RECOVERY", "OFZ_SPREAD_MOVE", "INDICES"):
            self.assertEqual(a[key], r[key], key)
        self.assertEqual({k: a["official"][k] for k in r["official"]}, r["official"])
        tot = lambda ps, k: sum(p["pnl"][k] for p in ps)  # noqa: E731
        for k in ("explained", "explainedCbr", "fxBasis", "official", "unexplained", "funding", "fxFunding", "fxCbr"):
            self.assertAlmostEqual(tot(a["positions"], k), tot(r["positions"], k), delta=1.0, msg=k)
        for k in ("carry", "fx", "fxCbr"):
            self.assertAlmostEqual(a["fundingPnl"][k], r["fundingPnl"][k], delta=1.0, msg=k)
        for b, x in r["fundingPnlByBook"].items():
            for k, v in x.items():
                self.assertAlmostEqual(a["fundingPnlByBook"][b][k], v, delta=1.0, msg=f"{b}.{k}")

        def ocp(d):
            out = {}
            for x in d["positions"] + d["funding"]:
                for c, v in x["fxDelta"].items():
                    out[(x["book"], c)] = out.get((x["book"], c), 0) + v
            return out
        ro, ao = ocp(r), ocp(a)
        self.assertEqual(set(ro), set(ao))
        for k in ro:
            self.assertAlmostEqual(ao[k], ro[k], delta=1.0, msg=str(k))
        for k in ("pnlOfficial", "pnlExplained", "dv01", "cs01"):
            self.assertAlmostEqual(a["history"][-1][k], r["history"][-1][k], delta=1.0, msg=k)
        self.assertEqual(len(a["history"]), len(r["history"]))


# ---------------------------------------------------------------------- совместимость с 1.2
class Bundle12(Base):
    def test_v12_bundle_still_imports(self):
        b = to_v12(BUNDLE)
        rid, status, warns = import_bundle(self.db, b)
        self.assertEqual(status, "complete")
        self.assertFalse(any("book_type" in x for x in warns))    # 1.2 — без требований 1.3
        d = self.desk()
        self.assertEqual(d["source"]["contractVersion"], "1.2")
        self.assertEqual(d["FX_CBR"], d["FX"])
        self.assertEqual(d["BOOKS"]["OFZ"]["bookType"], "TRADING")
        self.assertEqual(d["BOOKS"]["FXB"]["bookType"], "DERIVS")
        self.assertEqual(d["FUNDING_RULES"], [])
        self.assertTrue(all(f["kind"] == "DEAL" and not f["pnl"]["inPosition"] for f in d["funding"]))
        self.assertTrue(all(p["pnl"]["fxBasis"] == 0 and p["pnl"]["funding"] == 0 for p in d["positions"]))
        for p in d["positions"]:
            self.assertAlmostEqual(p["pnl"]["explained"] + p["pnl"]["unexplained"], p["pnl"]["official"], places=6)

    def test_schema_accepts_both(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema не установлен")
        schema = json.loads((ROOT / "contract" / "bundle.schema.json").read_text(encoding="utf-8"))
        jsonschema.validate(BUNDLE, schema)
        jsonschema.validate(to_v12(BUNDLE), schema)


if __name__ == "__main__":
    unittest.main()
