"""Запуск JS-тестов сценарного движка (нужен node; без него тест пропускается)."""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "node не установлен")
class ScenarioEngineJs(unittest.TestCase):
    def test_scenario_engine(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "scenario_engine.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_bonds(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "bonds.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_funding_carry(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "funding_carry.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_desk_scope(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "desk_scope.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_pnl_absence_and_curves_without_points(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "pnl_r10.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_pnl_absence_on_board_pivot_and_lab(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "pnl_r10_pages.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_coupon_display(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "coupon_r16.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_ccy_kind_table(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "ccy_kind.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_fx_forward_ui(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "fx_forward_ui.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_ui1_hints_group_sort_card_risk(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "ui1_batch.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_alias_ui(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "alias_ui.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_history_points_and_coverage(self):  # R44, R45
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "history_coverage.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_desk_updater_browser_check(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "desk_updater.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_batch2_columns_matrix_minfin_fx_tile(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "batch2_ui.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_batch1_floater_dm_history(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "batch1_dm.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_funding_b1_ui(self):
        r = subprocess.run(["node", str(ROOT / "tests" / "js" / "funding_b1.test.js")], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
