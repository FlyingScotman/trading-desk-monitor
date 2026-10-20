"""Страницы связаны: общая строка навигации (prototypes/desk-nav.js) на каждой странице, до загрузчика данных;
фильтр Desk Monitor (desk-scope.js) — на остальных страницах с данными деска. python -m unittest discover -s tests"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTO = ROOT / "prototypes"
PAGES = ["index.html", "desk-monitor.html", "control-center.html", "widget-board.html", "pivot-studio.html", "scenario-lab.html", "settings.html"]
DATA_PAGES = ["desk-monitor.html", "control-center.html", "widget-board.html", "pivot-studio.html", "scenario-lab.html"]


def scripts(html):
    return re.findall(r'<script src="([^"]+)"', html)


class Pages(unittest.TestCase):
    def test_every_page_has_the_nav_before_the_loader(self):
        self.assertEqual(sorted(p.name for p in PROTO.glob("*.html")), sorted(PAGES))
        for name in PAGES:
            src = scripts((PROTO / name).read_text(encoding="utf-8"))
            self.assertIn("desk-nav.js", src, name)
            if name in DATA_PAGES:
                self.assertLess(src.index("desk-nav.js"), src.index("desk-loader.js"), name)  # выбор прогона — в адрес раньше

    def test_react_app_loads_the_same_nav(self):
        for f in (ROOT / "frontend" / "index.html", ROOT / "frontend" / "dist" / "index.html"):
            self.assertIn('<script src="../desk-nav.js"></script>', f.read_text(encoding="utf-8"), f)

    def test_nav_links_every_page_relatively(self):
        nav = (PROTO / "desk-nav.js").read_text(encoding="utf-8")
        for page in [p for p in PAGES if p != "index.html"] + ["app/"]:
            self.assertIn(f"['{page}'", nav, page)
        self.assertNotIn("'/desk-", nav)  # относительные ссылки: работает и открытой как файл

    def test_demo_badge_is_not_hard_coded(self):
        for name in PAGES:
            self.assertNotIn("Тестовые данные</span>", (PROTO / name).read_text(encoding="utf-8"), name)

    def test_other_pages_apply_the_desk_monitor_filter(self):
        self.assertIn("desk-scope.js", (PROTO / "desk-loader.js").read_text(encoding="utf-8"))
        for name in DATA_PAGES:
            html = (PROTO / name).read_text(encoding="utf-8")
            self.assertEqual("DESK.scope.apply()" in html, name != "desk-monitor.html", name)  # у Desk Monitor свой фильтр


if __name__ == "__main__":
    unittest.main()
