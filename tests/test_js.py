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
