# Private input contract v1 (`private/1.2`)

The rows a private adapter emits, one JSON Schema per dataset (`<dataset>.schema.json`) and per reference table of
`reference()`, authoritative over the design tables of [08 §10](../../../docs/architecture/08_PRIVATE_ADAPTER_INTERFACE.md):

| Schema | What | Since |
|---|---|---|
| `positions` | positions (stock) | 1.0 (1.1: risk fields) |
| `books` | `reference()["books"]` | 1.0 (1.2: `book_type` `FUTURES`) |
| `ocp` | the open currency position as the bank reports it (stock) | 1.2 |
| `cash_balances` | cash balances per book, account and ledger (stock; a file may hold a history, 08 §6.1) | 1.2 |
| `pnl_official` | the official daily PnL, one row per report line (flow) | 1.2 |
| `trade_terms` | economic terms of OTC trades: FX forwards, NDFs, FX swap legs (stock) | 1.2 |
| `entities` | `reference()["entities"]`: legal entities and their funding scheme (optional) | 1.2 |
| `funding_rules` | `reference()["funding_rules"]`: funding basis and rate per selectors, with history (optional) | 1.2 |

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
`books.csv`, `entities.csv`, `funding_rules.csv` in the adapter config folder. The backend ships the schemas as
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

Open points (the contract leaves them to the adapter's data and to later minors, 08 §10 "Drafts"): which ledger of
`cash_balances` is authoritative on a break; the hub fixing a `trade_terms.fixing_source` maps to; the default day
count of a `funding_rules` row without `day_count`; REPO and swap terms; the `price_policy` table.
