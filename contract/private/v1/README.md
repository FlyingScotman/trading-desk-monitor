# Private input contract v1 (`private/1.1`)

The rows a private adapter emits, one JSON Schema per dataset (`<dataset>.schema.json`), authoritative over the
design tables of [08 §10](../../../docs/architecture/08_PRIVATE_ADAPTER_INTERFACE.md); only `positions` and `books`
exist so far, the other datasets come with their slices. Amounts are a finite JSON number or a decimal string,
dates are `YYYY-MM-DD`, an optional field may be absent or `null` (the same thing), `""` is invalid everywhere,
raw firm values go in `x_*` keys and `_src` (the row's location, no sheet names) is required. The backend
validates with its own standard-library validator (`desk.inbox.schema`), so the schemas use only `type`, `enum`,
`required`, `properties`, `patternProperties`, `additionalProperties`, `pattern`, `minLength`, `format: date` and
local `$ref`. `positions.csv` and `books.csv` are the canonical CSV templates read by the reference adapter
`desk.inbox.reference` (header only, `;`, UTF-8 with BOM, as `contract/csv/`). The backend ships the schemas as package data
(`backend/src/desk/inbox/schemas/v1/`); a test keeps that copy byte-identical to this one, so change both.

Optional risk fields of `positions`, minor 1.1 (all optional, so a `private/1.0` adapter stays valid and is
accepted as before): `maturity_date`, `risk_dv01_rub` (RUB PV change for a +1 bp parallel rates shift) and `risk_cs01_rub` (RUB
PV change for +1 bp of credit spread). The backend uses them only for rows it does not value itself: an `otc` or
`other` row is valued at its `mv_rub` (quality `reported_value`), its DV01 put into the RUB rates bucket nearest
`maturity_date`, its CS01 into CS01; an unmapped bond on the `reported_value` rung takes them too. Fill them only
when the report's figure is reliable.
