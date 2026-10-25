# Canonical fixtures of private contract 1.2–1.4 (datasets beyond positions)

Synthetic files in the canonical v1 CSV (`;`, UTF-8 with BOM, CRLF) for the reference adapter
`desk.inbox.reference`. All `end_of_day`, so the date in the name is the business date and no calendar is
needed. No real data: books, entities, rates and amounts are invented.

`config/` is the adapter config folder: `books.csv` (T1 `TRADING`, D1 `DERIVS`, F1 `FUTURES` of ENT1; C1
`TRADING` of ENT2), `entities.csv` (ENT1 `by_book_type`, ENT2 `cash_balance`), `funding_rules.csv` (demo rules
per book type and currency; `trading_rub` has two rows, the second from 2026-10-19 with a wider spread; `derivs_usd` and `ent2_cash_usd` fund the USD balances of the cash
history, B3). `funding_policy.csv` (private/1.3): a firm-wide row (`htm_base` `cash_spent`, the DEC01 timing at the engine's
neutral choices, REPO at the security's rate from 30 days at the contract rate, write-off on the last calendar
day) and `TRADING` swept monthly.

| File | What it tests |
|---|---|
| `ocp_20261020.csv` | `ocp` of D1 (USD in lower case: normalised), T1 a **marker** (covered, no rows) |
| `ocp_20261021.csv` | the next day: D1's CNY row is gone (closed by the snapshot), T1 not listed (`listed`: untouched) |
| `cash_balances_20261020.csv` | a **multi-date** file: rows of 2026-10-16 (`_report_date`) and of the file's date; D1 RUB in two ledgers (S1, S2) that **disagree** (a cash break) |
| `cash_balances_20261021.csv` | the next history file (B3, #61): rows of 2026-10-19 (both ledgers agree) and of the file's date: D1 RUB S1 as two accounts whose sum agrees with S2 (`match`), D1 USD in S1 only (`one_ledger`), C1 of ENT2 without a ledger (`single`) |
| `pnl_official_20261020.csv` | `trade_version` as an integer; K3's parts do not add up: `pnl_parts_mismatch` warning |
| `trade_terms_20261020.csv` | a deliverable forward, an NDF with fixing, an FX swap as two legs (D1, accepted); F1's swap row without `leg_id` is an error, so F1 is rejected (`partially_accepted`) |
| `trade_terms_20261021.csv` | private/1.4: an IRS (fixed against the key rate), an OIS on RUONIA with a spread, a CCS RUB/CNY with notional exchanges (D1, two legs each), a REPO and an HTM purchase lot (T1; the contract does not tie a product to a book type); all accepted |
