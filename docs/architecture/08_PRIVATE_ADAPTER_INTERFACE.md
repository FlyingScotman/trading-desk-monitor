# 08 Private adapter interface (SPI v1)

As of 23 October 2026, revision 5 (private contract 1.4: `trade_terms` of interest rate and cross-currency swaps, REPO and HTM purchases, for the valuation of swaps by core and the funding of REPO and HTM lots; revision 4 of 22 October, contract 1.3: the `reference()` table `funding_policy` and
`positions.purchase_accrued_ccy`, for the funding engine in the runs, wave B1; revision 3, contract 1.2: FUTURES books, the `ocp`, `cash_balances`, `pnl_official` and `trade_terms` schemas, `reference()` entities and funding rules, `describe()` settings tabs, multi-date cash history, adapter phases A7–A10; revision 2 of 19 October followed an independent review). Status: **accepted; both tracks build against it.** Decision: [ADR-008](adr/ADR-008-private-adapter-boundary.md). Owner of this document: the backend. A change here is a versioned change (§9).

The desk's own data (positions, OCP and cash, official PnL, trade terms, trades, internal market data, and the firm's own rules: entities, book types, funding) arrives as bank reports. The backend reads them through a **private adapter**: a Python package that lives outside this repository and is plugged in by a path in the settings. This document is the whole contract between the two. Neither side needs to read the other's code.

## 1 Two tracks, four couplings

The backend (slices S7+) and the adapter (phases A0–A10) are developed in parallel and mostly independently. They are coupled only through:

| Coupling | Lives in | Used by the backend for | Used by the adapter for |
|---|---|---|---|
| **This SPI** (§2–§9) | this file | loading and calling any adapter | what to implement |
| **Private input contract v1** (§10) | `contract/private/v1/*.schema.json` | validating and storing rows | what rows to emit |
| **Reference adapter and canonical fixtures** | `desk.inbox.reference`, `fixtures/private/v1/` | building S7+ with no private code | an example of a correct adapter |
| **Conformance kit** (§8) | `desk adapter check`, `python -m desk.inbox.conformance` | — | its tests, and the owner's dry run at work |

The backend never waits for the adapter: it develops on the reference adapter, which reads canonical CSV files. The adapter never waits for the backend: it develops against the schemas and the conformance kit. They meet at integration points I1–I10 (§11).

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

### 2.1 Optional local acquisition (version 1)

The read-only interface above is unchanged. A compatible private **package** may additionally describe
`"acquisition": {"version": 1, "entry": "relative/helper.py", "source": "declared_source", "dataset": "trade_terms"}`.
The source must declare only `trade_terms`, `end_of_day`, `full_snapshot`, scope `["book_id"]` and `covers=listed`;
an acquisition capability cannot acquire authority over omitted books. The entry must resolve inside the
effective fingerprinted package; absolute paths, traversal, escaping symlinks
and missing helpers are rejected. This capability has its own version, independent of `private/1.4`. Older
adapters and the reference reader remain valid readers and show acquisition unavailable. It is not a new
network operation in `match`, `read` or `reference`.

Settings provides an explicit local Python executable (`acquisition_python`, `DESK_ACQUISITION_PYTHON`). There
is no guessed executable or module installation. Provider connection settings use its existing `config_files`
and `settings_tabs`; credentials are excluded. The private helper's required source files are nested resources
of the normal adapter package, so the existing fingerprint and private packaging cover them.

`GET /api/v1/acquisition` returns availability and the current in-memory job. `POST .../start` accepts one manual
`op=inspect|check|fetch`; `POST .../cancel` requests cancellation. Every route requires both the existing admin
policy and actual loopback peer/Host. Forwarded peer headers are refused. POST requires a same scheme/host/port
Origin and `application/x-www-form-urlencoded`; missing/null/foreign origins are refused, also with auth off.
The existing session/CSRF rules still apply. A network admin cannot submit these credentials.

HTTP and child stdin use standard UTF-8 form encoding, at most 65,536 bytes and 256 fields, each value at most
4,096 characters. The child receives one request until EOF, never credentials in argv or environment. No JSON
credential body or Pydantic input-error echo is used. `inspect` takes an explicit ISO `date`. It returns a bounded
list of reports `{id, sha256, name, books, policy}`; policy states the existing status filter, excluded products
and eligible counts/product labels per canonical book. A missing report, ambiguity or empty eligible scope
must be explicit. The user chooses one report and books, without a hidden date, source activation or all-books
default. `check` and `fetch` take transient `username`/`password`. `fetch` also takes the inspected `report`,
`report_sha256`, `adapter_fingerprint` and repeated `book`; changed input/config/package requires a new inspection.

The monitor adds trusted `adapter_root`, `config_dir`, repeated `inbox_dir`, and a unique managed `output_dir`
to child stdin. The helper runs as `[configured_python, -I, -B, contained_entry]`, in its own process group, with
only PATH, SYSTEMROOT, WINDIR, TEMP, TMP, USERPROFILE, LOCALAPPDATA, APPDATA and PROGRAMDATA inherited. Store/hub
credentials and Python import overrides are not inherited. The provider reuses its existing acquisition and
parser code, with no runtime dependency on `desk`.

The result channel is one JSON object, at most 262,144 bytes. Successful `check` returns `complete/connected`;
`inspect` returns `complete/scope_ready` and the requested date/reports. A completed `fetch` returns
`complete|partial/acquired`, exact date/books and `{file, sha256}` for the atomically published file inside its
managed folder. Failure returns a closed allowlisted `code`, never raw exception text, credentials or URLs.
Library stdout/stderr must not become diagnostics; stderr is discarded and malformed/overflow stdout fails.
The helper and exporter sanitize errors before writing any export log or bundle, too.

Only one operation is active. Timeout is 30 minutes; cancellation, timeout and shutdown stop the local process
tree. There is no invented remote logout or guarantee that an already sent remote request is undone. Credentials
are cleared from UI fields after handoff and released by the backend after writing stdin; secure memory
zeroization is not claimed. Status is in memory, not a persisted credential/job store.

Before fetch the requested date must have market data and positions for every selected book. The finished file
goes through the existing scanner/reference/contract/store path with an exact source/date/book guard before
unit writes. Source precedence, `covers=listed` and full-snapshot semantics remain unchanged. All units must pass
validation before any managed acquisition unit is written: a scoped validation rejection blocks the entire
acquisition file, while ordinary offline scans keep their existing partial-unit behavior. Acceptance requires
the latest file attempt and all selected units accepted under the captured fingerprint, current terms sets for
that exact date/source, then a new **full EOD for that date** using the existing cycle lock and fresh private
inputs hash/lineage. Other dates, history/backfill and automatic source activation are not run. Other books'
inputs are preserved; the full EOD values them as usual. Manual CLI EOD uses that same cycle lock.

Cancellation is accepted before ingestion; once ingestion/valuation starts the UI explicitly refuses cancellation.
Success requires the completed run to contain the current private inputs hash. Partial acquisition or derivative
rows using imported/missing/invalid values remain `partial`, with aggregate status/reason counts and a link to
the new run. This proves data freshness, not real pricing acceptance. Errors after ingestion never claim rollback
of accepted inputs or a fresh completed run. There is no polling acquisition scheduler or retry/fallback provider.

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
- **Integers** (`trade_version`, since 1.2) are a JSON integer or a string of digits; the backend stores an integer.

## 4 `describe()`

```python
{
  "name": "tda",
  "version": "0.3.0",                  # the adapter's own release
  "spi": 1,
  "contract": "private/1.2",           # §9
  "sources": [
    {"source": "positions_daily",     "dataset": "positions",      "time_basis": "start_of_day", "completeness": "full_snapshot", "scope": ["book_id"], "covers": "all_active_books"},
    {"source": "ocp_cash",            "datasets": ["ocp", "cash_balances"], "time_basis": "end_of_day", "completeness": "full_snapshot",
                                      "scope": {"ocp": ["entity"], "cash_balances": ["entity", "book_id"]}, "covers": "listed"},
    {"source": "pnl_tradelevel",      "dataset": "pnl_official",   "time_basis": "end_of_day",   "completeness": "full_snapshot", "scope": ["entity", "book_id"], "covers": "listed"},
    {"source": "internal_market_eod", "dataset": "internal_market","time_basis": "end_of_day",   "completeness": "full_snapshot", "scope": ["kind"], "covers": "listed"},
    {"source": "trades_hourly",       "dataset": "trades",         "time_basis": "intraday",     "completeness": "full_snapshot", "scope": ["book_id"], "covers": "listed"},
    {"source": "fx_terms",            "dataset": "trade_terms",    "time_basis": "end_of_day",   "completeness": "full_snapshot", "scope": ["book_id"], "covers": "listed"}
  ],
  "config_files":  [...],              # optional, below
  "settings_tabs": [...]               # optional, below (1.2)
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
- `settings_tabs` (list, optional, since 1.2; adapter phase A7): forms over `config_files`, so the firm's rules
  (entities, book types, funding, prices) are edited on tabs the adapter itself describes, and the backend knows
  nothing about them:

  ```python
  "settings_tabs": [{"id": "funding", "title": "Funding", "file": "funding.json", "help": "...",
                     "schema": {"type": "object", "properties": {...}, "required": [...]}}]
  ```

  `id` matches `^[a-z][a-z0-9_]{1,40}$` and is unique; `file` is the `name` of a usable `config_files` entry;
  `schema` is a JSON Schema (draft 2020-12) object of `type` `object` that the settings page renders as a form
  and validates the file with before it writes it. The backend stores none of it and never interprets the file:
  what reaches calculations goes through `reference()` (§7), validated by the contract schemas. Without the key,
  or for a tab of a bad shape (ignored with a warning, `desk.inbox.loader.check_settings_tabs`), the file stays
  editable as raw JSON. A change of a file changes the fingerprint (§2), so the next scan calls `reference()`.
  Status 22.10.2026 (FND-4, R40): the settings page renders each tab as a form (`prototypes/settings-forms.js`:
  tables with a card per row, enums and the lists of entities, books, currencies and the hub's indices as
  dropdowns, raw JSON under «Эксперт»). Before a write the backend checks the file against the tab's schema and runs
  `reference()` over a copy of the config folder with the file replaced, so the page shows the adapter's own issue
  codes, new ones apart (`POST /api/v1/adapter/settings/{tab}/check`, `desk.settings_forms`); the write
  (`PUT /api/v1/adapter/settings/{tab}`) refuses a schema error and a file changed since it was read, and each
  write is a revision in the store (`adapter_config_rev`, migration 0018: who and when). The page matches fields to
  lists by name (`entity`, `ocp_to`, `book_id`, `ccy`, `index_code`); an adapter needs nothing beyond the schema.

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
- A multi-date file (allowed for `pnl_official`, `trades` and, since 1.2, `cash_balances`: a balance history delivered at once) puts `_report_date` on every row; a row without it has the file's `report_date`. Each row's date is mapped like `report_date`, and each (scope unit, date) is a unit of its own (§6.6). Its revision key is the file's: a later file, say a restated history, outranks an earlier one for the same unit. A unit whose date has no calendar yet (`start_of_day` only) is rejected with `waiting_calendar` and the rest of the file is stored.
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
- `positions`, `ocp`, `cash_balances` and `trade_terms` are **stocks**. A read for date D takes, per unit, the current revision with the latest `valid_date` ≤ D, and reports its age. A unit older than the previous business day is flagged `stale_input` on the day-status screen, and the run's lineage gives the `valid_date` per book.
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

`reference(ctx)` returns reference rows the adapter owns: `{"books": [...]}` (§10), since 1.2 optionally `"entities": [...]` and `"funding_rules": [...]`, since 1.3 `"funding_policy": [...]` (§10; absent: none, and the backend keeps what it stored before), optionally with `"issues": [...]` shaped as in §6.3 (for example a malformed line of its mapping file); an `error` there fails the reference call. It is **required** when any source emits `book_id`. The backend calls it at load time and on `desk inbox scan`, and stores a new revision only when the content changed. Each book row may carry `valid_from`; without it, a change applies from its `known_at`, so a later change of `book_type` does not rewrite history. A `book_id` that is not in `books` gets a unit-scoped `unknown_book` error, raised by the backend.

`entities`, `funding_rules` and `funding_policy` are the firm's rules **as data** (adapter phase A7): the backend's funding engine is
generic and parameterised by them, never by firm constants in public code. They are stored like books, bitemporal
(`reference_rev`; `store.current_reference(kind, on)`), a new revision only when a row changed. Unlike books they
may carry history: several rows of one key with different `valid_from`, the one in force on date D being the
latest `valid_from` <= D (a row without `valid_from` applies from its first revision on, a later change from the
day it was recorded, as books). An invalid row, or a repeated (key, `valid_from`), is left out with an error issue
and the rest is kept; a book or rule naming an entity that `entities` does not list gets `unknown_entity`, a rule
naming an unknown book `unknown_book`, two rules with the same selectors and `valid_from` `ambiguous_rule`, a
`funding_policy` setting given twice with different values `ambiguous_policy` (all warnings). The kit (§8) prints the number of rows kept per table.

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
  - Minors so far: 1.0 (S7a); 1.1 adds the optional `positions` fields `maturity_date`, `risk_dv01_rub`, `risk_cs01_rub`; 1.2 (22.10.2026) adds `book_type` `FUTURES`, the schemas of `ocp`, `cash_balances` (with the optional key field `ledger`), `pnl_official` and the new dataset `trade_terms`, the optional reference tables `entities` and `funding_rules`, the optional `describe()["settings_tabs"]`, and multi-date `cash_balances`; 1.3 (22.10.2026, wave B1) adds the optional reference table `funding_policy` and the optional `positions` field `purchase_accrued_ccy`; 1.4 (23.10.2026) adds to `trade_terms` the `product_kind` values `irs`, `ois`, `ccs`, `repo` and `bond_purchase`, the `side` values `pay`, `receive`, `repo` and `reverse_repo`, and their optional fields (§10). The backend supports 1.4 and accepts 1.0–1.3 adapters unchanged (a 1.2 adapter without `funding_policy` gets the engine's provisional conventions, said in each run). A 1.4 adapter that emits the new products needs a backend on 1.4: an older one rejects those `trade_terms` units (`bad_value` of `product_kind`), as with `FUTURES`.
  - A new enum value (`FUTURES`) is a minor too: old adapters stay valid. An adapter that emits it needs a backend on 1.2; an older one rejects that book (`bad_value`), never silently re-types it.
- The adapter's README states the lowest backend version it needs. `desk adapter check` prints the backend version, the SPI, the contract and the adapter fingerprint.

## 10 Private input contract v1 (`private/1.4`)

The schemas live in `contract/private/v1/` (one per dataset and reference table, with a header-only CSV template each). The tables below are the design; the schema files are authoritative once written (S7a; `ocp`, `cash_balances`, `pnl_official`, `trade_terms`, `entities`, `funding_rules` since 1.2, and a test keeps each table equal to its schema). They extend contract 1.3 ([DATA_REQUIREMENTS](../DATA_REQUIREMENTS.md) §4) and supersede its templates (below). A dataset without a schema (`trades`, `internal_market`) is declared but its sources are rejected with `dataset_not_supported` until its slice.

**Storage.** `positions` units are position sets (S7a). Every other dataset with a schema is stored generically, so a new dataset needs a schema and no code: one `dataset_set` per accepted unit (dataset, scope unit, business date) with the revision rules of §6.6, its rows in `dataset_row` (canonical fields as JSON, `_src` and `x_*` beside), typed views `ocp_row`, `cash_balance_row`, `pnl_official_row`, `trade_terms_row` over every revision, and `store.dataset_rows(dataset, D)` for the rows in force (§6.7: stocks carried forward, flows on D only).

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
| `purchase_date`, `purchase_price_pct`, `purchase_yield_pct`, `contract_rate_pct` | | HTM, as in the public 1.3 template: the purchase lot the funding engine funds at `contract_rate_pct` from `purchase_price_pct` × the row's face |
| `purchase_accrued_ccy` | num | HTM (since private 1.3): the accrued interest paid at purchase, for the whole row; funded until the coupon that returns it when `funding_policy.htm_base` is `cash_spent` |
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

Key: (`entity`, `book_id`, `ccy`, `account_id`, `ledger`). A file may hold several dates (§6.1), for a history
delivered at once.

| Field | Type | Meaning |
|---|---|---|
| `entity` ●, `book_id` ● | str | |
| `ccy` ● | str | |
| `account_id` ● | str | The account or ledger line; a constant if the report has one line per currency |
| `balance_ccy` ● | num | `+` placed, `−` borrowed |
| `account_kind` | str | For example settlement, margin, nostro |
| `ledger` | str | The bookkeeping system the balance comes from, as the adapter codes it, when the same balance is reported by more than one system (their reconciliation is a backend screen, S9: the monitors' C15, `desk.cash`). Which system is authoritative on a break is not part of the contract: the backend compares the ledgers' sums per (`entity`, `book_id`, `ccy`) over the accounts, and a balance whose ledgers differ by more than max(0.01, 1e-8 × |balance|) (the rule of §6.4) has no known value, so it is not funded and is a funding problem; ledgers that agree, or one ledger alone, give the balance |

### `pnl_official` — flow; `full_snapshot`, scope `["entity", "book_id"]`, `time_basis = end_of_day`; may be multi-date (§6.1)

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
| `pnl_daily_rub` ● | num | Official daily PnL (the total of the official trade-level PnL report), RUB |
| `pnl_pl_daily_rub` ●, `pnl_oci_daily_rub` ● | num | Its P&L and OCI parts. The backend checks `pnl_daily_rub = pnl_pl_daily_rub + pnl_oci_daily_rub` per line (tolerance max(0.01, 1e-8 × \|total\|)) and records a mismatch as a warning `pnl_parts_mismatch` |
| `pnl_mtd_rub`, `pnl_ytd_rub` | num | As reported; never summed across dates |
| `accrual_rpt`, `price_change_rpt`, `fx_reval_rpt`, `premium_discount_rpt`, `new_trades_rpt`, `commission_rpt`, `brokerage_rpt`, `reserves_rpt`, `other_mtm_rpt`, `dividend_rpt` | num | Components in report units, not confirmed as RUB. Evidence for break analysis; the backend does not assume they add up to `pnl_daily_rub` |

A missing primary amount is an `error`. A missing component is `null`, never 0.

**Reconciliation keys** (S8, S11):
- bonds: (`valid_date`, `book_id`, `instrument_id`, falling back to `isin`, then `position_id`);
- FX: (`valid_date`, `book_id`, `ccy`);
- OTC: `trade_id` = `deal_id`.

The kit reports the share of PnL lines that join a position.

### `trade_terms` — stock; usually `full_snapshot`, scope `["book_id"]`, `covers = listed` (since 1.2)

The economic terms of OTC trades the backend values itself from terms instead of taking the report's PV (adapter
phase A9): their rates risk, the currency PV of their legs for the OCP, a reconciliation of PV; and the terms of
the deals the funding engine funds at their own rate. 1.2 defines FX forwards, NDFs and FX swaps; 1.4 adds interest
rate swaps (`irs`, `ois`), cross-currency swaps (`ccs`), REPO (`repo`) and HTM purchases (`bond_purchase`).
One row per trade, or per leg (`leg_id`) where a trade has several exchanges or legs. A trade missing from a later
snapshot of its book has matured or been closed. Key: (`trade_id`, `leg_id`). Amounts are unsigned: `side` gives
the direction. Which fields a product needs is listed under the table; a field a product does not use is left
out.

| Field | Type | Meaning |
|---|---|---|
| `trade_id` ● | str | The trade's id in the booking system: the `deal_id` (and OTC `position_id`) of its `positions` row and the `trade_id` of its `pnl_official` lines, where those exist |
| `leg_id` | str | The leg of a multi-leg trade, as the adapter codes it (an `fx_swap` has two rows, e.g. `near`, `far`); absent for a single-leg trade |
| `trade_version` | int | As reported |
| `book_id` ● | str | |
| `entity` | str | When the report has it |
| `position_id` | str | The `positions` row this trade is valued under, when not its `trade_id` |
| `product_kind` ● | enum | `fx_forward` (one exchange on `value_date`: a deliverable outright, or an NDF with `ndf`), `fx_swap` (one row per leg); since 1.4 `irs` (a single-currency swap whose floating leg is not an overnight compounded index, e.g. the key rate average), `ois` (a single-currency swap on a compounded overnight index, e.g. RUONIA), `ccs` (a cross-currency swap), one row per leg; `repo` (one row per deal, both legs); `bond_purchase` (one HTM purchase lot) |
| `source_product` | str | The report's own product code, as reported |
| `trade_date` ● | date | |
| `value_date` | date | The exchange's settlement date; for an NDF the settlement date of the difference. Required for every product of 1.2 |
| `base_ccy`, `quote_ccy` | str | The pair: `contract_rate` is `quote_ccy` per one `base_ccy` (USD/RUB: base USD, quote RUB). Required for every product of 1.2 |
| `side` | enum | FX: `buy`, `sell`: the desk buys or sells `base_ccy` on `value_date` (and pays or receives `quote_ccy`). Required for every product of 1.2. Since 1.4: a swap leg `pay` or `receive` (the desk pays or receives the leg's interest and, with `notional_exchange`, its final notional); a REPO `repo` (the desk borrows cash against the security) or `reverse_repo` (lends cash); a `bond_purchase` `buy` (the default) |
| `notional_base`, `notional_quote` | num | Unsigned amounts of the two legs, in `base_ccy` and `quote_ccy`. Required for every product of 1.2 |
| `contract_rate` | num | The agreed rate, `quote_ccy` per `base_ccy`; when absent the backend uses `notional_quote / notional_base` |
| `ndf` | bool | Non-deliverable: settled by the difference against the fixing, in `settle_ccy` (default false) |
| `settle_ccy` | str | NDF: the settlement currency |
| `fixing_date` | date | NDF: the fixing date; an NDF without it gets a warning `ndf_without_fixing` |
| `fixing_source` | str | NDF: the fixing as the report names it (a central-bank rate, an exchange fixing); the backend maps it to a hub fixing |
| `ccy` | str | Since 1.4: the currency of the row's amounts: a swap leg's currency, a REPO's cash, a purchase's face and accrued |
| `notional_ccy` | num | Since 1.4: a swap leg's notional; a REPO's cash amount of the first leg |
| `start_date` | date | Since 1.4: a swap leg's effective date (start of the first accrual period); a REPO's first-leg settlement |
| `maturity_date` | date | Since 1.4: a swap leg's end date as the trade states it; a REPO's second-leg date (absent: an open REPO) |
| `fixed_rate_pct` | num | Since 1.4: a fixed leg's rate; a fixed-rate REPO's rate |
| `index_code` | str | Since 1.4: a floating leg's or floating REPO's index, as the hub's fixing index code (`KEY_RATE`, `RUONIA`, ...) the adapter maps the report's name to (as in `funding_rules`) |
| `spread_bp` | num | Since 1.4: added to `index_code`; absent: 0 |
| `payment_frequency` | str | Since 1.4: a swap leg's period between payments, a tenor `<n>D`, `<n>W`, `<n>M` or `<n>Y`, or `term` (one payment at maturity) |
| `day_count` | enum | Since 1.4: `ACT/365` (fixed), `ACT/360`, `ACT/ACT` (ISDA), `30/360`, of a leg's or REPO's interest; absent: the backend's convention of the index or currency |
| `notional_exchange` | enum | Since 1.4, `ccs`: `none`, `final`, `initial_and_final`: which notional exchanges the leg has; absent: not stated |
| `source_instrument_id`, `source_instrument_name`, `isin`, `regnum`, `moex_secid`, `instrument_id` | str | Since 1.4: a REPO's security, a purchase's bond, as in `positions` (§6.1: `instrument_id` only from `ctx.instruments.lookup`) |
| `quantity` | num | Since 1.4: number of bonds of a REPO's security or a purchase |
| `face_amount_ccy` | num | Since 1.4: number of bonds × initial face, of a REPO's security or a purchase |
| `purchase_price_pct`, `purchase_yield_pct` | num | Since 1.4, `bond_purchase`: the clean purchase price in % of the current face, and the yield at it, as in `positions` |
| `contract_rate_pct` | num | Since 1.4, `bond_purchase`: the contract rate the lot is funded at, as in `positions` |
| `purchase_accrued_ccy` | num | Since 1.4, `bond_purchase`: the accrued interest paid at purchase, for the whole lot, as in `positions` |
| `counterparty_id` | str | As in the report or the adapter's mapping |
| `pv_rub` | num | Reconciliation: the PV as the bank's system reports it |

Rules beyond the schema (`desk.inbox.validate`), each an `error` for the row's unit unless said otherwise:
- `fx_forward`, `fx_swap` (1.2): without `value_date`, `base_ccy`, `quote_ccy`, `side` or a notional
  `missing_required`, as is an `fx_swap` row without `leg_id`; `base_ccy` = `quote_ccy` is `bad_value`; an NDF
  without `fixing_date` a warning `ndf_without_fixing`.
- `irs`, `ois`, `ccs` (1.4): a row is one leg and needs `leg_id`, `side` (`pay`, `receive`), `ccy`, `notional_ccy`,
  `start_date` and `maturity_date` (`missing_required`), and exactly one of `fixed_rate_pct` (a fixed leg) and
  `index_code` (a floating leg). A trade needs a `pay` and a `receive` leg (`missing_required`); the legs of an `irs`
  or `ois` are in one currency, those of a `ccs` in two, and all rows of a `trade_id` have one `product_kind`
  (`bad_value`).
- `repo` (1.4): needs `side` (`repo`, `reverse_repo`), `ccy`, `notional_ccy`, `start_date` and exactly one of
  `fixed_rate_pct` and `index_code`; a REPO without any instrument field is a warning `repo_without_security` (the
  funding engine needs it for `funding_policy.repo_rate` `security`).
- `bond_purchase` (1.4): needs `ccy`, `purchase_price_pct`, one instrument field and `quantity` or `face_amount_ccy`.
  The purchase date is `trade_date`, its settlement `value_date`.
- Every product: `side` of another product is `bad_value`, as is a negative `notional_ccy`, `quantity` or
  `face_amount_ccy`, a non-zero `spread_bp` with `fixed_rate_pct`, and `maturity_date` not after `start_date`.

Status 25.10.2026: the backend validates and stores the 1.4 rows (`dataset_rows`). Valuation
(`valuation.derivatives`, FlyingScotman/trading-desk-monitor#45) reads the FX kinds, `irs` / `ois` on `RUONIA` or
`KEY_RATE` in RUB (core's swap, risk per curve) and `repo` with a `maturity_date` (fixed, or `RUONIA` / `KEY_RATE`);
a `ccs`, a swap with two floating legs, an open REPO and a REPO on another index keep the report's value with the
reason (`ccs_unsupported`, `basis_swap_unsupported`, `repo_open`, `no_fixings`). A swap leg's `payment_frequency`
and `day_count` replace the exchange convention of its index (absent: that convention, provisional); `30/360` and
day or week frequencies are not valued. The funding of REPO and HTM lots from terms comes with the funding engine
(FlyingScotman/trading-desk-monitor#55). The typed view `trade_terms_row` has the 1.4 fields as columns after the 1.2
ones (migration 0020); every field is also in its `data`.

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

As 1.3 `books`: `book_id` ●, `name` ●, `desk` ●, `book_type` ● (`TRADING`, `HTM`, `DERIVS`, `FUTURES`), `entity` ●, `ocp_to`, `trader`, `active` (default true), `valid_from`. `book_type` and `entity` are required here; 1.3 left them optional by mistake.

`book_type` is the funding regime of the book, whatever the firm calls it: `TRADING` (a bond book funded on its market value), `HTM` (funded on its cost), `DERIVS` (funded on its cash balances), `FUTURES` (since 1.2: a book whose PnL is settled daily as variation margin, not funded). How each type is funded, at which rate, comes from `funding_rules`; `FUTURES` with a rule of `rate_kind` `none` is "not funded". `ocp_to`: the book the end-of-day currency position is transferred to, when the bank does that.

### `entities` — from `reference()` (optional, since 1.2)

The legal entities the books belong to (the `entity` of books and rows). Key: `entity`, with `valid_from`.

| Field | Type | Meaning |
|---|---|---|
| `entity` ● | str | The entity code exactly as books and rows carry it |
| `name` | str | |
| `funding_scheme` ● | enum | `by_book_type`: each book is funded by the rules of its `book_type`; `cash_balance`: the entity's whole cash balance is funded, whatever its books |
| `pnl_sweep` | enum | `daily`, `monthly`, `quarterly`, `none`: how often the entity's PnL is swept out of its cash (a sweep shows as a jump in `cash_balances`); absent: not known |
| `active` | bool | Default true |
| `valid_from` | date | First business date the row applies (§7) |

### `funding_rules` — from `reference()` (optional, since 1.2)

How books are funded: what is funded (the basis) and at which rate, per selectors. Key: `rule_id`, with `valid_from`
(a change of rate is a new row of the same rule with a later `valid_from`, so past days keep the rates of their
time).

| Field | Type | Meaning |
|---|---|---|
| `rule_id` ● | str | The adapter's stable id of the rule |
| `valid_from` | date | First business date the row applies (§7) |
| `active` | bool | Default true; false retires the rule from `valid_from` on |
| `book_type` | enum | Selector: as `books` |
| `entity` | str | Selector |
| `book_id` | str | Selector: a special rate of one book |
| `ccy` | str | Selector: the currency funded |
| `instrument_id` | str | Selector: a special rate of one security (a hub instrument id) |
| `basis` | enum | What is funded: `mv_clean`, `mv_dirty` (market value without or with accrued), `cash` (the cash balance), `cost` (the purchase cost; whether the accrued paid at purchase is funded is OQ-02), `none`; absent: the engine's default for the book type (`HTM` cost, `DERIVS` cash, `FUTURES` none; `TRADING` has none, so its rules must give `mv_clean` or `mv_dirty`) |
| `rate_kind` ● | enum | `index_spread` (an index plus `spread_bp`), `fixed` (`fixed_rate_pct`), `contract` (the position's or trade's own contract rate), `none` (not funded) |
| `index_code` | str | `index_spread`: the hub's fixing index code (`KEY_RATE`, `RUONIA`, ...); required for it |
| `spread_bp` | num | `index_spread`: added to the index; absent: 0 |
| `fixed_rate_pct` | num | `fixed`: required for it |
| `day_count` | enum | `ACT/365`, `ACT/360`; absent: the engine's default for the currency |

A rule **applies** to a position (entity, book, book type, currency, instrument) on date D when it is in force on D
and every selector it gives equals the position's; an absent selector matches anything. Of the rules that apply, the
most specific wins: the one that gives the first selector, in the order `instrument_id`, `book_id`, `entity`,
`book_type`, `ccy`, that the others do not. Rules with equal selectors are ambiguous (`ambiguous_rule`); the engine
then takes the lowest `rule_id` and flags it. Interest is simple, accrued per calendar day and converted to RUB by
the engine; nothing here compounds. A position no rule applies to is not funded and is flagged by the engine.

### `funding_policy` — from `reference()` (optional, since 1.3)

The organisation's funding conventions that are not a rate: its answers to the open decisions the generic engine
leaves as settings (DEC01 timing, OQ-02 HTM base, DEC03 REPO, FND-13/14 write-off). Key: `policy_id`, with
`valid_from`. A row **without** `book_type` is firm-wide and gives any of the firm-wide fields; a row **with**
`book_type` gives only that book type's `pnl_sweep`. A field no row gives keeps the engine's provisional default
(`rules.funding.OPEN_DECISIONS`), shown as such in the run (`policy.provisional`); a setting two rows in force give
with different values is `ambiguous_policy` (warning) and also keeps the default. The values are the engine's
(`desk.rules.funding`), so a row maps one to one onto `FundingPolicy` (`rules.reference.apply_funding_policy`).

| Field | Type | Meaning |
|---|---|---|
| `policy_id` ● | str | The adapter's stable id of the row |
| `valid_from` | date | First business date the row applies (§7) |
| `active` | bool | Default true; false retires the row from `valid_from` on |
| `book_type` | enum | As `books`: the row gives this book type's `pnl_sweep`; absent: a firm-wide row |
| `pnl_sweep` | enum | With `book_type` only: `daily`, `monthly`, `quarterly`, `none`, how often the PnL of books of the type is written off (an entity's own `pnl_sweep` in `entities` wins) |
| `htm_base` | enum | Firm-wide: `cash_spent` (the clean cost plus the accrued paid at purchase, `positions.purchase_accrued_ccy`, until the coupon that returns it) or `clean_cost` (OQ-02); absent: an HTM lot is not funded (a problem) |
| `mv_base_date` | enum | Firm-wide: `previous_business_day` or `as_of`, whose MV funds the period (DEC01) |
| `cash_base_date` | enum | Firm-wide: `previous_business_day` or `as_of`, whose cash balance funds the period (DEC01) |
| `new_positions` | enum | Firm-wide: `next_period`, `last_day` or `full_period` (a position opened in the period, DEC01) |
| `closed_positions` | enum | Firm-wide: `full_period` or `not_funded` (a position closed in the period, DEC01) |
| `fx_date` | enum | Firm-wide: `as_of`, `previous_business_day` or `each_day`, the date of the CBR rate that converts interest to RUB (DEC01) |
| `out_of_currency` | enum | Firm-wide: `security_ccy` or `settlement_ccy`, the funding currency of a bond settled in another currency |
| `entity_cash_book_type` | enum | Firm-wide: the book type whose rules fund the cash of an entity of scheme `cash_balance`; absent: the rules that apply to the balance |
| `repo_rate` | enum | Firm-wide: `contract` (the deal's rate) or `security` (the collateral's rate, as a bond of `repo_security_book_type`) (DEC03) |
| `repo_contract_from_term_days` | int | Firm-wide: a REPO of at least this term (days) is funded at its contract rate even with `repo_rate` `security` (DEC03 residual) |
| `repo_security_book_type` | enum | Firm-wide: the book type whose rules give "the collateral's rate"; absent: `TRADING` |
| `write_off_date` | enum | Firm-wide: `last_calendar_day` or `last_business_day` of the write-off period |

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

### Contract 1.3 templates superseded

The 1.3 CSV templates of the prototype (`contract/csv/`, [DATA_REQUIREMENTS](../DATA_REQUIREMENTS.md)) stay for the
prototype `tdm`; for the backend the private contract replaces them:

| 1.3 template | Superseded by |
|---|---|
| `positions.csv` | `positions` |
| `books.csv` | `reference()["books"]` |
| `funding_rates.csv` | `reference()["funding_rules"]` (its selectors `book_type`, `book_id`, `entity`, `instrument_id`, `ccy` and rate `index_name` + `spread_bp` or `fixed_rate` carry over) |
| `cash_balances.csv` | `cash_balances` |
| `pnl_official.csv` | `pnl_official` |
| `otc_deals.csv` | `trade_terms` (FX forwards since 1.2; IRS, OIS, CCS since 1.4; CDS: a later minor) and `positions` (`otc` rows with the report's PV and risk) |
| `funding.csv` (REPO and funding deals) | `trade_terms` (REPO since 1.4) |
| `trades.csv` | `trades` |

### Drafts (not in 1.4)

Designs agreed in direction, without a schema yet; each becomes a minor with its slice:

- Done in 1.4: `trade_terms` for swaps, REPO and HTM purchases (above). The official names: a leg's direction is
  `side` (`pay`, `receive`) and `leg_id` is the adapter's own code of the leg; the amount is `notional_ccy` (units in
  names); a rate, of a swap leg and of a REPO alike, is `fixed_rate_pct` or `index_code` + `spread_bp`.
- `price_policy` (adapter A10), a reference table: per security (`instrument_id`, falling back to `isin`) a `policy`
  (`sticky_internal`: the last internal EOD price holds until the next one and the hub's price does not replace it
  intraday), with `valid_from`. A security without a hub price is valued only at the internal price, else down the
  ladder with a warning.
- Extra spreads by bond class for `funding_rules` (the engine's `ExtraSpread`): a new table or selector, once the
  rules are settled. (The REPO policy, the security's rate or the contract rate from a term threshold, is
  `funding_policy` since 1.3; the REPO deals themselves are `trade_terms` `repo` since 1.4.)
- `trades`, `internal_market`: as designed above, with S13 and S10.

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
| **I7 Settings and rules** | `settings_tabs` (§4) on the settings page; `reference()` `entities`, `funding_rules` (1.2) and `funding_policy` (1.3) validated and stored (§7); the generic funding engine reads them in every run (B1: `publish.funding_run`) | A7: the tabs, `entities.json`, `funding.json` (rules and the `funding_policy` conventions), FUTURES books, `purchase_accrued_ccy` of HTM rows | the tabs open and save; the engine's funding of a synthetic book on synthetic rules equals a hand calculation (`test_funding_run.py`) |
| **I8 Cash balances** | `cash_balances` schema, storage and multi-date history (§6.1, 1.2); funding of cash-funded books (S9) | A8 | a day's balances equal the report; a history file fills the past dates; two ledgers side by side |
| **I9 Trade terms** | `trade_terms` schema and storage (1.2, FX; 1.4 swaps, REPO, HTM purchases); valuation from terms by core (S14) | A9 | PV of FX forwards from terms vs `pv_rub`; the currency PV of legs enters the derived OCP |
| **I10 Price policy** | `price_policy` (draft, §10) and the sticky internal price with S10 | A10 | a security on the list keeps its internal price intraday |

Each point is done when the kit passes on the synthetic fixtures **and** the owner's dry run at work shows no `error` issues.

Status 22.10.2026: I1 and I2 are live. The backend has the schemas and the storage of I3 (official PnL), I4
(`ocp`), I7 (reference tables, settings tabs as a contract), I8 and I9; the screens and calculations on them come
with their slices (S8, S9, S11, S14) and the funding engine. The reference adapter reads all of them from canonical
CSV (`desk.inbox.reference`), so the backend builds on them before the private adapter emits them.

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
