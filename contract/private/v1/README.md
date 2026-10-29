# Private input contract v1 (`private/1.5`)

The rows a private adapter emits, one JSON Schema per dataset (`<dataset>.schema.json`) and per reference table of
`reference()`, authoritative over the design tables of [08 §10](../../../docs/architecture/08_PRIVATE_ADAPTER_INTERFACE.md):

| Schema | What | Since |
|---|---|---|
| `positions` | positions (stock) | 1.0 (1.1: risk fields; 1.3: `purchase_accrued_ccy`) |
| `books` | `reference()["books"]` | 1.0 (1.2: `book_type` `FUTURES`) |
| `ocp` | the open currency position as the bank reports it (stock) | 1.2 |
| `cash_balances` | cash balances per book, account and ledger (stock; a file may hold a history, 08 §6.1) | 1.2 |
| `pnl_official` | the official daily PnL, one row per report line (flow) | 1.2 |
| `trade_terms` | economic terms of OTC trades: FX forwards, NDFs, FX swap legs (stock) | 1.2 (1.4: IRS, OIS and CCS legs, REPO, HTM purchases) |
| `entities` | `reference()["entities"]`: legal entities and their funding scheme (optional) | 1.2 |
| `funding_rules` | `reference()["funding_rules"]`: funding basis and rate per selectors, with history (optional) | 1.2 |
| `funding_policy` | `reference()["funding_policy"]`: the funding conventions that are not a rate (HTM base, timing, REPO policy, write-off), with history (optional) | 1.3 |
| `ocp_policy` | `reference()["ocp_policy"]`: the OCP conventions (which components the bank's OCP report holds, what is published, the amount basis, NDFs, cash, settlement window, tolerance, report age), with history (optional) | 1.5 |

`trades` and `internal_market` are declared in 08 but have no schema yet: they come with their slices.

Amounts are a finite JSON number or a decimal string, integers (`trade_version`) a JSON integer or a string of
digits, dates are `YYYY-MM-DD`, an optional field may be absent or `null` (the same thing), `""` is invalid
everywhere, currency codes are upper-cased (`RUR` is `RUB`), raw firm values go in `x_*` keys and `_src` (the row's
location, no sheet names) is required. The backend validates with its own standard-library validator
(`desk.inbox.schema`), so the schemas use only `type`, `enum`, `required`, `properties`, `patternProperties`,
`additionalProperties`, `pattern`, `minLength`, `format: date`, `default` and local `$ref`. A rule across fields (a
bond needs a size, an FX trade its legs, a funding rule of `index_spread` its index) is checked in
`desk.inbox.validate` and listed in 08 §10.

Each `<name>.csv` is the canonical CSV template of a schema (header only, `;`, UTF-8 with BOM, CRLF, as
`contract/csv/`), read by the reference adapter `desk.inbox.reference`: `<dataset>_YYYYMMDD.csv` in the inbox,
`books.csv`, `entities.csv`, `funding_rules.csv`, `funding_policy.csv`, `ocp_policy.csv` in the adapter config folder. The backend ships the schemas as
package data (`backend/src/desk/inbox/schemas/v1/`); a test keeps that copy byte-identical to this one, so change
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
