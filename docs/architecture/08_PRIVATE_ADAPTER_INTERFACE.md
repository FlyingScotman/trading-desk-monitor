# 08 Private adapter interface (SPI v1)

As of 19 October 2026, revision 2 (after an independent review). Status: **accepted; both tracks build against it.** Decision: [ADR-008](adr/ADR-008-private-adapter-boundary.md). Owner of this document: the backend. A change here is a versioned change (§9).

The desk's own data (positions, OCP and cash, official PnL, trades, internal market data) arrives as bank reports. The backend reads them through a **private adapter**: a Python package that lives outside this repository and is plugged in by a path in the settings. This document is the whole contract between the two. Neither side needs to read the other's code.

## 1 Two tracks, four couplings

The backend (slices S7+) and the adapter (phases A0–A6) are developed in parallel and mostly independently. They are coupled only through:

| Coupling | Lives in | Used by the backend for | Used by the adapter for |
|---|---|---|---|
| **This SPI** (§2–§9) | this file | loading and calling any adapter | what to implement |
| **Private input contract v1** (§10) | `contract/private/v1/*.schema.json` | validating and storing rows | what rows to emit |
| **Reference adapter and canonical fixtures** | `desk.inbox.reference`, `fixtures/private/v1/` | building S7+ with no private code | an example of a correct adapter |
| **Conformance kit** (§8) | `desk adapter check`, `python -m desk.inbox.conformance` | — | its tests, and the owner's dry run at work |

The backend never waits for the adapter: it develops on the reference adapter, which reads canonical CSV files. The adapter never waits for the backend: it develops against the schemas and the conformance kit. They meet at integration points I1–I6 (§11).

## 2 Plugging the adapter in

Setting `DESK_PRIVATE_ADAPTER` (environment, the settings page or `backend/.env`, like every `DESK_*` setting):

| Value | Meaning |
|---|---|
| empty or unset | the reference adapter `desk.inbox.reference` (canonical CSV) |
| a path to a package directory, e.g. `C:\desk\trades-data-adapter\src\tda` | the directory is imported as a package named after it: `importlib.util.spec_from_file_location(<name>, <dir>/__init__.py, submodule_search_locations=[<dir>])`, registered in `sys.modules` so the package's own imports work. `sys.path` is not changed. Loading fails if `<name>` is already in `sys.modules` or importable from `sys.path` (no shadowing either way) |
| a dotted module name, e.g. `tda` | `importlib.import_module`, for an adapter installed into the backend's environment |

`DESK_PRIVATE_ADAPTER_CONFIG` (optional): a folder on the work machine with the adapter's local configuration (mapping overrides, file-name patterns). It is passed to the adapter as `ctx.config_dir`; default `<home>/adapter-config` (the per-user data folder, created on demand). The backend reads it to fingerprint it, and the settings page edits the files the adapter declares in `config_files` (§4); the backend never interprets them.

**Where it runs.** The backend calls the adapter in a **child process**: `<python> -I -m desk.inbox.runner`, with the environment stripped to `DESK_PRIVATE_ADAPTER*`, `PATH`, `SYSTEMROOT` and `TEMP`. Requests and results cross as JSON on stdin and stdout. So:
- a hung or crashing adapter cannot stop the watcher or the API: `DESK_ADAPTER_MATCH_TIMEOUT_S` (default 10, for one batch of `match` calls) and `DESK_ADAPTER_READ_TIMEOUT_S` (default 600, per file); a file over `DESK_INBOX_MAX_FILE_MB` (default 500) is not offered;
- the adapter never sees the store URL or the hub token;
- "plain data only" (§3) is enforced by the JSON encoding.

**Fingerprint.** At load the backend computes the SHA-256 over the adapter package's files and the contents of `DESK_PRIVATE_ADAPTER_CONFIG`. The fingerprint is shown by `desk inbox status` and recorded on every ledger row and revision. `DESK_PRIVATE_ADAPTER_SHA256`, if set, must match, or loading fails.

**When it loads.** Once per process, by every command that ingests (`desk inbox …`, `desk adapter check`) and by `desk serve` when the watcher is on.

**When loading fails** (import error, name collision, missing function, unsupported SPI or contract version, fingerprint mismatch), ingestion is off and says why: `desk inbox status` and `/health/ready` show `adapter: error <reason>`. Everything else (existing runs, the API, scenarios) keeps working. There is no silent fallback to the reference adapter.

**Constraints on the adapter.** Python 3.11, standard library only. It must not import `desk` at run time (its tests may, for the conformance kit). No network access, no file writes, no state between calls. Within one inbox scan the backend reuses a few long-lived child processes for many calls (since 20.10.2026: the import and the instruments index are paid once, not per file), so module-level state survives between calls of a scan: it may hold caches keyed by what they cache (a file's path, size and modification time), never anything that changes the result of a call. Since 21.10.2026 (R22) every worker imports the adapter and loads the index before the first `match`, under the read timeout, so a slow import does not count against the match timeout; `match` calls of different workers run at the same time, and the files of a call that timed out or crashed are offered to `match` once more in smaller calls.

## 3 The module interface

The package's top level exposes:

```python
SPI_VERSION = 1                                     # int; must equal describe()["spi"]

def describe() -> dict: ...                          # §4, called once at load
def match(path: str, ctx) -> dict | None: ...        # §5, called for every new stable file
def read(path: str, hit: dict, ctx) -> dict: ...     # §6, called for every matched file
def reference(ctx) -> dict: ...                      # §7; required when any source emits book_id
```

**Values.** All values crossing the boundary are JSON types: `dict`, `list`, `str`, `int`, `float`, `bool`, `None`.
- Dates are `YYYY-MM-DD`. Timestamps are ISO 8601; one without an offset is Europe/Moscow.
- Amounts are a finite JSON number or a string matching `^-?[0-9]+(\.[0-9]+)?$`. The backend stores them as `numeric` and sums in `Decimal`. NaN, infinity, thousands separators and locale commas are `bad_number` errors in the adapter, never passed on.
- **Absent, null and empty.** An optional field may be absent or `null`; the two mean the same. The empty string `""` is invalid in every field: the adapter sends `null`.
- **Reserved keys** in rows: `_src` (required, str: where the row came from), `_dataset` (required for multi-dataset sources, §4), `_report_date` (rows of multi-date files, §6.1). No other key may start with `_`.

## 4 `describe()`

```python
{
  "name": "tda",
  "version": "0.3.0",                  # the adapter's own release
  "spi": 1,
  "contract": "private/1.0",           # §9
  "sources": [
    {"source": "positions_daily",     "dataset": "positions",      "time_basis": "start_of_day", "completeness": "full_snapshot", "scope": ["book_id"], "covers": "all_active_books"},
    {"source": "ocp_cash",            "datasets": ["ocp", "cash_balances"], "time_basis": "start_of_day", "completeness": "full_snapshot",
                                      "scope": {"ocp": ["entity"], "cash_balances": ["entity", "book_id"]}, "covers": "listed"},
    {"source": "pnl_tradelevel",      "dataset": "pnl_official",   "time_basis": "end_of_day",   "completeness": "full_snapshot", "scope": ["entity", "book_id"], "covers": "listed"},
    {"source": "internal_market_eod", "dataset": "internal_market","time_basis": "end_of_day",   "completeness": "full_snapshot", "scope": ["kind"], "covers": "listed"},
    {"source": "trades_hourly",       "dataset": "trades",         "time_basis": "intraday",     "completeness": "full_snapshot", "scope": ["book_id"], "covers": "listed"}
  ]
}
```

- `source`: the adapter's stable id of one report family.
- `dataset`: a dataset of §10. A report carrying two datasets declares `datasets` instead, and every row carries `_dataset`; `scope` is then a dict per dataset.
- `time_basis`: how the report date maps to the business date (§6.5).
- `completeness`:
  - `full_snapshot`: for each scope unit the file lists in `scopes` (§6.2), the file holds every row of that unit; a row missing from a later file for the same unit means closed or zero;
  - `increment`: rows add to what is there; duplicates are found by the dataset key (§10).

  Completeness and scope are set **per source**; the headings in §10 give the usual choice.
- `scope`: canonical field names forming the replacement unit, for example `["book_id"]` or `["entity", "book_id"]`.
- `covers` (for `full_snapshot`):
  - `all_active_books`: the report always lists every active book. Every `active` book of `reference()["books"]` missing from `scopes` is treated as listed and empty, and gets a `book_closed_by_omission` warning;
  - `listed`: only the units in `scopes` are touched.
- `priority` (int, optional): see §6.6 on two sources for one unit.
- `config_files` (list, optional): the files the adapter reads from `ctx.config_dir`, for the settings page
  (`/settings.html`), which lists them and edits each one as JSON, pre-filled with `template` while the file
  is missing:

  ```python
  "config_files": [{"name": "books.json", "title": "Books", "help": "...", "required": True, "template": {}}]
  ```

  `name` is a plain file name (no folder). The backend tolerates the key's absence; an entry of a bad shape
  is ignored with a warning, never a load error. The files are part of the fingerprint (§2) like the rest of
  the config folder.

## 5 `match(path, ctx)`

Recognises a file. Returns `None` for a file that is not the adapter's, or for a source that has no reader yet, otherwise:

```python
{"source": "positions_daily", "report_date": "2026-10-20", "date_from": "name", "report_ts": None, "seq": None}
```

- `report_date` (required): for `start_of_day` it is **the calendar date of the cut** (positions at 00:00 of 20.10 → `2026-10-20`), even if the report's header prints the close date. For `end_of_day` it is the date the data is for. For `intraday` it is the date of the cut.
- `date_from`: `name` or `header`, where the date was read.
- `report_ts`: required for `intraday`, the cut time.
- `seq`: optional ordering of several files of one source and date.

Rules:
- Cheap and deterministic: the name, the size, at most the first sheet's header rows. No full parse.
- Never raises on a foreign or odd file; it returns `None`. An exception counts as an adapter bug: the file gets status `match_failed`, and the scan goes on.
- A file that nothing matches gets status `ignored` and stays visible in `desk inbox status`, so a renamed report is noticed.
- An ambiguous date (two candidate dates in the name, as pnl_tool's `filename_date_missing_or_ambiguous`) means `None`. The adapter never guesses.

## 6 `read(path, hit, ctx)`

Parses one matched file. `hit` is what `match` returned. Returns:

```python
{
  "rows": [ {...canonical row..., "_src": "R12"}, ... ],
  "scopes": [{"book_id": "B1"}, {"book_id": "B2"}],
  "issues": [ {"severity": "warning", "code": "unmapped_instrument", "src": "R40", "scope": {"book_id": "B2"}, "message": "security id not in mapping", "count": 1} ],
  "controls": {"positions": {"row_count": 1840, "sums": {"face_amount_ccy": "123400000"}}},
  "meta": {"generated_at": "2026-10-20T06:42:00+03:00"}
}
```

### 6.1 Rows
- Each row is one record of the source's dataset (§10).
- `_src`: the row's location in the report: a row number (`R12`), or a line number for CSV. **No sheet names**: the profiler hides them, and so does everything that may leave work. It is kept with the stored row for drill-down from any screen back to the report line.
- Raw firm values the desk wants to see but calculations never use go in keys prefixed `x_`. The backend stores them as they are. Any other unknown key fails validation, except fields of a newer minor contract (§9).
- A multi-date file (allowed for `pnl_official` and `trades` only) puts `_report_date` on every row. Each row's date is mapped like `report_date`.
- **Instrument identity.** A row carries:
  - `source_instrument_id`: as in the report (required in `positions` and `trades`, otherwise when the report has it);
  - `source_instrument_name`: when the report has it;
  - any of `isin`, `regnum`, `moex_secid` that the report or the adapter's own mapping gives;
  - `instrument_id`: **only** a value returned by `ctx.instruments.lookup(...)["instrument_id"]`, never anything else.

  The backend re-resolves every row whose `instrument_id` is null from `isin`, `regnum` and `moex_secid`, against the static snapshot of the row's business date. This matters for the backfill: a bond that matured in March is not in today's static data. An unresolved instrument is a `warning`, never a dropped row: the backend keeps the position, values it down the ladder (better valued somehow than not at all) and lists it in coverage.

### 6.2 Scopes
`scopes` is **required for every `full_snapshot` source**. It lists the scope units the file covers, **including units with no rows**. Each item has exactly the source's scope keys, plus `_dataset` for multi-dataset sources and `_report_date` for multi-date files. Only these units are replaced. With `covers = all_active_books`, the active books that are missing are added as empty (§4).

A `full_snapshot` read without `scopes` is `rejected` (`missing_scopes`). An absent book is never a zero by accident, and never kept for ever by accident.

### 6.3 Issues
- `severity`:
  - `error`: rejects the scope unit named in the issue's `scope`, or the whole file when the issue has no `scope`;
  - `warning`: a row is kept but doubtful;
  - `info`: for the log only.
- `code` matches `^[a-z][a-z0-9_]{1,40}$`. Standard codes: `missing_required`, `bad_number`, `bad_date`, `unknown_book`, `unmapped_instrument`, `duplicate_key`, `control_mismatch`, `empty_report`, `unexpected_layout`, `missing_scopes`, `cached_formula_missing`. The adapter may add its own.
- `scope` (optional): the unit the issue concerns, shaped like a `scopes` item. `unknown_book` always carries it.
- `src`, `message`: a location (as `_src`) and a reason. **No cell values in messages or codes.** Issues are shown and logged, and the conformance summary must be shareable outside work.

### 6.4 Controls
`controls` holds **only figures the report itself states**: footer totals, a stated row count, a "total" line. Never sums the adapter computed from its own rows, which would make the check pass trivially.
- Keys are dataset names. Under each: `row_count` (if the report states one) and `sums` keyed by canonical field names.
- A report that states nothing gives `controls: {}`, and the kit reports `no_controls`.
- The backend re-sums the rows. A difference above max(0.01, 1e-8 × |total|) is a `control_mismatch` error for the file. This is the "no silent zeros" rule from pnl_tool.

### 6.5 Time semantics (done by the backend)

| `time_basis` | Business date (`valid_date`) | Example |
|---|---|---|
| `start_of_day` | the previous business day before `report_date` | positions at 00:00 of Mon 20.10 → close of Fri 17.10 |
| `end_of_day` | `report_date`, **even on a non-business day**: never shifted or dropped | official PnL for 17.10, delivered on 20.10 |
| `intraday` | `report_date`, with `report_ts` as the cut | trades up to 13:00 |

- **Calendar.** Business days follow [07](07_CORE_PRODUCT_COVERAGE.md) §0.1: holidays and transferred working Saturdays, and MOEX weekend sessions are not business days. The calendar is that of the newest cached static snapshot whose range covers `report_date`. If none covers it, the file waits with status `waiting_calendar` and is retried; the date is never guessed.
- Reports of Saturday 18.10 and Monday 20.10 both map to Friday 17.10. The later report wins (§6.6), and an `info` issue `same_valid_date` is written.
- **Plausibility.** For `start_of_day`, `meta.generated_at` should fall on `report_date`; for `end_of_day`, after `report_date`. Otherwise the file gets a `report_date_suspicious` warning. It catches the off-by-one of a header that prints the close date.
- `known_at` is the time the backend accepted the file. It serves "as known at T" questions only and never orders revisions.

### 6.6 Which revision is current
- **Per unit.** The replacement unit is (dataset, scope unit, `valid_date`), **across sources**.
- **Ordering.** Within a unit, the current revision is the one with the greatest (`report_date`, `report_ts`, `seq`, `meta.generated_at`, file modification time). So a backfill or replay that meets files in folder order still ends at the right revision. A file that sorts below the current one is stored as `superseded` on arrival.
- **Two sources.** A unit for a date comes from one source only. A second source emitting the same unit is `rejected` with `scope_conflict` for that unit, unless both sources declare `priority`, in which case the higher priority wins and the other revision is stored as superseded. `desk load-positions` (synthetic CSV) counts as a source with priority 0.
- **Change detection.** A new revision is stored only when the canonical fields differ. `_src` and `x_*` are stored but excluded from the change hash, so an inserted header line or a new `x_` field does not restate history.

### 6.7 Stocks and flows
- `positions`, `ocp` and `cash_balances` are **stocks**. A read for date D takes, per unit, the current revision with the latest `valid_date` ≤ D, and reports its age. A unit older than the previous business day is flagged `stale_input` on the day-status screen, and the run's lineage gives the `valid_date` per book.
- `pnl_official`, `trades` and `internal_market` are **flows or observations**. A read for D uses `valid_date = D` only and **never carries forward**. A missing day of official PnL is a gap, not yesterday's PnL again.

### 6.8 Failures and acceptance

| Outcome | File status |
|---|---|
| `read` raises or times out | `failed` / `timed_out`; no rows stored |
| `error` without `scope` | `rejected` |
| `error` with `scope` | that unit rejected and dropped from `scopes`; the rest accepted → `partially_accepted`. A rejected unit keeps its previous revision and is flagged `stale_input` |
| warnings only | `accepted` |

A partial `full_snapshot` without this rule would close positions that are merely unparsed.

**Exception texts.** The exception type and the traceback frames (file and line) go to the ledger. The exception *message* (which can hold a cell value) is kept only in the local log file. It never appears in the API, the ledger list or the conformance output.

## 7 The context object `ctx`

Read-only; the backend builds it. An adapter must work when every lookup returns `None` (the kit's `--stub-ctx` does exactly that).

| Member | What it gives |
|---|---|
| `ctx.spi` | `1` |
| `ctx.config_dir` | `DESK_PRIVATE_ADAPTER_CONFIG` as a string, or `None` |
| `ctx.inbox_dirs` | the report folders (`DESK_INBOX_DIRS` or the settings page) as a list of absolute path strings, same syntax (a recursive folder ends in `\**` or `/**`); `[]` when none. Optional member (§9): read it with `getattr(ctx, "inbox_dirs", [])`. For `reference()` that discovers books in the newest reports, say; the backend still offers files only through `match` |
| `ctx.instruments.lookup(kind, value, on=None)` | `kind` ∈ `isin`, `regnum`, `moex_secid`; `on` is a date (default: the latest). Returns `{"instrument_id", "isin", "regnum", "moex_secid", "name", "currency", "kind", "initial_face"}` from the cached static snapshot covering `on`, or `None` |
| `ctx.instruments.snapshot_id` | the static snapshot used, recorded in lineage |

Because the adapter runs in a child process, `ctx.instruments` is served from a read-only index file that the backend writes before the call. The adapter cannot tell the difference.

`reference(ctx)` returns reference rows the adapter owns: `{"books": [...]}` (§10), optionally with `"issues": [...]` shaped as in §6.3 (for example a malformed line of its mapping file); an `error` there fails the reference call. It is **required** when any source emits `book_id`. The backend calls it at load time and on `desk inbox scan`, and stores a new revision only when the content changed. Each book row may carry `valid_from`; without it, a change applies from its `known_at`, so a later change of `book_type` does not rewrite history. A `book_id` that is not in `books` gets a unit-scoped `unknown_book` error, raised by the backend.

## 8 Conformance kit

`desk adapter check [--adapter PATH] [--dir FOLDER ...] [--stub-ctx] [--json]`, also runnable as `python -m desk.inbox.conformance`. It runs the adapter through the same child-process runner, **without touching the store**:

1. Load: name collision, SPI version (`SPI_VERSION == describe()["spi"]`), `describe()` shape, datasets known, contract version supported, `reference()` present if needed.
2. For each file in the folders:
   - `match`: no exceptions, time spent;
   - `read`: row validation against the dataset schema, reserved keys, `scopes` shape, issue shape and code pattern, control re-sum;
   - determinism: a second `read` gives the same canonical rows;
   - dataset key uniqueness (§10);
   - the business date per §6.5, plausibility, ordering.
3. A summary per source and date:
   - files, rows, scope units;
   - issues by code and severity;
   - control status (`ok`, `mismatch`, `no_controls`);
   - the weekday histogram of `report_date` and `valid_date`;
   - unmapped instruments as a count and as a share of notional, resolved with the **real cached static data** unless `--stub-ctx` is given.

The summary holds counts and codes only. It has no values and no sheet names, and counts below 10 are shown as `<10`, as in the profiler. So the owner can run it on the real folders at work and share it the way the profiler's metadata is shared.

`desk.inbox.conformance`, `desk.inbox.runner` and the schema validator import **only the standard library and `desk.inbox`**. So the adapter's tests can import them from a sibling checkout (`../trading_desk_monitor/backend/src`) without psycopg, core or FastAPI. In the adapter's tests, the kit test is skipped only when that checkout is absent; an import error fails the test.

## 9 Versions

- **SPI** (`SPI_VERSION`, this document): an integer major. The backend lists the majors it supports (now `{1}`). Additions that old adapters can ignore (a new optional `ctx` member, a new optional function) do not bump it; anything else does, and the backend keeps the old major for one release.
- **Private input contract** (`describe()["contract"]`, `private/<major>.<minor>`): the dataset schemas of §10, versioned on their own, separate from the public results contract (1.3/1.4).
  - The backend accepts its own major with any minor. Fields from a newer minor are dropped with a `field_from_newer_contract` warning; the file is not rejected.
  - A new optional field is a minor version; a renamed, retyped or newly required field is a major one.
  - Minors so far: 1.0 (S7a); 1.1 adds the optional `positions` fields `maturity_date`, `risk_dv01_rub`, `risk_cs01_rub`. The backend supports 1.1 and accepts 1.0 adapters unchanged.
- The adapter's README states the lowest backend version it needs. `desk adapter check` prints the backend version, the SPI, the contract and the adapter fingerprint.

## 10 Private input contract v1 (`private/1.1`)

The schemas live in `contract/private/v1/`. The tables below are the design; the schema files are authoritative once written (S7a). They extend contract 1.3 ([DATA_REQUIREMENTS](../DATA_REQUIREMENTS.md) §4).

**Common rules.**
- Units are in field names, as in the hub's wire format: `_pct`, `_bp`, `_rub`, `_ccy` (in the row's `ccy`), `_rpt` (in the report's own unit, not confirmed). Prices are clean, in % of the current face. FX is RUB per unit.
- Signs: long and placement `+`, short and borrowing `−`, PnL profit `+`. In `trades` the quantity is unsigned and `side` gives the direction.
- A row has no date field: the file's business date applies (§6.5), or `_report_date` (§6.1).
- **Books.** `book_id` is the canonical book code, **unique across entities**. The adapter maps (entity, raw book code) to `book_id`. Books are described by `reference()["books"]`, and `entity` in rows is checked against them.
- **Position identity.** `position_id` is built from `book_id` and `source_instrument_id` (bonds) or `ccy` and `value_date` (FX), **never from `instrument_id`**. So a mapping that improves does not close and reopen a position.
- Every dataset may carry `x_*` fields and has `_src`. ● means required. "Reconciliation" means the backend compares the field with its own number and never values from it.

### `positions` — stock; usually `full_snapshot`, scope `["book_id"]`, `covers = all_active_books`

Key: (`book_id`, `position_id`).

| Field | Type | Meaning |
|---|---|---|
| `book_id` ● | str | |
| `position_id` ● | str | `book_id|source_instrument_id` for bonds, `book_id|ccy[|value_date]` for FX, the deal id for OTC |
| `asset_class` ● | enum | `bond`, `fx`, `otc`, `other` |
| `source_instrument_id` ● | str | As in the report |
| `source_instrument_name` | str | |
| `isin`, `regnum`, `moex_secid` | str | When the report or the adapter's mapping gives them |
| `instrument_id` | str | Only from `ctx.instruments.lookup` (§6.1) |
| `ccy` ● | str | Currency of the amounts in the row |
| `quantity` | num | Bonds: number of bonds, signed |
| `face_amount_ccy` | num | Bonds: number of bonds × **initial** face, signed |
| `face_outstanding_ccy` | num | Bonds: number of bonds × current (amortized) face, signed |
| `amount_ccy` | num | FX and cash-like rows: amount, signed; required for `fx` |
| `value_date` | date | FX forwards and other dated rows. An `fx` row with a `value_date` after the business date is a forward (NDF, FX future, unsettled leg): its PV is `mv_rub` if given, else 0, and `amount_ccy` counts only in the FX delta (open currency position). Without it, or with one not after the business date, the row is cash: PV = amount × the rate of the day |
| `deal_id` | str | OTC |
| `mark_price_pct`, `accrued_ccy`, `mv_rub`, `book_value_rub` | num | Reconciliation |
| `purchase_date`, `purchase_price_pct`, `purchase_yield_pct`, `contract_rate_pct` | | HTM, as in 1.3 |
| `maturity_date` | date | OTC and other rows: final maturity, for the bucket of `risk_dv01_rub` |
| `risk_dv01_rub`, `risk_cs01_rub` | num | The report's own risk of a row the backend does not value itself (OTC, other, an unmapped bond): RUB PV change for +1 bp parallel rates, resp. credit spread |

A bond row needs one of `quantity`, `face_amount_ccy`, `face_outstanding_ccy`. The backend derives the number of bonds with the instrument's initial or current face. When two are given and disagree by more than half a bond, the row gets a `quantity_face_mismatch` warning.

An `otc` or `other` row is valued at its `mv_rub` (quality `reported_value`; without `mv_rub` it is not valued: it shows in coverage and, without a PV, in the Desk Monitor positions list). Its `risk_dv01_rub` goes whole into the RUB rates bucket whose pillar is nearest `maturity_date` (none, or past: the shortest bucket, flagged), `risk_cs01_rub` into CS01. The same risk fields are used for an unmapped bond on the `reported_value` rung; a bond the backend values itself ignores them. The adapter fills them only when the report's figure is reliable.

### `ocp` — stock; `full_snapshot`, scope `["entity"]` or `["entity", "book_id"]`

The open currency position as the bank's system reports it. The backend derives OCP from positions too and compares the two **at the level the report has**: per entity, or per book (S9). Key: (`entity`, `book_id`, `ccy`).

| Field | Type | Meaning |
|---|---|---|
| `entity` ● | str | |
| `book_id` | str | When the report is per book |
| `ccy` ● | str | |
| `ocp_ccy` ● | num | Signed: `+` long the currency |
| `ocp_rub`, `rate_rub_per_unit` | num | As reported |

### `cash_balances` — stock; `full_snapshot`, scope `["entity", "book_id"]`

Key: (`entity`, `book_id`, `ccy`, `account_id`).

| Field | Type | Meaning |
|---|---|---|
| `entity` ●, `book_id` ● | str | |
| `ccy` ● | str | |
| `account_id` ● | str | The account or ledger line; a constant if the report has one line per currency |
| `balance_ccy` ● | num | `+` placed, `−` borrowed |
| `account_kind` | str | For example settlement, margin, nostro |

### `pnl_official` — flow; `full_snapshot`, scope `["entity", "book_id"]`, `time_basis = end_of_day`

One row per report line. Book totals are sums; the report's own totals go to `controls`. Key: (`entity`, `book_id`, `line_id`).

| Field | Type | Meaning |
|---|---|---|
| `entity` ●, `book_id` ● | str | |
| `line_id` ● | str | The report's own row key. A trade id is not enough: one trade can have several lines |
| `position_id` | str | Built by the same rule as in `positions`, when the line belongs to a position |
| `trade_id`, `trade_status` | str | Drill-down and new-trade detection |
| `trade_version` | int | |
| `trade_date`, `settle_date`, `maturity_date` | date | |
| `source_instrument_id`, `source_instrument_name`, `isin`, `regnum`, `moex_secid`, `instrument_id` | str | As in `positions` |
| `product_family`, `product_type`, `product_subtype` | str | The report's classification |
| `ccy` | str | The instrument's currency (a slice; the primary amounts are already RUB) |
| `long_short`, `buy_sell` | str | As reported |
| `pnl_daily_rub` ● | num | Official daily PnL (TradeLevel IFRS), RUB |
| `pnl_pl_daily_rub` ●, `pnl_oci_daily_rub` ● | num | Its P&L and OCI parts. The backend checks `pnl_daily_rub = pnl_pl_daily_rub + pnl_oci_daily_rub` per line and records a mismatch as a warning |
| `pnl_mtd_rub`, `pnl_ytd_rub` | num | As reported; never summed across dates |
| `accrual_rpt`, `price_change_rpt`, `fx_reval_rpt`, `premium_discount_rpt`, `new_trades_rpt`, `commission_rpt`, `brokerage_rpt`, `reserves_rpt`, `other_mtm_rpt`, `dividend_rpt` | num | Components in report units, not confirmed as RUB. Evidence for break analysis; the backend does not assume they add up to `pnl_daily_rub` |

A missing primary amount is an `error`. A missing component is `null`, never 0.

**Reconciliation keys** (S8, S11):
- bonds: (`valid_date`, `book_id`, `instrument_id`, falling back to `isin`, then `position_id`);
- FX: (`valid_date`, `book_id`, `ccy`);
- OTC: `trade_id` = `deal_id`.

The kit reports the share of PnL lines that join a position.

### `trades` — flow; `full_snapshot` per book for cumulative exports, or `increment` for deltas; `time_basis = intraday`

Key: `trade_id`. The current row is the one with the highest integer `trade_version`. A cumulative export replaces the book's trades of `valid_date`, ordered by `report_ts`. Rows with `trade_date` ≠ `report_date` (late bookings) are allowed and kept.

| Field | Type | Meaning |
|---|---|---|
| `trade_id` ● | str | |
| `trade_version` | int | |
| `status` ● | enum | `live`, `cancelled` |
| `trade_date` ● | date | |
| `trade_ts` | ts | |
| `book_id` ●, `position_id` ● | str | `position_id` by the `positions` rule |
| `asset_class` ● | enum | As in `positions` |
| `source_instrument_id` ●, `source_instrument_name`, `isin`, `regnum`, `moex_secid`, `instrument_id` | str | |
| `side` ● | enum | `buy`, `sell` |
| `quantity`, `face_amount_ccy`, `amount_ccy` | num | Unsigned |
| `price_pct`, `fx_rate` | num | Clean price, or the FX rate |
| `accrued_ccy`, `settle_date`, `counterparty_id`, `ccy` | | |

### `internal_market` — observation; `full_snapshot`, scope `["kind"]`, `time_basis = end_of_day`

The bank's own EOD inputs, as an overlay on the hub snapshot of the same date. Whatever it covers replaces the hub input; the rest stays from the hub. The run records both snapshot ids and the replaced keys.

| Field | Type | Meaning |
|---|---|---|
| `kind` ● | enum | `bond_price`, `curve_point`, `fx_rate`, `fixing` |
| `source_instrument_id`, `source_instrument_name`, `isin`, `regnum`, `moex_secid`, `instrument_id` | str | `bond_price` |
| `clean_price_pct`, `yield_pct`, `accrued_ccy` | num | `bond_price` |
| `curve_id` | str | `curve_point`: a desk curve name, mapped to a core curve spec by the backend |
| `tenor`, `pillar_date` | str, date | `curve_point`; one of the two |
| `rate_pct`, `rate_kind` | num, enum | `curve_point`; `rate_kind` ∈ `zero`, `par`, `quote` |
| `ccy`, `rate_rub_per_unit` | str, num | `fx_rate` (the internal ~19:00 mark) |
| `index_code`, `value_pct` | str, num | `fixing` |

Keys by kind:
- `bond_price`: `instrument_id`, falling back to `source_instrument_id`;
- `curve_point`: `curve_id` plus `tenor` or `pillar_date`;
- `fx_rate`: `ccy`;
- `fixing`: `index_code`.

### `books` — from `reference()`

As 1.3 `books`: `book_id` ●, `name` ●, `desk` ●, `book_type` ● (`TRADING`, `HTM`, `DERIVS`), `entity` ●, `ocp_to`, `trader`, `active` (default true), `valid_from`. `book_type` and `entity` are required here; 1.3 left them optional by mistake.

### Differences from 1.3, settled here

| 1.3 | Private contract v1 | Why |
|---|---|---|
| `as_of` in every row | none; the file's business date (§6.5) | The adapter does not do calendars |
| `quantity`, `face_amount`, `accrued` without units | `quantity`, `face_amount_ccy` (initial face), `face_outstanding_ccy`, `accrued_ccy` | `accrued` meant different units in different datasets; face was ambiguous for amortizing bonds |
| `trades.quantity` sign unspecified | unsigned; `side` gives the direction | |
| `pnl_official.position_id` empty = book total | lines only; totals in `controls` | A nullable key cannot be unique |
| `pnl_official` components `carry`, `funding`, `fx` | the report's own components (`_rpt`) | The backend computes carry; the report's components are evidence |
| revisions per (book, date) | per (dataset, scope unit, business date), across sources | |
| `books.book_type`, `entity` optional | required | |

The backend's current `desk load-positions` CSV (`position_id, book_id, secid, quantity_bonds`) stays for the synthetic books. The reference adapter reads canonical v1 CSV.

## 11 Integration points

| Point | Backend has | Adapter has | Check |
|---|---|---|---|
| **I1 Plug** | loader and runner (§2), `desk inbox`, ledger, reference adapter, conformance kit | A0: `describe`, `reference`, `match` returning `None` for sources without a reader | `desk adapter check` loads the private adapter from a path and lists its sources |
| **I2 Positions** | S7a: positions from the inbox into runs | A2 | dry run on the real folder at work, then live: the Desk Monitor on the real book |
| **I3 Official PnL** | S8 | A3 | YTD by book equals pnl_tool on the same files |
| **I4 OCP and cash** | S9 | A4 | reported OCP vs OCP derived from positions |
| **I5 Internal market data** | S10 | A5 | EOD-internal run and the diff screen |
| **I6 Trades** | S13 | A6 | intraday book = opening positions + trades |

Each point is done when the kit passes on the synthetic fixtures **and** the owner's dry run at work shows no `error` issues.

## 12 Who does what

| Concern | Backend (public) | Adapter (private) |
|---|---|---|
| Watching folders, file stability, temp files, size limit | yes | — |
| SHA-256, ledger, archive, duplicates, stale copies | yes | — |
| Running the adapter in a child process, timeouts, fingerprint | yes | — |
| Recognising a report family, its date | — | `match` |
| Parsing the layout, firm codes, units of the report | — | `read` |
| Firm codes → canonical codes (books, products), `books` reference | — | `read`, `reference`, its mapping files |
| Security ids → hub instrument | `ctx.instruments`; re-resolution on the business date | asks; gives `isin`/`regnum`/`moex_secid` from its own mapping |
| Schema validation, control re-sum, key uniqueness | yes | states the controls |
| Business date, calendar, `known_at`, revision order, supersession, stock vs flow reads | yes | — |
| Events, pipelines, reruns | yes | — |
| Showing issues and coverage | yes | writes issues without values |

## 13 Backend side: inbox behaviour

So both sides read the same rules:

- `DESK_INBOX_DIRS`: folders separated by `;` (Windows paths), each resolved like other path settings. A folder ending in `\**` (or `/**`) is scanned recursively.
- `DESK_INBOX_POLL_SECONDS` (default 60). A file is offered to `match` once its size and modification time are unchanged since the previous scan of the same process, or its modification time is older than `DESK_INBOX_POLL_SECONDS`. So a one-shot `desk inbox scan` takes files copied earlier. A known (path, size, modification time) is not hashed again. Names `~$*`, `*.tmp`, `*.part` and dot-files are skipped (Windows hidden attributes: S7b).
- **One scan at a time**: a scan holds a Postgres advisory try-lock (a second scan reports "busy" and does nothing), so `desk serve`'s watcher and a manual `desk inbox scan` never process a file twice. Ledger inserts are idempotent on the file hash.
- **Ledger.** One file row, keyed by SHA-256; an append-only status history; one row per (dataset, `valid_date`, scope unit) it touched. A unit that sorts below the current revision, or whose content equals it, stores no new position set. Statuses: `waiting_calendar`, `ignored`, `ignored_too_old` (report date behind the backend's inbox lookback; never read), `match_failed`, `accepted`, `partially_accepted`, `rejected`, `failed`, `timed_out`, `duplicate`, `stale_copy`, `superseded`.
  - The same content again with the same (source, `report_date`) is `duplicate`, and nothing is loaded.
  - The same content with a different (source, `report_date`) is `stale_copy`: yesterday's file re-sent under today's name. It is shown on day status, because today is then missing.
  - Duplicates and stale copies are recorded per sighting (path, size, time), not as a status of the content, so they never overwrite the original file's status. A match result is stored per attempt, because it depends on the adapter version.
- **Archive.** Every matched file, whatever its status, is copied to `DESK_INBOX_ARCHIVE` (default `.devdata/inbox`) as `<sha256>/<original name>`; the name is kept because adapters match by name. So `replay` and `retry` work after the bank cleans its folder. The archive stays on the work machine, like the store.
- **Re-offering.** When the adapter fingerprint changes (a new release or new mappings), files in `ignored`, `match_failed`, `failed`, `timed_out` and `rejected` are offered again on the next scan.
  - `desk inbox retry --status S` does it by hand.
  - `desk inbox replay --source S --from D1 --to D2` re-reads accepted files; a new revision is stored only where canonical rows changed (§6.6).
- **Events.** Every status change writes an event (`input.accepted`, `input.rejected`, `input.stale`, …) with the source, dataset, business date, file hash and counts. The orchestrator starts the dependent runs from these events (ROADMAP §1). The API serves them at `/api/v1/inbox` and `/api/v1/events`.
- **Logs** carry codes, counts and `_src` row numbers, never row values or exception messages (§6.8).
