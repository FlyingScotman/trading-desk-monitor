# Private input contract v1 (`private/1.0`)

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
