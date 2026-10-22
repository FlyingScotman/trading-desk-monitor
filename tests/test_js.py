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
