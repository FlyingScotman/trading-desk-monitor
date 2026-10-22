"""Страницы связаны: общая строка навигации (prototypes/desk-nav.js) на каждой странице, до загрузчика данных;
фильтр Desk Monitor (desk-scope.js) — на остальных страницах с данными деска. python -m unittest discover -s tests"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTO = ROOT / "prototypes"
PAGES = ["index.html", "desk-monitor.html", "widget-board.html", "pivot-studio.html", "scenario-lab.html", "settings.html"]
DATA_PAGES = ["desk-monitor.html", "widget-board.html", "pivot-studio.html", "scenario-lab.html"]
# Основной набор — мониторы (concepts/), «Сценарии (точно)» (app/) и Настройки; ранние экраны устарели (22.10.2026):
# файлы остаются и открываются по прямому адресу, но интерфейс их не предлагает (README.md, «Устаревшие экраны»)
MAIN = ["concepts/index.html", "app/", "settings.html"]
DEPRECATED = DATA_PAGES


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

    def test_monitors_load_the_ccy_attribution_after_the_kit(self):
        # lib/instr.js — отнесение к валюте «по ногам» (DV01 по валюте кривой, ОВП по ключам FX-дельты): им пользуются
        # шапка, карточка и ОВП kit.js на каждом мониторе
        for f in sorted((PROTO / "concepts").glob("c*.html")):
            src = scripts(f.read_text(encoding="utf-8"))
            self.assertIn("lib/instr.js", src, f.name)
            self.assertLess(src.index("lib/kit.js"), src.index("lib/instr.js"), f.name)

    def test_monitor_tables_sort_groups_like_ordinary_fields(self):
        # R31: группы таблиц (валюта → эмитент…) сортируются щелчком по столбцу, как обычные поля, — общим правилом
        # KIT.groupKeys; ни один монитор не упорядочивает группы только своим порядком уровня
        for f in sorted((PROTO / "concepts").glob("c*.html")) + [PROTO / "concepts" / "lib" / "kit.js"]:
            src = f.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"keys\.sort\((g|G)\.order\)", src), f.name)
        self.assertIn("K.groupKeys(", (PROTO / "concepts" / "c03-blotter.html").read_text(encoding="utf-8"))

    def test_react_app_loads_the_same_nav(self):
        for f in (ROOT / "frontend" / "index.html", ROOT / "frontend" / "dist" / "index.html"):
            self.assertIn('<script src="../desk-nav.js"></script>', f.read_text(encoding="utf-8"), f)

    def test_nav_links_the_main_pages_relatively(self):
        nav = (PROTO / "desk-nav.js").read_text(encoding="utf-8")
        for page in MAIN:
            self.assertIn(f"['{page}'", nav, page)
        self.assertNotIn("'/desk-", nav)  # относительные ссылки: работает и открытой как файл

    def test_deprecated_screens_are_not_offered_but_kept(self):
        nav = (PROTO / "desk-nav.js").read_text(encoding="utf-8")
        pages = nav[nav.index("var PAGES = ["):nav.index("];", nav.index("var PAGES = ["))]
        home = (PROTO / "index.html").read_text(encoding="utf-8")
        for name in ("index.html", "lib/kit.js"):
            home += (PROTO / "concepts" / name).read_text(encoding="utf-8")
        for page in DEPRECATED:
            self.assertNotIn(page, pages, page)
            self.assertNotIn(f'href="{page}', home, page)
            self.assertNotIn(f"../{page}", home, page)
            html = (PROTO / page).read_text(encoding="utf-8")
            self.assertIn("DEPRECATED", html[:400], page)  # помечен, но не удалён
        for page in MAIN:
            self.assertIn(f'href="{page}"', (PROTO / "index.html").read_text(encoding="utf-8"), page)

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
