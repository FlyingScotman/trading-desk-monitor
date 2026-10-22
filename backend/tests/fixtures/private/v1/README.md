# Canonical private-contract fixtures (v1)

Synthetic positions in the canonical v1 CSV (`;`, UTF-8 with BOM, CRLF), read by the reference adapter
`desk.inbox.reference` (source `positions_csv`, `start_of_day`, `covers=listed`) and by the test adapter
`../../adapters/good_adapter` (same files, source `positions_daily`, `covers=all_active_books`). No real data.

`datasets/` holds the fixtures of the other datasets and reference tables of private/1.2 (`ocp`,
`cash_balances`, `pnl_official`, `trade_terms`, entities, funding rules) with their own config folder: see its
README.

`config/` is the adapter config folder (`DESK_PRIVATE_ADAPTER_CONFIG`): `books.csv` has B1 (TRADING), B2 (HTM),
B3 (TRADING), all entity ENT1 and active, and B9, inactive. Valid dates below use the RU_PRODUCTION calendar
of the static snapshot in `../../mdhub_v1` (2026-01-01..2027-12-31).

| File | Report date | Valid date | What it tests |
|---|---|---|---|
| `positions_20261014.csv` | Wed 14.10 | Tue 13.10 | clean: B1 (two bonds, one FX row), B2 (incl. an amortizer with face and outstanding face), B3 (one bond with an ISIN not in static data: unmapped) |
| `positions_20261015.csv` | Thu 15.10 | Wed 14.10 | **unit-scoped error**: B2's quantity `12,5` is `bad_number`, so B2 is rejected and B1 accepted (`partially_accepted`). **Book closed by omission**: B3 is absent; with `good_adapter` it becomes an empty unit with `book_closed_by_omission`, with the reference adapter (`listed`) it is untouched |
| `positions_20261016.csv` | Fri 16.10 | Thu 15.10 | the original; B3 is a **book marker** (a row with only `book_id`): covered and empty, which closes B3 under `listed` |
| `positions_20261016_2.csv` | Fri 16.10 | Thu 15.10 | the **correction** (`seq` 2): B1's SEC-26238 quantity 12500 instead of 12000. Offer it before the original: the original then sorts below it and is `superseded` |
| `positions_20261017.csv` | **Sat** 17.10 | Fri 16.10 | a Saturday report: B1, B2; B3 absent again |
| `positions_20261019.csv` | Mon 19.10 | Fri 16.10 | a **stale copy**: the same bytes as the Saturday file under Monday's name (`stale_copy`, 08 §13) |

B1's quantity of SEC-26238 grows by date (10000, 11000, 12000/12500, 13000), so the revision in force on a date
can be read off it.
