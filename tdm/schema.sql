-- Trading Desk Monitor: схема хранилища результатов расчётов (SQLite).
-- Контракт данных описан в docs/DATA_CONTRACT.md; версия схемы — в meta.schema_version.
-- Все суммы — RUB, если в имени колонки не указано иное. Даты — ISO 'YYYY-MM-DD',
-- метки времени — ISO 'YYYY-MM-DDTHH:MM:SS' (локальное время сервера расчётов).

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Глобальные справочники (одинаковы для всех прогонов)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ref_bucket (
  bucket       TEXT PRIMARY KEY,            -- '1M','3M',...,'20Y+'
  ord          INTEGER NOT NULL UNIQUE,     -- порядок на оси
  pillar_years REAL NOT NULL                -- срок пиллара в годах
);

CREATE TABLE IF NOT EXISTS ref_rating (
  rating TEXT PRIMARY KEY,                  -- 'AAA','AA+',...
  ord    INTEGER NOT NULL UNIQUE            -- 0 = лучший
);

CREATE TABLE IF NOT EXISTS scenario_def (
  scenario_id     TEXT PRIMARY KEY,         -- 'keyhike'
  name            TEXT NOT NULL,
  ord             INTEGER NOT NULL DEFAULT 0,
  definition_json TEXT NOT NULL,            -- {"curves":{...},"fx":{...},"spreads":{...}}
  active          INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  updated_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Расчётный прогон: единица публикации. UI видит только status = 'complete'.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS calc_run (
  run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of            TEXT NOT NULL,           -- дата среза
  run_type         TEXT NOT NULL CHECK (run_type IN ('EOD', 'INTRADAY', 'TEST')),
  status           TEXT NOT NULL CHECK (status IN ('loading', 'complete', 'failed')),
  contract_version TEXT NOT NULL,
  source           TEXT,                    -- имя/хост расчётного сервиса
  lib_version      TEXT,                    -- версия расчётной библиотеки
  market_snapshot  TEXT,                    -- идентификатор среза рыночных данных
  comment          TEXT,
  started_at       TEXT NOT NULL,
  published_at     TEXT,                    -- момент перехода в 'complete'
  error            TEXT                     -- причина 'failed'
);
CREATE INDEX IF NOT EXISTS ix_calc_run_asof ON calc_run (as_of, status, run_id);

CREATE TABLE IF NOT EXISTS calc_run_log (
  run_id  INTEGER NOT NULL REFERENCES calc_run (run_id) ON DELETE CASCADE,
  ts      TEXT NOT NULL,
  level   TEXT NOT NULL CHECK (level IN ('INFO', 'WARN', 'ERROR')),
  message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_calc_run_log ON calc_run_log (run_id);

-- ---------------------------------------------------------------------------
-- Статика на прогон (снимок: прогон воспроизводим, даже если справочник позже изменится)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_book (
  run_id  INTEGER NOT NULL REFERENCES calc_run (run_id) ON DELETE CASCADE,
  book_id TEXT NOT NULL,                    -- 'OFZ'
  name    TEXT NOT NULL,
  desk    TEXT NOT NULL,                    -- 'Rates' / 'Credit' / ...
  trader  TEXT,
  ord     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, book_id)
);

CREATE TABLE IF NOT EXISTS run_issuer (
  run_id       INTEGER NOT NULL REFERENCES calc_run (run_id) ON DELETE CASCADE,
  issuer       TEXT NOT NULL,
  issuer_group TEXT NOT NULL,
  sector       TEXT,
  rating       TEXT,                        -- из ref_rating
  is_gov       INTEGER NOT NULL DEFAULT 0 CHECK (is_gov IN (0, 1)),
  is_quasi     INTEGER NOT NULL DEFAULT 0 CHECK (is_quasi IN (0, 1)),
  PRIMARY KEY (run_id, issuer)
);

CREATE TABLE IF NOT EXISTS run_curve (
  run_id   INTEGER NOT NULL REFERENCES calc_run (run_id) ON DELETE CASCADE,
  curve_id TEXT NOT NULL,                   -- 'RUB OFZ'
  name     TEXT NOT NULL,
  ccy      TEXT NOT NULL,
  kind     TEXT NOT NULL CHECK (kind IN ('bond', 'swap')),
  ord      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, curve_id)
);

-- ---------------------------------------------------------------------------
-- Рынок на прогон
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mkt_curve_point (
  run_id   INTEGER NOT NULL,
  curve_id TEXT NOT NULL,
  bucket   TEXT NOT NULL REFERENCES ref_bucket (bucket),
  rate     REAL NOT NULL,                   -- %, уровень на срез
  move_bp  REAL NOT NULL DEFAULT 0,         -- изменение за день, б.п. (то, что использовано в PnL explain)
  PRIMARY KEY (run_id, curve_id, bucket),
  FOREIGN KEY (run_id, curve_id) REFERENCES run_curve (run_id, curve_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS mkt_fx (
  run_id   INTEGER NOT NULL REFERENCES calc_run (run_id) ON DELETE CASCADE,
  ccy      TEXT NOT NULL,                   -- 'USD'; для 'RUB' rate = 1
  rate     REAL NOT NULL,                   -- RUB за 1 единицу валюты
  move_pct REAL NOT NULL DEFAULT 0,         -- изменение курса за день, %
  PRIMARY KEY (run_id, ccy)
);

CREATE TABLE IF NOT EXISTS mkt_index (
  run_id     INTEGER NOT NULL REFERENCES calc_run (run_id) ON DELETE CASCADE,
  index_name TEXT NOT NULL,                 -- 'КС','RUONIA','SOFR','RUSFAR CNY','€STR'
  value      REAL NOT NULL,                 -- %
  PRIMARY KEY (run_id, index_name)
);

-- ---------------------------------------------------------------------------
-- Позиции и чувствительности
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS position (
  run_id       INTEGER NOT NULL,
  position_id  TEXT NOT NULL,               -- стабильный id сделки/позиции между днями
  book_id      TEXT NOT NULL,
  instr_type   TEXT NOT NULL CHECK (instr_type IN ('BOND', 'IRS', 'XCCY', 'CDS', 'FX')),
  asset_class  TEXT NOT NULL,               -- 'ОФЗ-ПД','Корп RUB фикс','IRS',...
  segment      TEXT NOT NULL CHECK (segment IN ('Гос', 'Корп', 'Секьюритизация', 'Деривативы', 'FX')),
  name         TEXT NOT NULL,
  isin         TEXT,
  issuer       TEXT,                        -- ключ run_issuer (BOND, CDS)
  ccy          TEXT NOT NULL,
  maturity     TEXT,
  tenor_y      REAL,                        -- лет до погашения; NULL -> считается из maturity
  coupon_type  TEXT CHECK (coupon_type IN ('fix', 'float')),
  coupon       REAL,                        -- %, фикс. купон / фикс. нога / спред CDS в %
  index_name   TEXT,                        -- плавающий индекс
  margin       REAL,                        -- %, маржа к индексу (флоатеры)
  side         TEXT,                        -- 'PAY'/'REC' (IRS), 'PAY'/'RECFX' (XCCY), 'BUY'/'SELL' (CDS, FX)
  notional     REAL NOT NULL,               -- в валюте ccy, со знаком для FX
  notional_rub REAL,                        -- XCCY: рублёвая нога
  price        REAL,                        -- % от номинала (облигации) / курс сделки (FX)
  mv           REAL NOT NULL,               -- PV в валюте ccy
  mv_rub       REAL NOT NULL,               -- PV в RUB
  ytm          REAL,                        -- %
  spread_bp    REAL,                        -- б.п.: Z/G-спред облигации, рыночный спред CDS
  mdur         REAL,                        -- модиф. дюрация, лет
  spr_dur      REAL,                        -- спред-дюрация, лет
  convexity    REAL,                        -- выпуклость к доходности, лет^2 (если нет position_gamma — из неё строится 2-й порядок)
  dv01         REAL NOT NULL,               -- RUB за +1 б.п. по всем кривым (= сумма position_risk)
  cs01         REAL NOT NULL DEFAULT 0,     -- RUB за +1 б.п. кредитного спреда
  jtd          REAL NOT NULL DEFAULT 0,     -- jump-to-default, RUB (потеря при дефолте, со знаком)
  wal          REAL,                        -- секьюритизация: WAL, лет
  cpr          REAL,                        -- секьюритизация: CPR, %
  counterparty TEXT,
  new_today    INTEGER NOT NULL DEFAULT 0 CHECK (new_today IN (0, 1)),
  extra_json   TEXT,                        -- любые доп. атрибуты (UI их игнорирует)
  PRIMARY KEY (run_id, position_id),
  FOREIGN KEY (run_id, book_id) REFERENCES run_book (run_id, book_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_position_book ON position (run_id, book_id);

CREATE TABLE IF NOT EXISTS position_risk (
  run_id      INTEGER NOT NULL,
  position_id TEXT NOT NULL,
  curve_id    TEXT NOT NULL,
  bucket      TEXT NOT NULL REFERENCES ref_bucket (bucket),
  dv01        REAL NOT NULL,                -- key-rate DV01, RUB за +1 б.п. в этом пилларе
  PRIMARY KEY (run_id, position_id, curve_id, bucket),
  FOREIGN KEY (run_id, position_id) REFERENCES position (run_id, position_id) ON DELETE CASCADE,
  FOREIGN KEY (run_id, curve_id) REFERENCES run_curve (run_id, curve_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS position_fx (
  run_id      INTEGER NOT NULL,
  position_id TEXT NOT NULL,
  ccy         TEXT NOT NULL,
  delta       REAL NOT NULL,                -- FX-дельта в единицах валюты, + = длинная валюта
  PRIMARY KEY (run_id, position_id, ccy),
  FOREIGN KEY (run_id, position_id) REFERENCES position (run_id, position_id) ON DELETE CASCADE
);

-- Вторые производные (гамма, выпуклость, кросс-гамма). Разреженная симметричная матрица по факторам,
-- хранится верхний треугольник: factor_1 <= factor_2 (сравнение строк). Факторы и единицы:
--   'IR|<curve_id>|<bucket>' — key-rate сдвиг пиллара кривой, 1 б.п.;
--   'CS'                    — собственный кредитный спред позиции, 1 б.п.;
--   'FX|<ccy>'              — изменение курса валюты к рублю, 1 %.
-- gamma = d2 PV_RUB / (d a d b) в RUB на (единица a × единица b).
-- Вклад в PnL сценария: 0.5 * gamma * da^2 для диагонали, gamma * da * db вне диагонали.
CREATE TABLE IF NOT EXISTS position_gamma (
  run_id      INTEGER NOT NULL,
  position_id TEXT NOT NULL,
  factor_1    TEXT NOT NULL,
  factor_2    TEXT NOT NULL,
  gamma       REAL NOT NULL,
  PRIMARY KEY (run_id, position_id, factor_1, factor_2),
  CHECK (factor_1 <= factor_2),
  FOREIGN KEY (run_id, position_id) REFERENCES position (run_id, position_id) ON DELETE CASCADE
);

-- PnL за день и его объяснение. explained = сумма компонент; unexplained = official - explained.
CREATE TABLE IF NOT EXISTS position_pnl (
  run_id      INTEGER NOT NULL,
  position_id TEXT NOT NULL,
  carry       REAL NOT NULL DEFAULT 0,
  rolldown    REAL NOT NULL DEFAULT 0,
  rates       REAL NOT NULL DEFAULT 0,
  spread      REAL NOT NULL DEFAULT 0,
  fx_gross    REAL NOT NULL DEFAULT 0,      -- FX-переоценка самой позиции
  fx_funding  REAL NOT NULL DEFAULT 0,      -- доля FX-переоценки валютного фондирования книги
  convexity   REAL NOT NULL DEFAULT 0,
  new_trades  REAL NOT NULL DEFAULT 0,
  other       REAL NOT NULL DEFAULT 0,
  official    REAL,                         -- официальный PnL (бэк-офис); NULL = ещё нет
  break_note  TEXT,                         -- комментарий к расхождению
  PRIMARY KEY (run_id, position_id),
  FOREIGN KEY (run_id, position_id) REFERENCES position (run_id, position_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Фондирование
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS funding_deal (
  run_id       INTEGER NOT NULL,
  deal_id      TEXT NOT NULL,
  book_id      TEXT NOT NULL,
  type         TEXT NOT NULL,               -- 'РЕПО с ЦК','TSY FTP','Обратное РЕПО','FX-своп','Гарантийное обеспечение'
  ccy          TEXT NOT NULL,
  amount       REAL NOT NULL,               -- в валюте; < 0 привлечение, > 0 размещение
  rate_expr    TEXT,                        -- для показа: 'RUONIA + 0.20%'
  rate         REAL NOT NULL,               -- %, текущая эффективная ставка
  start_date   TEXT,
  maturity     TEXT NOT NULL,
  counterparty TEXT,
  collateral   TEXT,
  PRIMARY KEY (run_id, deal_id),
  FOREIGN KEY (run_id, book_id) REFERENCES run_book (run_id, book_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS funding_pnl (
  run_id       INTEGER NOT NULL,
  deal_id      TEXT NOT NULL,
  carry        REAL NOT NULL DEFAULT 0,     -- стоимость фондирования за день (привлечение < 0)
  fx_gross     REAL NOT NULL DEFAULT 0,     -- FX-переоценка сделки
  fx_allocated REAL NOT NULL DEFAULT 0,     -- часть fx_gross, разнесённая в position_pnl.fx_funding
  PRIMARY KEY (run_id, deal_id),
  FOREIGN KEY (run_id, deal_id) REFERENCES funding_deal (run_id, deal_id) ON DELETE CASCADE
);

-- ---------------------------------------------------------------------------
-- Сценарии: полная переоценка расчётным сервисом (необязательно)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scenario_result (
  run_id      INTEGER NOT NULL,
  scenario_id TEXT NOT NULL REFERENCES scenario_def (scenario_id),
  position_id TEXT NOT NULL,
  rates       REAL NOT NULL DEFAULT 0,
  spread      REAL NOT NULL DEFAULT 0,
  fx_gross    REAL NOT NULL DEFAULT 0,
  fx_funding  REAL NOT NULL DEFAULT 0,
  convexity   REAL NOT NULL DEFAULT 0,      -- второй порядок по ставкам и спредам (выпуклость, гамма) и прочая нелинейность
  fx_cross    REAL NOT NULL DEFAULT 0,      -- кросс-эффект FX × ставки/спреды (переоценка валютной чувствительности)
  total       REAL NOT NULL,
  PRIMARY KEY (run_id, scenario_id, position_id),
  FOREIGN KEY (run_id, position_id) REFERENCES position (run_id, position_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scenario_funding_result (
  run_id      INTEGER NOT NULL REFERENCES calc_run (run_id) ON DELETE CASCADE,
  scenario_id TEXT NOT NULL REFERENCES scenario_def (scenario_id),
  book_id     TEXT NOT NULL,
  fx_residual REAL NOT NULL,                -- FX фондирования книги, не разнесённый на позиции
  PRIMARY KEY (run_id, scenario_id, book_id)
);

-- ---------------------------------------------------------------------------
-- История итогов по деску (для графиков за 22 дня)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS desk_history (
  as_of         TEXT PRIMARY KEY,
  run_id        INTEGER REFERENCES calc_run (run_id) ON DELETE SET NULL,  -- NULL = загружено как история
  pnl_official  REAL NOT NULL,              -- позиции + фондирование (carry + FX-остаток)
  pnl_explained REAL NOT NULL,
  dv01          REAL NOT NULL,
  cs01          REAL NOT NULL,
  updated_at    TEXT NOT NULL
);

-- ===========================================================================
-- Схема v2: слой ВХОДНЫХ данных центрального сервиса (по датам, не по прогонам).
-- Наборы и поля — tdm/datasets.py, список к сбору — docs/DATA_REQUIREMENTS.md.
-- ===========================================================================

-- Справочники (актуальная версия; история важных атрибутов — отдельными рядами)
CREATE TABLE IF NOT EXISTS ref_book (
  book_id TEXT PRIMARY KEY, name TEXT NOT NULL, desk TEXT NOT NULL, trader TEXT,
  active INTEGER NOT NULL DEFAULT 1, ord INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_issuer (
  issuer_id TEXT PRIMARY KEY, name TEXT NOT NULL, issuer_group TEXT NOT NULL, sector TEXT, country TEXT,
  is_gov INTEGER NOT NULL DEFAULT 0, is_quasi INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_issuer_rating (
  issuer_id TEXT NOT NULL, agency TEXT NOT NULL, date TEXT NOT NULL, rating TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (issuer_id, agency, date)
);
CREATE TABLE IF NOT EXISTS ref_counterparty (
  counterparty_id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT, issuer_id TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_instrument (
  instrument_id TEXT PRIMARY KEY, isin TEXT, name TEXT NOT NULL, issuer_id TEXT NOT NULL, asset_class TEXT NOT NULL,
  segment TEXT NOT NULL, ccy TEXT NOT NULL, face_value REAL NOT NULL, issue_date TEXT, maturity TEXT NOT NULL,
  coupon_type TEXT NOT NULL, coupon_rate REAL, coupon_freq INTEGER, day_count TEXT, index_name TEXT, margin REAL,
  amortizing INTEGER, offer_date TEXT, is_mbs INTEGER, collateral_eligible INTEGER, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_cashflow (
  instrument_id TEXT NOT NULL, date TEXT NOT NULL, type TEXT NOT NULL, rate REAL, amount REAL, updated_at TEXT NOT NULL,
  PRIMARY KEY (instrument_id, date, type)
);
CREATE TABLE IF NOT EXISTS ref_otc_deal (
  deal_id TEXT PRIMARY KEY, instr_type TEXT NOT NULL CHECK (instr_type IN ('IRS', 'XCCY', 'CDS', 'FX')),
  book_id TEXT NOT NULL, trade_date TEXT NOT NULL, start_date TEXT, maturity TEXT NOT NULL, ccy TEXT NOT NULL,
  notional REAL NOT NULL, side TEXT NOT NULL, fixed_rate REAL, float_index TEXT, float_spread_bp REAL, notional_rub REAL,
  reference_issuer_id TEXT, cds_spread_bp REAL, fx_rate REAL, counterparty_id TEXT, cleared INTEGER, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_curve (
  curve_id TEXT PRIMARY KEY, name TEXT NOT NULL, ccy TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('bond', 'swap')),
  index_name TEXT, source TEXT, ord INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_calendar (
  date TEXT PRIMARY KEY, is_business_day INTEGER NOT NULL, updated_at TEXT NOT NULL
);

-- Ежедневные срезы
CREATE TABLE IF NOT EXISTS pos_snapshot (
  as_of TEXT NOT NULL, book_id TEXT NOT NULL, position_id TEXT NOT NULL, instrument_id TEXT, deal_id TEXT,
  quantity REAL, face_amount REAL, avg_price REAL, book_value REAL, mark_price REAL, accrued REAL, mv_rub REAL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (as_of, position_id),
  CHECK (instrument_id IS NOT NULL OR deal_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS ix_pos_snapshot_book ON pos_snapshot (as_of, book_id);
CREATE TABLE IF NOT EXISTS fund_snapshot (
  as_of TEXT NOT NULL, deal_id TEXT NOT NULL, book_id TEXT NOT NULL, type TEXT NOT NULL, ccy TEXT NOT NULL,
  amount REAL NOT NULL, rate_type TEXT, index_name TEXT, spread_bp REAL, rate REAL NOT NULL, start_date TEXT,
  maturity TEXT NOT NULL, counterparty_id TEXT, collateral TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (as_of, deal_id)
);
CREATE TABLE IF NOT EXISTS trade_log (
  trade_date TEXT NOT NULL, trade_id TEXT NOT NULL, book_id TEXT NOT NULL, position_id TEXT NOT NULL, instrument_id TEXT,
  deal_id TEXT, side TEXT NOT NULL, quantity REAL, face_amount REAL, price REAL, counterparty_id TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (trade_date, trade_id)
);
CREATE TABLE IF NOT EXISTS pnl_official (
  as_of TEXT NOT NULL, book_id TEXT NOT NULL, position_id TEXT NOT NULL DEFAULT '',  -- '' = итог по книге
  pnl_rub REAL NOT NULL, comment TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (as_of, book_id, position_id)
);

-- Рынок (временные ряды)
CREATE TABLE IF NOT EXISTS md_curve_point (
  date TEXT NOT NULL, curve_id TEXT NOT NULL, tenor_years REAL NOT NULL, rate REAL NOT NULL, source TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (date, curve_id, tenor_years)
);
CREATE TABLE IF NOT EXISTS md_fx (
  date TEXT NOT NULL, ccy TEXT NOT NULL, rate REAL NOT NULL, source TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (date, ccy)
);
CREATE TABLE IF NOT EXISTS md_fixing (
  date TEXT NOT NULL, index_name TEXT NOT NULL, value REAL NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (date, index_name)
);
CREATE TABLE IF NOT EXISTS md_bond_quote (
  date TEXT NOT NULL, instrument_id TEXT NOT NULL, clean_price REAL NOT NULL, ytm REAL, z_spread_bp REAL, g_spread_bp REAL,
  accrued REAL, volume_rub REAL, source TEXT, updated_at TEXT NOT NULL,
  PRIMARY KEY (date, instrument_id)
);
CREATE INDEX IF NOT EXISTS ix_md_bond_quote_instr ON md_bond_quote (instrument_id, date);
CREATE TABLE IF NOT EXISTS md_cds_quote (
  date TEXT NOT NULL, issuer_id TEXT NOT NULL, tenor_years REAL NOT NULL, spread_bp REAL NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (date, issuer_id, tenor_years)
);

-- Журнал загрузок
CREATE TABLE IF NOT EXISTS data_load (
  load_id INTEGER PRIMARY KEY AUTOINCREMENT, dataset TEXT NOT NULL, file_name TEXT, loaded_at TEXT NOT NULL,
  rows INTEGER NOT NULL DEFAULT 0, date_from TEXT, date_to TEXT, status TEXT NOT NULL CHECK (status IN ('ok', 'error')), error TEXT
);

-- Сценарии по запросу (уровень 3; сервер пока только хранит очередь, исполняет расчётный сервис)
CREATE TABLE IF NOT EXISTS scenario_job (
  job_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, requested_by TEXT,
  base_run_id INTEGER REFERENCES calc_run (run_id), definition_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
  started_at TEXT, finished_at TEXT, error TEXT, result_scenario_id TEXT
);
