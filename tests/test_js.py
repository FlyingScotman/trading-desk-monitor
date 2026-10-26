"""Запуск всех JS-тестов `tests/js/*.test.js` одним проходом `node --test` (нужен node; без него тест пропускается).

Файлы берутся по маске, а не списком: новый тест подключается сам (#235).
"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "node не установлен")
class JsTests(unittest.TestCase):
    def test_all_js_tests(self):
        files = sorted(str(p) for p in (ROOT / "tests" / "js").glob("*.test.js"))
        self.assertTrue(files, "нет tests/js/*.test.js")
        r = subprocess.run(["node", "--test", *files], capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
        self.assertEqual(r.returncode, 0, r.stdout[-20000:] + r.stderr[-5000:])
