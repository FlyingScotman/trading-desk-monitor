"""Страницы связаны: общая строка навигации (prototypes/desk-nav.js) на каждой странице, до загрузчика данных;
фильтр Desk Monitor (desk-scope.js) — на остальных страницах с данными деска. python -m unittest discover -s tests"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTO = ROOT / "prototypes"
PAGES = ["index.html", "desk-monitor.html", "widget-board.html", "pivot-studio.html", "scenario-lab.html", "settings.html",
         "users.html"]
LOGIN = "login.html"  # открыт без входа: без строки и без сторонних скриптов (backend api/auth.py PUBLIC)
DATA_PAGES = ["desk-monitor.html", "widget-board.html", "pivot-studio.html", "scenario-lab.html"]
# Основной набор — мониторы (concepts/), «Сценарии (точно)» (app/) и Настройки; ранние экраны устарели (22.10.2026):
# файлы остаются и открываются по прямому адресу, но интерфейс их не предлагает (README.md, «Устаревшие экраны»)
MAIN = ["concepts/index.html", "app/", "settings.html"]
DEPRECATED = DATA_PAGES


def scripts(html):
    return re.findall(r'<script src="([^"]+)"', html)


class Pages(unittest.TestCase):
    def test_every_page_has_the_nav_before_the_loader(self):
        self.assertEqual(sorted(p.name for p in PROTO.glob("*.html")), sorted([*PAGES, LOGIN]))
        self.assertEqual(scripts((PROTO / LOGIN).read_text(encoding="utf-8")), [])
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

    def test_day_status_polls_the_feed_from_an_empty_store(self):
        # C14 (#25): на свежей базе событий нет (last_event_id = null); курсор ленты тогда 0, иначе опрос
        # /api/v1/events?after= не начнётся и уведомления о первых событиях не придут
        src = (PROTO / "concepts" / "c14-day-status.html").read_text(encoding="utf-8")
        self.assertIn("lastId = d.last_event_id == null ? 0 : d.last_event_id", src)
        self.assertRegex(src, r"function poll\(\) \{\s*if \(document\.hidden \|\| lastId == null\) return;")
        self.assertIn("'api/v1/events?after=' + lastId", src)

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

    def test_settings_page_sections_and_forms(self):
        # R41: разделы с навигацией слева; ссылки других страниц (#up — desk-updater.js, #bf — kit.js) ведут в раздел.
        # R40: формы адаптера — settings-forms.js, после строки навигации, в стиле мониторов (kit.css)
        html = (PROTO / "settings.html").read_text(encoding="utf-8")
        for sec in ("hub", "adapter", "entities", "books", "funding", "auto", "up", "diag"):
            self.assertIn(f'href="#{sec}" data-sec="{sec}"', html, sec)
            self.assertRegex(html, rf'<section class="sec" id="[^"]+" data-sec="{sec}"', sec)
        for anchor in ("up", "up-gh", "up-refresh", "bf", "rc", "dg"):
            self.assertIn(f'id="{anchor}"', html, anchor)
        src = scripts(html)
        self.assertLess(src.index("desk-nav.js"), src.index("settings-forms.js"))
        self.assertIn('href="concepts/lib/kit.css"', html)
        self.assertIn("concepts-theme", html)  # та же тема, что у мониторов

    def test_settings_page_hosts_the_users_section(self):
        # пакет 1, п. 9: раздел «Пользователи» (desk-users.js) — в настройках, слева в навигации; users.html — переход туда
        html = (PROTO / "settings.html").read_text(encoding="utf-8")
        self.assertIn('href="#users" data-sec="users"', html)
        self.assertRegex(html, r'<section class="sec" id="sec-users" data-sec="users"')
        self.assertIn("<section class=\"bd\" data-desk-users>", html)
        self.assertIn("'desk-users.js'", html)
        self.assertIn("START_HERE.txt, раздел 9", html)  # вход выключен: как включить
        users = (PROTO / "users.html").read_text(encoding="utf-8")
        self.assertIn("settings.html#users", users)
        self.assertIn("/settings.html#users", (PROTO / "desk-auth.js").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
