# Private input contract v1 (`private/1.9`)

The rows a private adapter emits, one JSON Schema per dataset (`<dataset>.schema.json`) and per reference table of
`reference()`, authoritative over the design tables of [08 §10](../../../docs/architecture/08_PRIVATE_ADAPTER_INTERFACE.md):

| Schema | What | Since |
|---|---|---|
| `positions` | positions (stock) | 1.0 (1.1: risk fields; 1.3: `purchase_accrued_ccy`) |
| `books` | `reference()["books"]` | 1.0 (1.2: `book_type` `FUTURES`) |
| `ocp` | the open currency position as the bank reports it (stock) | 1.2 (1.7: `derivatives_ccy`; 1.9: `spot_future`) |
| `cash_balances` | cash balances per book, account and ledger (stock; a file may hold a history, 08 §6.1) | 1.2 |
| `pnl_official` | the official daily PnL, one row per report line (flow) | 1.2 |
| `trade_terms` | economic terms of OTC trades: FX forwards, NDFs, FX swap legs (stock) | 1.2 (1.4: IRS, OIS and CCS legs, REPO, HTM purchases) |
| `trades` | the day's trades, one row per trade version: a cumulative export per book or deltas (flow; a file may hold several dates, 08 §6.1) | 1.6 |
| `trade_list` | the live trades of the owner's books without their terms: id, version, book, status (stock) | 1.8 |
| `entities` | `reference()["entities"]`: legal entities and their funding scheme (optional) | 1.2 |
| `funding_rules` | `reference()["funding_rules"]`: funding basis and rate per selectors, with history (optional) | 1.2 |
| `funding_policy` | `reference()["funding_policy"]`: the funding conventions that are not a rate (HTM base, timing, REPO policy, write-off), with history (optional) | 1.3 |
| `ocp_policy` | `reference()["ocp_policy"]`: the OCP conventions (which components the bank's OCP report holds, what is published, the amount basis, NDFs, cash, settlement window, tolerance, report age), with history (optional) | 1.5 (1.7: `report_base_plus_day`, `report_max_age_bd`) |

`internal_market` is declared in 08 but has no schema yet: it comes with its slice (S10).

Amounts are a finite JSON number or a decimal string, integers (`trade_version`) a JSON integer or a string of
digits, dates are `YYYY-MM-DD`, timestamps (`trades.trade_ts`) ISO 8601 `YYYY-MM-DDTHH:MM[:SS[.ffffff]][Z|+HH:MM]` (without an offset: Moscow time), an optional field may be absent or `null` (the same thing), `""` is invalid
everywhere, currency codes are upper-cased (`RUR` is `RUB`), raw firm values go in `x_*` keys and `_src` (the row's
location, no sheet names) is required. The backend validates with its own standard-library validator
(`desk.inbox.schema`), so the schemas use only `type`, `enum`, `required`, `properties`, `patternProperties`,
`additionalProperties`, `pattern`, `minLength`, `format: date`, `format: date-time` (1.6), `default`, local `$ref`
and, since 1.7, an `object` field with its own `properties` (`ocp.derivatives_ccy`) and, since 1.9, an `array` field
with `items` (`ocp.spot_future`: an object with `required` and `properties`). A rule across fields (a
bond needs a size, an FX trade its legs, a funding rule of `index_spread` its index) is checked in
`desk.inbox.validate` and listed in 08 §10.

Each `<name>.csv` is the canonical CSV template of a schema (header only, `;`, UTF-8 with BOM, CRLF, as
`contract/csv/`), read by the reference adapter `desk.inbox.reference`: `<dataset>_YYYYMMDD.csv` in the inbox
(`trades`: `trades_YYYYMMDD[_HHMM].csv`, a cumulative export cut at HH:MM, and `trades_delta_YYYYMMDD[_HHMM].csv`,
deltas), `books.csv`, `entities.csv`, `funding_rules.csv`, `funding_policy.csv`, `ocp_policy.csv` in the adapter
config folder. The backend ships the schemas as package data (`backend/src/desk/inbox/schemas/v1/`); a test keeps that copy byte-identical to this one, so change
both.

Optional risk fields of `positions`, minor 1.1 (all optional, so a `private/1.0` adapter stays valid and is
accepted as before): `maturity_date`, `risk_dv01_rub` (RUB PV change for a +1 bp parallel rates shift) and `risk_cs01_rub` (RUB
PV change for +1 bp of credit spread). The backend uses them only for rows it does not value itself: an `otc` or
`other` row is valued at its `mv_rub` (quality `reported_value`), its DV01 put into the RUB rates bucket nearest
`maturity_date`, its CS01 into CS01; an unmapped bond on the `reported_value` rung takes them too. Fill them only
when the report's figure is reliable.

Minor 1.2 is additive: every 1.0 and 1.1 adapter stays valid. An adapter that emits a `FUTURES` book needs a
backend on 1.2 (an older one rejects that book with `bad_value`). The 1.3 templates of `contract/csv/` that these
schemas replace are listed in 08 §10 ("Contract 1.3 templates superseded") and in `contract/csv/SUPERSEDED.md`.

Minor 1.3 (wave B1, the funding engine in the runs) is additive too: the optional reference table `funding_policy`
(the firm's answers to the open funding conventions: `htm_base`, the DEC01 timing fields, the REPO policy, the
write-off periodicity per book type and its date; 08 §10) and the optional `positions` field `purchase_accrued_ccy`
(the accrued paid at an HTM purchase). A 1.2 adapter without them is accepted; its runs use the engine's
provisional conventions and say so (`policy.provisional` of the run's funding block), and an HTM lot is a problem
until `htm_base` is given.

Minor 1.4 is additive too: `trade_terms` gains the `product_kind` values `irs`, `ois`, `ccs` (one row per leg),
`repo` and `bond_purchase`, the `side` values `pay`, `receive`, `repo`, `reverse_repo`, and their optional fields
(`ccy`, `notional_ccy`, `start_date`, `maturity_date`, `fixed_rate_pct`, `index_code`, `spread_bp`,
`payment_frequency`, `day_count`, `notional_exchange`, the instrument fields, `quantity`, `face_amount_ccy` and the
HTM purchase fields of `positions`). Which product needs which field is in 08 §10 under the `trade_terms` table. A
1.0–1.3 adapter stays valid; one that emits the new products needs a backend on 1.4 (an older one rejects those
units with `bad_value`). The backend stores them; valuation of swaps and funding of REPO and HTM lots from these
terms come with their slices.

Open points (the contract leaves them to the adapter's data and to later minors, 08 §10 "Drafts"): which ledger of
`cash_balances` is authoritative on a break; the hub fixing a `trade_terms.fixing_source` maps to; the default day
count of a `funding_rules` row without `day_count`; extra spreads by bond class; the `price_policy` table.

Minor 1.5 (TDM#180, TDM#416: the published OCP) is additive: the optional reference table `ocp_policy`, the firm's
OCP conventions (`report_components`, the components the bank's OCP report holds, comma-separated keys of
`desk.rules.ocp.COMPONENTS`; `published`; `amount_basis`; `include_ndf`; `cash_from_balances`; `settle_window_bd`; `tolerance_ccy`;
`report_max_age_days`; 08 §10). The run publishes the report plus the components outside it, derived from positions,
and the derived OCP where no report unit covers a book. A 1.0–1.4 adapter stays valid; its runs use the OCP engine's
provisional conventions and say so (`policy.provisional` of the run's OCP block).

Minor 1.6 (S13a, TDM#525) is additive: the schema of the dataset `trades`, declared since 1.0 and rejected with
`dataset_not_supported` until now. One row per trade version (08 §10): the current row of a `trade_id` is its highest
`trade_version`; a cancelled trade stays with `status` `cancelled`; a late booking (`trade_date` before the report
date) is a trade of the date. A source is a cumulative export (`full_snapshot`, scope `book_id`: it replaces the
book's trades of the date, ordered by `report_ts`) or deltas (`increment`: they add). Quantities and amounts are
unsigned (`side` gives the direction; a negative one is `bad_value`). A 1.0–1.5 adapter stays valid; one that emits
`trades` needs a backend on 1.6 (an older one rejects those sources with `dataset_not_supported`). The backend stores
and serves them (`GET /api/v1/trades`); valuing them and new-trade PnL come with the rest of S13.

Minor 1.7 (TDM#531: the published OCP «report base + day») is additive: the optional `ocp` field `derivatives_ccy`,
an object «derivative kind → the signed nominal of the report's lines of that kind with a value date after the
report's date, left out of `ocp_ccy`». Kinds: `fx_forward`, `fx_swap`, `fx_future`, `ndf`, `fx_option`, `swap`,
`other`; amounts as everywhere, a zero kept (a key, even `"0"`, says the kind is apart); canonical, so in the unit's
change hash. The split is per report file and kind: a kind is apart for every unit of a file when any row of the file
has its key. `ocp_policy.published` takes the value `report_base_plus_day` (the engine's default for dates from
03.11.2026; 08 §10 `ocp`) and the new optional `ocp_policy.report_max_age_bd` (business days, default 1). A 1.0–1.6
adapter stays valid: its `ocp` files hold every derivative at its nominal, the run adds no PV and says so in its gaps.
An adapter that emits `derivatives_ccy` needs a backend on 1.7 (an older one drops the field with
`field_from_newer_contract` and reads `ocp_ccy` as the whole report).

Minor 1.9 (TDM#703) is additive: the optional `ocp` field `spot_future`, a list of the report's lines of a spot type
with a future value date, `{value_date, amount_ccy, pair, src}` (`value_date` and `amount_ccy` required; the signed
amount in the row's currency). Such a line is the nominal of a forward, an NDF or a future that the report wrote as a
spot line: it stays in `ocp_ccy`, and the monitor takes a line that matches a deal of the book out of the base and counts
the deal itself (TDM#693). Canonical, so in the unit's change hash: a report accepted before and read again with an
adapter of 1.9 is a new revision. An empty list is the field absent; the order of the entries does not matter. An adapter
0.9.14 gave the lines in `x_spot_future`; the monitor reads it while a row has no `spot_future`. A 1.0–1.8 adapter stays
valid. An adapter that emits `spot_future` needs a backend on 1.9 (an older one drops the field with
`field_from_newer_contract`): update the monitor first, then the adapter. During the transition the adapter may, and is
advised to, give `spot_future` and `x_spot_future` with the same content: a backend of 1.9 reads `spot_future` and never
adds the two, a backend before 1.9 reads `x_spot_future`, so a wrong order of installation does not bring the double
count back.
