# Trading Desk Monitor

Монитор FI-деска: риски по кривым (key-rate DV01), кредитный риск, ОВП, фондирование, сценарии и PnL explain. Экраны в браузере,
расчёты и хранилище прогонов — бэкенд `desk` ([backend/README.md](backend/README.md)).

```
хаб рыночных данных ──► desk (ядро rub-fixed-income-core) ──► хранилище прогонов (PostgreSQL) ──► desk serve ──► браузеры
```

| Экран | Для чего |
|---|---|
| **Мониторы** (`prototypes/concepts/`, `/concepts/index.html`) | основной набор, 13 экранов: кокпит, лестница рисков, позиции с карточкой, агрегатор, утренний брифинг, плиточная доска, карта портфеля, календари выплат, рынок, PnL explain, динамика, терминал, эмитенты; общий фильтр книг и портфелей в шапке |
| **Сценарии (точно)** (`frontend/`, `/app/`) | Taylor в браузере и точная переоценка на бэкенде, невязка |
| **Настройки** (`settings.html`) | хаб, папки отчётов, адаптер, автопилот, обновление программы |

Лимиты и VaR сюда не входят: их считают отдельные модули.

Страницы связаны: строка навигации вверху главной, Настроек и React-приложения `/app/` ведёт на основные страницы и несёт общий
выбор прогона (дата/номер — во всех страницах один), показывает автопилот с кнопкой «Обновить» и плашку «Тестовые данные»,
только когда страница и правда на демо-наборе. У мониторов своя шапка: выбор прогона, фильтр книг и портфелей, поиск.

### Устаревшие экраны

С 22.10.2026 ранние экраны убраны из интерфейса: их нет в строке навигации, на главной и в мониторах.

Desk Monitor, Widget Board и Pivot Studio удалены (#292, шаг 3 плана в #107). Их старые адреса `desk serve` на один
релиз перенаправляет на мониторы, которые их заменили, с тем же выбором прогона (`?run_id=`); потом перенаправления
тоже уберём (`REMOVED_SCREENS` в `backend/src/desk/api/app.py`).

| Экран | Был файл | Адрес ведёт на | Замена в мониторах |
|---|---|---|---|
| Desk Monitor | `prototypes/desk-monitor.html` | C01 | C01 Кокпит, C02 Лестница рисков, C03 Позиции, C10 PnL explain |
| Widget Board | `prototypes/widget-board.html` | C06 | C06 Плиточная доска |
| Pivot Studio | `prototypes/pivot-studio.html` | C04 | C04 Агрегатор |

Scenario & PnL Lab (`prototypes/scenario-lab.html`) пока остаётся, открывается по прямому адресу и не развивается.
Части его сценариев в мониторах и в «Сценариях (точно)» (`/app/`) нет: редактор FX-шока, спреды по рейтингу и
сектору, исторические сценарии. Нужны ли они, решает владелец (#107); до его ответа остаются и файлы, без которых
Scenario Lab не работает: `desk-loader.js`, `desk-util.js`, `desk-scope.js`, `desk-fallback.js`, `desk-empty.js`,
демо-набор `data.js`. Фильтр позиций `desk-scope.js` применяет выбор, сохранённый ещё в Desk Monitor; снять его — ✕ в
строке навигации.

## Быстрый старт

Разработка и локальный запуск — [backend/README.md](backend/README.md) (`uv sync`, `desk sync`, `desk eod`, `desk serve`).
Установка на рабочую машину Windows — офлайн-сборка, [docs/INSTALL_BACKEND_WINDOWS.md](docs/INSTALL_BACKEND_WINDOWS.md).

```bash
cd backend && uv sync --python 3.11 --extra dev && .venv/bin/python -m pytest -q   # тесты бэкенда
backend/.venv/bin/python -m pytest -q tests                                       # корневые тесты (страницы, JS)
node --test tests/js/*.test.js                                                    # JS-тесты мониторов
```

Страницы можно открыть и просто как файлы (`prototypes/*.html`): тогда они работают на демо-наборе `prototypes/data.js`.

## Документы

| Документ | О чём |
|---|---|
| [docs/architecture/](docs/architecture/README.md) | **архитектура системы** (с 16.10.2026): компоненты, контракт рыночных данных ↔ core, разделение расчётов бэкенд/фронтенд, бэкенд, интеграция фронтенда, развёртывание, план развития core, roadmap, ADR |
| [docs/ORG_CONVENTIONS.md](docs/ORG_CONVENTIONS.md) | **общая методика** фондирования, FX, PnL, кривых и сценариев. Правила конкретной организации — в приватном адаптере (ADR-008) |
| [docs/SERVICE_DATA_MODEL.md](docs/SERVICE_DATA_MODEL.md), [docs/SCENARIOS.md](docs/SCENARIOS.md) | модель данных и сценарии времён прототипа; часть ещё актуальна как описание требований |

### Прототип `tdm` (удалён)

Первый прототип — пакет `tdm` (SQLite, `python -m tdm serve`, контракт данных 1.3) — удалён 26.10.2026 (#295): бэкенд
`desk` его больше не использует. Его документы оставлены как история и помечены устаревшими:
[docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md), [docs/DATA_REQUIREMENTS.md](docs/DATA_REQUIREMENTS.md) (шаблоны
`contract/csv/`, `contract/bundle.schema.json`), [docs/INSTALL_WINDOWS.md](docs/INSTALL_WINDOWS.md). Входные данные
деска бэкенд читает через приватный адаптер, контракт — [`contract/private/v1/`](contract/private/v1/README.md).

## Структура

```
prototypes/   страницы UI, desk-nav.js (строка навигации, общий прогон, автопилот), desk-loader.js (откуда брать данные),
              desk-util.js (расчёты в браузере), desk-scope.js, data.js (демо) — для устаревшего Scenario Lab
backend/      бэкенд desk: рыночные данные, оценка через ядро, PnL, прогоны, API, сборка Windows
frontend/     React-приложение «Сценарии (точно)» (/app/)
contract/     private/v1 — входной контракт приватного адаптера; csv/ и bundle.schema.json — контракт 1.3 прототипа (устарел)
docs/         архитектура, методика, установка
tests/        корневые тесты: страницы (test_pages.py), JS-тесты мониторов (tests/js, запуск — test_js.py)
```

Прототипы перенесены из `Balance-sheet-playground` (ветка `claude/zen-ritchie-ao73d8`).
