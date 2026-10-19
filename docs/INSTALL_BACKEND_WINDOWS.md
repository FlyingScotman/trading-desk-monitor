# Установка desk-бэкенда на рабочую машину (Windows)

Это установка нового бэкенда (`desk`: снапшоты рынка из хаба, оценка книг через core, EOD-прогоны, Desk Monitor и Scenario Lab). Старый прототип `tdm` ставится по [`INSTALL_WINDOWS.md`](INSTALL_WINDOWS.md); эти две установки независимы.

Бэкенду нужны пакеты (FastAPI, numpy, psycopg, встроенный PostgreSQL и др.). Доступ к PyPI и GitHub с рабочей машины не гарантирован, поэтому всё собирается **дома** в один zip-архив, переносится на работу и ставится **без интернета**. База (desk store) — встроенный PostgreSQL из пакета `pgserver`: у него есть готовая сборка для Windows и Python 3.11, отдельно ставить PostgreSQL не нужно.

## 1. Что понадобится

**Дома (Mac):** клон репозитория, [uv](https://docs.astral.sh/uv/), `git`, Python 3.11 с `pip` (тот, что ставит uv, подходит), интернет.

**На работе (Windows):**

- Python 3.11. Проверить:

  ```bat
  py -3.11 --version
  ```
  - `py` — стандартный Windows-лаунчер Python;
  - `-3.11` — выбрать именно Python 3.11 (другая версия не подойдёт: колёса в архиве собраны под 3.11);
  - `--version` — напечатать версию и выйти.

  Если команда не найдена, поставьте Python 3.11 с python.org или через корпоративный портал ПО (галочка «py launcher» в установщике должна стоять).
- Папка для бэкенда, например `C:\desk`: на локальном диске, путь **без пробелов и кириллицы** (PostgreSQL их не любит). Эта папка станет и виртуальным окружением Python, и «корнем» бэкенда: в ней будут настройки (`desk.env`) и данные (`.devdata\`).

## 2. Собрать архив дома

```bash
cd ~/Code/python/trading_desk_monitor
git status
python3.11 backend/tools/build_windows_bundle.py
```

- `git status` — убедиться, что всё закоммичено: скрипт отказывается собирать из незакоммиченных изменений (флаг `--allow-dirty` разрешает, но тогда архив не воспроизводится по коммиту);
- `python3.11 backend/tools/build_windows_bundle.py` — собрать архив. Если `python3.11` без `pip`, то так: `uv run --no-project --python 3.11 --with pip python backend/tools/build_windows_bundle.py`.

Скрипт (несколько минут, качает около 35 МБ):
1. берёт точные версии зависимостей из `backend/uv.lock` и добавляет `pgserver`; разрешает их для Windows (`uv pip compile --python-platform x86_64-pc-windows-msvc`);
2. скачивает колёса (`.whl`) для win_amd64 / CPython 3.11 в `wheelhouse\`;
3. собирает колёса `desk-backend`, `trading-desk-monitor` и `rub-fixed-income-core` (с GitHub, ровно тот коммит, что закреплён в `backend/pyproject.toml`);
4. кладёт рядом прототипы (`prototypes\`), собранный фронтенд (`frontend\dist\`), синтетическую книгу (`examples\test_book.csv`), шаблон настроек `desk.env.example`, `.bat`-скрипты и эту инструкцию;
5. пишет `MANIFEST.sha256` (контрольная сумма каждого файла) и печатает его, затем путь, размер и sha256 самого архива.

Результат: `dist/desk-backend-windows-<версия>.zip` (около 35 МБ, 47 файлов; `dist/` не попадает в git).

## 3. Перенести на работу

Скопируйте zip на рабочую машину (флешка, корпоративная почта — как разрешено). Сверьте контрольную сумму с той, что напечатал скрипт:

```bat
certutil -hashfile desk-backend-windows-0.1.0.zip SHA256
```

- `certutil` — встроенная утилита Windows; `-hashfile <файл> SHA256` — посчитать SHA-256 файла.

## 4. Установить

```bat
mkdir C:\desk
tar -xf desk-backend-windows-0.1.0.zip --strip-components 1 -C C:\desk
cd /d C:\desk
install_backend.bat
```

- `mkdir C:\desk` — создать папку;
- `tar -xf <архив>` — распаковать (`tar` встроен в Windows 10 и новее, zip он понимает); `--strip-components 1` — без верхней папки `desk-backend-windows-0.1.0` внутри архива; `-C C:\desk` — куда распаковать. Можно и проводником: распакуйте и перенесите *содержимое* папки `desk-backend-windows-0.1.0` в `C:\desk`;
- `cd /d C:\desk` — перейти в папку (`/d` — сменить заодно и диск);
- `install_backend.bat` — установка. Он:
  1. проверяет, что `py -3.11` есть;
  2. создаёт виртуальное окружение Python прямо в `C:\desk` (папки `Scripts\`, `Lib\`, файл `pyvenv.cfg`);
  3. ставит все пакеты из `wheelhouse\` без интернета (`pip install --no-index --find-links wheelhouse -r wheelhouse\requirements.txt`: `--no-index` — не ходить в PyPI, `--find-links` — брать колёса из папки, `-r` — список точных версий);
  4. проверяет, что бэкенд видит `C:\desk` своим корнем (там он ищет `prototypes\` и `frontend\dist\`);
  5. копирует `desk.env.example` в `desk.env`, если `desk.env` ещё нет;
  6. печатает настройки (`desk config`) и создаёт базу (`desk db-migrate`): при первом запуске встроенный PostgreSQL создаёт кластер в `C:\desk\.devdata\desk_pg` (10–30 секунд) и остаётся работать в фоне; следующие команды подключаются к нему.

Ни один `.bat` не требует прав администратора. Запускайте их из обычной (не «от имени администратора») командной строки.

## 5. Настроить: `desk.env`

Откройте `C:\desk\desk.env` в Блокноте. Это строки `КЛЮЧ=значение`; пустое значение — «не задано», тогда действует значение по умолчанию. Относительные пути считаются от `C:\desk`.

| Ключ | Что писать |
|---|---|
| `DESK_HUB_URL` | адрес хаба, `https://…` (его даёт владелец; в репозитории его нет) |
| `DESK_HUB_TOKEN_FILE` | путь к файлу с токеном хаба (scope `snapshots`), например `C:\Users\<имя>\.desk\hub.token`. Файл — одна строка с токеном. Не кладите токен в `C:\desk` и никуда, откуда он может уйти в git |
| `DESK_HUB_TOKEN` | токен прямо строкой — не используйте, если можно файл |
| `DESK_HUB_CA_BUNDLE` | PEM-файл с корневым сертификатом прокси, если хаб не открывается из-за TLS (раздел 11). Пусто — системное хранилище сертификатов Windows |
| `DESK_HUB_TIMEOUT_S` | таймаут запроса к хабу в секундах, по умолчанию 60 |
| `DESK_SNAPSHOT_CACHE` | папка проверенных снапшотов, по умолчанию `.devdata\snapshots` |
| `DESK_SNAPSHOT_DROP` | папка, куда вы руками кладёте снапшоты, если хаб недоступен (вариант Б ниже) |
| `DESK_STORE_URL` | пусто — встроенный PostgreSQL в `.devdata\desk_pg`. Или `postgresql://…` на отдельный сервер |
| `DESK_DB` | база прототипа `tdm` (только для `desk eod --tdm`), по умолчанию `.devdata\tdm.sqlite` |
| `DESK_REPORT_DIR` | папка отчётов сверки с MOEX, например `.devdata\reports`; пусто — без отчётов |

`HTTPS_PROXY` в `desk.env` **не читается**: это переменная окружения Windows (раздел 11).

Все `.bat` сами указывают бэкенду на этот файл. Чтобы запускать команды `desk` вручную, в каждом новом окне сначала:

```bat
cd /d C:\desk
set DESK_ENV_FILE=C:\desk\desk.env
Scripts\desk.exe config
```

- `set DESK_ENV_FILE=…` — где лежат настройки (действует до закрытия окна). Без этого `desk` их не найдёт и `desk config` покажет `hub_url not set`. Чтобы не повторять, один раз: `setx DESK_ENV_FILE C:\desk\desk.env` (`setx` сохраняет переменную для вашей учётной записи; действует в новых окнах);
- `Scripts\desk.exe config` — напечатать действующие настройки; токен показывается замаскированным.

Дальше снапшоты рынка попадают в кэш одним из двух способов.

**Вариант А — хаб доступен.** Заполните `DESK_HUB_URL` и `DESK_HUB_TOKEN_FILE`, затем:

```bat
Scripts\desk.exe hub-check
Scripts\desk.exe sync --last 2
```

- `hub-check` — версия хаба, проверка токена, последний опубликованный снапшот и его возраст;
- `sync --last 2` — скачать два последних опубликованных дня в кэш (каждый файл проверяется по sha256). Печатает даты — они понадобятся в разделе 6.

**Вариант Б — прокси не пускает к хабу (папка drop).** Дома в клоне репозитория:

```bash
cd backend && .venv/bin/desk sync --last 2
```

затем скопируйте файлы `<snapshot_id>.json.gz` из `.devdata/snapshots/` (в корне клона) на флешку. На работе в `desk.env` задайте `DESK_SNAPSHOT_DROP=C:\desk\drop`, положите файлы в `C:\desk\drop` и:

```bat
Scripts\desk.exe import-drop
```

- `import-drop` — проверить каждый файл так же, как при скачивании, и скопировать в кэш. Испорченный файл отклоняется с причиной и в кэш не попадает. Даты загруженных снапшотов печатает `desk sync` дома.

## 6. Первый прогон на синтетической книге

Книга `examples\test_book.csv` синтетическая: реальные публичные выпуски, выдуманные количества; реальных позиций в архиве нет.

```bat
cd /d C:\desk
set DESK_ENV_FILE=C:\desk\desk.env
Scripts\desk.exe eod --date 2026-10-15 --book examples\test_book.csv
Scripts\desk.exe runs
```

- `eod` — EOD-прогон одной даты;
- `--date 2026-10-15` — дата: подставьте **последнюю** дату, которую напечатал `sync` (или дату снапшотов из drop). Если в кэше есть и предыдущий рабочий день, к прогону приложится flash PnL против него;
- `--book examples\test_book.csv` — сначала загрузить позиции из файла в базу на эту дату (повторная загрузка того же файла ничего не добавляет); следующие прогоны без `--book` берут книги из базы;
- `runs` — список прогонов в базе: дата, тип, статус.

Прогон печатает ID снапшота, откуда он, когда опубликован и его возраст, а также оценку каждой книги с флагом качества.

## 7. Открыть Desk Monitor

```bat
cd /d C:\desk
run_backend.bat
```

- `run_backend.bat` — запустить `desk serve` (API, Desk Monitor, Scenario Lab) на порту 8000 и через несколько секунд открыть браузер. Остановить — `Ctrl+C` в окне. `run_backend.bat 8001` — то же на порту 8001.

Адреса:
- http://127.0.0.1:8000/desk-monitor.html — Desk Monitor на последнем прогоне (переключатель прогонов внизу слева);
- http://127.0.0.1:8000/app/ — Scenario Lab;
- http://127.0.0.1:8000/docs — описание API.

Сервер слушает только `127.0.0.1` (эту машину). У API нет авторизации, поэтому открывать его в сеть (`desk serve --host 0.0.0.0`) пока не нужно.

## 8. Каждый день

```bat
cd /d C:\desk
daily_backend.bat
```

- `daily_backend.bat` — снапшоты за предыдущий рабочий день и день до него из хаба (`desk eod --sync`), затем EOD-прогон книг из базы за предыдущий рабочий день;
- `daily_backend.bat drop` — то же, но снапшоты из папки drop (`desk import-drop`), если хаб недоступен;
- `daily_backend.bat hub 2026-10-16` — явная дата. «Предыдущий рабочий день» считается как предыдущий будний: праздники скрипт не знает, после праздника передайте дату сами.

## 9. Обновление

Соберите новый архив дома (раздел 2), распакуйте его **поверх** `C:\desk` той же командой `tar` и снова запустите `install_backend.bat`. Он остановит встроенный PostgreSQL (его файлы лежат в устанавливаемом пакете), обновит пакеты и применит новые миграции базы. `desk.env` и `.devdata\` (база, кэш снапшотов) не трогаются.

## 10. Подключение приватного адаптера (S7a)

> **Заготовка.** Этот раздел заполнится вместе со слайсом S7a, который делает другой исполнитель. Пока известно только то, что сказано в [`architecture/08_PRIVATE_ADAPTER_INTERFACE.md`](architecture/08_PRIVATE_ADAPTER_INTERFACE.md) §2 и §13:
> - `DESK_PRIVATE_ADAPTER` — какой адаптер читает приватные файлы: пусто — эталонный адаптер (канонический CSV); путь к папке пакета (например `C:\desk\trades-data-adapter\src\tda`) или имя установленного модуля;
> - `DESK_INBOX_DIRS` — папки входящих файлов через `;` (папка с `\**` на конце просматривается с подпапками).
>
> До S7a этих настроек в бэкенде нет. Когда они появятся, сборщик архива добавит их ключи в `desk.env.example` сам (он берёт список ключей из `desk/settings.py`).

## 11. Если что-то не так

| Симптом | Причина / что делать |
|---|---|
| `Python 3.11 not found by the py launcher` | нет Python 3.11 или лаунчера `py`: раздел 1 |
| `pip` пишет `No matching distribution found` | колесо не под эту машину: Python не 3.11 или не 64-битный. `py -3.11 -c "import platform; print(platform.architecture())"` должен показать `64bit` |
| `desk config` показывает `hub_url not set` | в этом окне не задан `DESK_ENV_FILE` (раздел 5) или `desk.env` пуст |
| `hub-check`: `CERTIFICATE_VERIFY_FAILED` | прокси подменяет TLS-сертификат, а его корневого сертификата нет в хранилище Windows. Попросите у ИТ корневой сертификат прокси в формате PEM (Base-64 `.cer`) и укажите путь в `DESK_HUB_CA_BUNDLE`. Этот файл **заменяет** системное хранилище, поэтому в нём должен быть именно корневой сертификат прокси |
| `hub-check`: таймаут или `Connection refused` | прокси не задан. Python берёт прокси из переменной `HTTPS_PROXY`, а если её нет — из системных настроек Windows. Задать явно: `set HTTPS_PROXY=http://<прокси>:<порт>` в окне (или `setx HTTPS_PROXY …` навсегда), затем `Scripts\desk.exe config` — строка `https_proxy` |
| `hub-check`: `407 Proxy Authentication Required` | прокси требует входа (NTLM/Kerberos), стандартная библиотека Python так не умеет. Используйте папку drop (раздел 5, вариант Б) |
| `Address already in use` / `[WinError 10048]` | порт занят: `run_backend.bat 8001`. Кто занял: `netstat -ano \| findstr :8000` (последняя колонка — PID процесса в Диспетчере задач) |
| `desk: the desk store is not migrated (desk db-migrate)` | база не создана или устарела: `Scripts\desk.exe db-migrate` (с `DESK_ENV_FILE`, раздел 5) или ещё раз `install_backend.bat` |
| PostgreSQL не стартует (`Failed to start server`, `Timeout starting server`) | смотрите журнал `C:\desk\.devdata\desk_pg\log`. Частые причины: окно запущено «от имени администратора»; в пути пробелы или кириллица; антивирус блокирует `postgres.exe` в `C:\desk\Lib\site-packages\pgserver\pginstall\bin` (попросите ИТ разрешить); после сбоя или жёсткой перезагрузки остался `.devdata\desk_pg\postmaster.pid` — если в Диспетчере задач нет процессов `postgres.exe`, удалите этот один файл и повторите |
| `install_backend.bat` падает на `pip`: «файл используется другим процессом» | работает встроенный PostgreSQL или `run_backend.bat`. Закройте окно сервера и выполните `Lib\site-packages\pgserver\pginstall\bin\pg_ctl.exe stop -D .devdata\desk_pg -m fast`, затем снова `install_backend.bat` |
| Встроенный PostgreSQL так и не заработал | запасной вариант — отдельный PostgreSQL (например, zip-архив бинарников EDB «PostgreSQL binaries», без установщика): создать кластер `initdb`, запустить `pg_ctl start`, создать базу `desk` и указать `DESK_STORE_URL=postgresql://<пользователь>:<пароль>@127.0.0.1:5432/desk` в `desk.env`. Тогда `pgserver` не используется |
| `desk eod`: нет снапшота за дату | этой даты нет в кэше: `sync --date <дата>` или `import-drop`; после праздника дату передайте явно |
| Кракозябры в консоли | `.bat` сами переключают консоль в UTF-8 (`chcp 65001`); при ручном запуске сделайте то же |
