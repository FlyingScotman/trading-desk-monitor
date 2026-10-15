# Trading Desk Monitor decision register

Revision 0.1, 14 October 2026. Open decisions are not implicit approvals. An implementing agent can build fixtures and measure alternatives while a financial decision is pending, but cannot release the affected behavior. Do not repeatedly ask the owner to accept ordinary reversible implementation details already covered by the specification.

## Established constraints

Positions, private trade terms, internal PnL, cash balances, funding settings and portfolio results remain inside the organization. Local Python and public-data downloads are available on the work PC. VPS reachability and corporate proxy behavior require a real connection test. The user will obtain the existing library from the Hermes VPS; no library repository or source snapshot is currently supplied.

The user wants the external server to collect and serve market/static data and host an agent that handles difficult public data cases. The existing financial library is the candidate core to reuse and extend. Code generation and development take place outside work using public/synthetic inputs.

## Decisions requiring the desk owner

| ID | Decision | Current evidence or planning position | Blocks |
|---|---|---|---|
| DEC01 | Funding period base and trade timing | Organization rules say previous business-day clean MV and calendar-day rates. Current implementation uses as-of MV and cash. Decide initial/new/reduced/closed positions, intraday cash and CBR conversion date for interest. | METHOD-01, DESK-03, pilot funding acceptance |
| DEC02 | Accrual bases and calendars | TRADING ACT/365 and DERIVS RUB/CNY 365 versus USD/EUR/CHF 360 are recorded assumptions. Confirm per product, cash/funding convention and holiday source. | METHOD-01 and affected pricing/funding |
| DEC03 | REPO cash-leg funding | Term REPO rate versus book rate remains TBD. Also decide if overlapping POS and REPO lines are intentional for the book. | Real REPO coverage |
| DEC04 | Authoritative market cuts and overrides | Current organizational mark is internal market FX around 19:00. Proposed VPS data must reproduce an accepted cut or be an explicitly secondary source. Define quote type, timezone, stale tolerance and provider precedence by series. | Real valuations, PnL and data-quality release |
| DEC05 | PnL attribution and official scope | Define risk at previous/current/frozen date; carry by yield versus coupon cash flows; rolldown; spread moves; cross terms; new/closed/round-trip trades; fees; source of truth for position versus book totals. Official PnL includes funding. | DESK-03 and reconciliation acceptance |
| DEC06 | OCP transfer convention | Actual transfer deals are internal inputs. Rate assumed to be CBR established for tomorrow and value date T+1; confirm exceptions and signed leg treatment. | Real OCP acceptance |
| DEC07 | Recovery and JTD sign | Recovery default 0 for bond JTD is recorded; CDS quote-to-upfront recovery 25 percent has a separate purpose. Confirm signed PV change versus positive loss magnitude, dirty/clean PV and subordination exceptions. | JTD coverage |
| DEC08 | Numerical conventions and tolerances | Choose derivatives versus one-sided PV-change interpretation for DV01/CS01; shock and bootstrap semantics; units; model-specific price/risk tolerances and material-shock acceptance. Independent reviewer proposes values from evidence. | CORE-02/03 acceptance |
| DEC09 | Pilot universe and release coverage | Proposed R1 is one bond book plus synthetic HTM and DERIVS cash examples. Specify actual instrument classes, expected scale and priority currencies. No private holding list is required outside work. | Pilot scope and final coverage claims |
| DEC10 | Open special-product requirements | Inflation factors, extension risk and advanced MBS/ABS need a separate approved contract or explicit deferral. A single generic BOND label is insufficient. | Only those products |
| DEC11 | Historical corrections and as-known replay | Proposed append-only source/term/rule revisions; new valuation run on corrected inputs; separately revisioned official/scenario attachments. Decide restatement policy and official history display. | Historical replay acceptance |
| DEC12 | Alerts and carry presets | Existing PnL-break thresholds, OCP threshold and carry policy presets are provisional. Use accepted configurable values or informational status; do not claim approved risk limits. | Operational alert interpretation |

## Proposed engineering defaults

| ID | Default | Validation before adoption |
|---|---|---|
| ADR01 | Existing private repository is the control center; preserve tdm/prototypes paths. Keep the financial library a pinned reusable package. | Confirm library ownership and whether it already has a repository. |
| ADR02 | One Linux VPS, PostgreSQL, raw immutable disk archive, FastAPI, Nginx, deterministic systemd jobs, restricted Hermes execution. No GPU for remote-model usage. | Operator confirms supported host image, resources, backup target, domain, budget and administration route. |
| ADR03 | External versioned bulk snapshots; work client downloads a broad universe. Private overrides apply locally. | Source rights, universe size, bandwidth and HTTPS access from work. |
| ADR04 | Named human accounts with mature password/session auth; scoped revocable machine tokens; reader/curator/admin roles. | Select a maintained implementation, redaction policy and token/identity lifecycle. Do not implement custom cryptography. |
| ADR05 | Runtime Hermes submits corrections and uses allowlisted typed recovery actions. Normal collection never depends on LLM availability. | Enforced permissions, incident tests, model credentials, daily budget and escalation recipient. |
| ADR06 | Work application serves only localhost; exact scenario queue is local; external frontend assets are bundled. | Windows network inspection and offline synthetic replay. |
| ADR07 | Daily off-host encrypted backup, initial RPO 24 hours and RTO 2 hours; seven-day data soak and five-business-day pilot. | Actual restore drill and owner acceptance. |
| ADR08 | Exact versions pinned at implementation, reproducible release artifacts for Linux and Windows. | Windows dependency test before broad core refactoring. |

## Source and access inputs still needed

1. Library and current collector source export, dependency versions, tests, public sample inputs, current schedules and data provenance.
2. Actual data vendors and available API/export entitlements for bond prices, swaps/OIS/basis, FX, fixings, ratings and CDS.
3. Selected VPS provider/region, domain, secure administrator access, backup destination and an authorized spending limit.
4. The work Python version, network/proxy behavior and input file schemas. Real files stay at work; only permitted schema descriptions or invented equivalents may be used outside.
5. LLM provider credentials suitable for unattended operation, permitted source documents, cost budget and incident notification destination.

No password, token or private data belongs in this register. These inputs gate only dependent work.

## Change procedure

For each decision record status, owner, date, rationale, affected requirement IDs, chosen rule, alternatives rejected, test evidence and required contract version. Put approved changes in a reviewed PR. A coding agent does not promote a financial assumption into a confirmed convention by implementing it.
