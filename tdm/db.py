"""Подключение к SQLite и создание схемы."""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = "2"
DEFAULT_DB = os.environ.get("TDM_DB", str(Path.cwd() / "data" / "tdm.sqlite"))

BUCKETS = [("1M", 1 / 12), ("3M", 0.25), ("6M", 0.5), ("1Y", 1.0), ("2Y", 2.0), ("3Y", 3.0),
           ("5Y", 5.0), ("7Y", 7.0), ("10Y", 10.0), ("15Y", 15.0), ("20Y+", 20.0)]
RATINGS = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B", "B-", "CCC", "D"]


def now_iso():
    return datetime.now().replace(microsecond=0).isoformat()


def connect(path=None, readonly=False):
    """Открыть базу. WAL: один писатель и сколько угодно читателей одновременно."""
    path = str(path or DEFAULT_DB)
    if readonly:
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30, check_same_thread=False)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db(path=None):
    """Создать таблицы и глобальные справочники (идемпотентно)."""
    con = connect(path)
    con.executescript((Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8"))
    with con:
        _migrate(con)
        con.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SCHEMA_VERSION,))
        con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (now_iso(),))
        if not con.execute("SELECT 1 FROM ref_bucket LIMIT 1").fetchone():
            con.executemany("INSERT INTO ref_bucket(bucket, ord, pillar_years) VALUES (?,?,?)",
                            [(b, i, y) for i, (b, y) in enumerate(BUCKETS)])
        if not con.execute("SELECT 1 FROM ref_rating LIMIT 1").fetchone():
            con.executemany("INSERT INTO ref_rating(rating, ord) VALUES (?,?)", [(r, i) for i, r in enumerate(RATINGS)])
    return con


def _columns(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _migrate(con):
    """Добавить колонки, появившиеся после v1 (таблицы создаёт schema.sql)."""
    if "name" not in _columns(con, "run_issuer"):
        con.execute("ALTER TABLE run_issuer ADD COLUMN name TEXT")          # отображаемое имя, если issuer — код
    cols = _columns(con, "scenario_def")
    if "kind" not in cols:
        con.execute("ALTER TABLE scenario_def ADD COLUMN kind TEXT NOT NULL DEFAULT 'preset'")  # preset | historical
    if "description" not in cols:
        con.execute("ALTER TABLE scenario_def ADD COLUMN description TEXT")
