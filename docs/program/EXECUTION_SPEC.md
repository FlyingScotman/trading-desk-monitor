# Trading Desk Monitor implementation specification

> **Superseded in part (16 October 2026).** Per [ADR-001](../architecture/adr/ADR-001-hosting-topology.md), the backend and frontend run on a **Windows server inside the work network**. The hub may be split between a Linux VPS (public collection) and the inside server, linked by immutable snapshots. The Hermes runtime agent, the work-PC-only backend and the bulk-download parts of this pack no longer apply. The current architecture is in [docs/architecture/](../architecture/README.md). `DECISIONS.md` DEC01–DEC12 remain valid.


Revision 0.1, 14 October 2026. Baseline: trading_desk_monitor commit a6ba30bf5573ada869a9594c3e69f73faf741ed2, contract 1.3. Decisions marked proposed remain defaults for planning; financial choices marked open need the desk owner before the affected feature is released.

Build an external service that collects, validates, versions, and serves market and public reference data. Use the existing financial Python library as the candidate calculation core, after audit and any necessary extensions. Run that core and all portfolio calculations locally on the Windows work PC. Preserve and extend the current frontend and tdm publication contract.

## 1 Outcome and scope

The work user supplies internal positions, private OTC terms, trades, funding settings, cash balances, and official PnL through local files or internal network paths. The local backend obtains a public or properly licensed market snapshot, combines it with internal overrides, calculates valuations, sensitivities, funding, PnL explain and scenarios, and publishes a complete tdm run. Official PnL arriving on T+1 attaches with an auditable revision.

The home environment uses the same software with public instrument examples and synthetic portfolios. The VPS never receives work portfolios or their results. Public unit analytics such as a bond Z-spread may be computed on the VPS using the same versioned core when the data rights permit; portfolio calculations stay local. Store vendor observations and calculated analytics as distinct fields with their term, curve, model and calibration versions; a computed spread must not silently replace a source observation.

The target includes all confirmed contract 1.3 capabilities. Deliver them in declared coverage waves. VaR, limits, order execution, automatic execution of OCP transfers, and a public portfolio website are excluded. Inflation linked bond factors, extension risk and complex securitization are assessed explicitly; a proposed organizational convention is not automatically an implemented requirement.

## 2 Architecture and ownership

| Component | Location | Responsibility |
|---|---|---|
| Financial core | Python package developed outside work; runs locally at work | Deterministic instrument terms, schedules, curves, PV, yield/spread inversion, risk and repricing. No network, accounts, file discovery, agent calls, or implicit global market state. |
| Market service | New VPS | Source connectors, raw archive, normalized public data, effective-dated reference records, curve snapshots, immutable bundles, coverage, authentication and audit. |
| Data curator | Restricted Hermes runtime on the new VPS | Investigate failed feeds and difficult public terms; create evidence-backed correction proposals and bounded recovery requests. |
| Desk backend | Windows work PC | Internal file adapters, private terms and configuration, market snapshot download/cache, local valuation and PnL engine, funding, reconciliation, scenario jobs and publication. |
| Monitor | Work PC localhost, existing tdm and prototypes | Display published runs and approximations, retain last successful run with clear age/coverage, and request exact scenarios only from the local worker. |
| Project control | Existing private GitHub repository | Canonical specifications, decisions, task state, test evidence, PRs, releases and dependency versions. |

The public service is an additional provider upstream of the local tdm input layer. It must not replace tdm with a remote portfolio backend. Keep the existing window.DESK contract and four screens. Make compatible optional additions where possible; use a new contract revision for changes in meaning or new factor types.

Develop against synthetic fixtures and frozen public snapshots. No coding harness on a personal machine or external server may read the work drive, work portfolio database, private trade terms, real calculation output, or internal diagnostic logs.

The work downloader requests broad published bundles by date, cut and universe. It must not send the holding list, notional, book, entity, PnL, or missing-position diagnostics to the VPS. Individual public instrument lookup remains available for public research, but the work integration does not use it to disclose the portfolio universe.

## 3 Implementation defaults

Use one new Linux VPS with a supported distribution, PostgreSQL for the external service, immutable raw files and snapshot bundles on disk, a Python FastAPI application, and Nginx for HTTPS and reverse proxying. The API and database listen privately. Only HTTPS and restricted administration access are exposed.

Use systemd services and timers for API supervision, deterministic collection jobs and monitoring. Pin application environments and maintain deployment configuration in version control. Run the persistent Hermes execution environment in a separate unprivileged container or equivalent OS sandbox, without the host container-management socket, root privileges, production secrets, or database administrator access. The temporary provisioning operator is a different role from the persistent runtime agent.

Start with one host and one database. Do not introduce Kubernetes, Kafka, a vector database or multiple servers for the initial EOD workload. Begin sizing around 4 vCPU and 8 GB RAM with expandable SSD storage, then revise from measured backfill, archive and concurrent workload. This is a planning envelope, not a sizing guarantee or a purchase instruction. A remotely hosted LLM is assumed; a local model would require a different hardware decision.

Keep the work installation lightweight: the approved Python runtime, the portable financial package, the desk backend, local SQLite, and bundled frontend assets. Linux development success does not establish Windows compatibility. Test and package the complete Windows dependency set before substantial onsite integration.

Do not copy the whole Hermes environment to the new host. Export the selected code, source definitions, public fixtures, tests and reproducible dependencies; create fresh accounts and secrets through an approved secure provisioning route.

## 4 Data responsibilities and provenance

requirements.csv maps every one of the 23 current input datasets to its supplier and execution boundary. Internal groupings, credit overrides and funding rules remain internal even when similarly named public reference data exists.

The external service stores four stages: original source response or document; normalized records; validated candidate dataset; published snapshot. Preserve the original artifact and checksum. Failed parsing and conflicting values go into quarantine and leave the last valid published snapshot intact.

Each normalized record includes instrument or series ID, source, observation time, effective date, publication or known time when available, ingestion time, units, currency, convention identity, quality state and record revision. Static terms have effective-from/to and known-from/to semantics. Corrections append revisions rather than silently replacing history. If historic terms cannot be reconstructed, mark the period unknown.

Public static coverage includes identifiers and aliases, issuer links and public ratings, issue terms, outstanding face value, coupon and principal schedules, amortization, put and call details, floater index rules and lags, day counts, calendars and event notices. Future unknown floater coupons are formula-driven projections, not invented fixed cash flows.

Market data covers bond clean prices and accrued conventions; bid/ask/last/valuation price identities; raw swap, OIS and basis quotes with instrument conventions; built curves and their construction configuration; FX marks; effective CBR rates; overnight fixings; official CBR meeting and effective dates; and CDS quotes only where an entitled source exists. Public accessibility alone does not establish useful coverage, correct cut or redistribution rights.

Select one authoritative provider and cut per series, with a documented fallback. Different marks and provider revisions may coexist but must never be spliced without labels. Store raw curve instruments as well as the resulting curve: eleven display pillars are not a sufficient archive of a pricing curve. Raw rates must distinguish par/zero/discount-factor representation, compounding, day count, maturity, projection index, collateral/discount curve, bootstrap and interpolation configuration.

For CBR FX distinguish announcement time, effective date and quote nominal. Normalize to RUB per one unit. The next rate must be the expected effective observation known at the run cut, never an arbitrary future row. For overnight rates distinguish observation date, publication lag and applicable accrual date. Weekend applicability must not conceal a missing new fixing.

A Source Registry records access method, credential owner, universe, frequency, historical range, expected delay, field coverage, permitted storage and redistribution, use in calculations, submission of documents to an LLM, and fallback. Sources supplied under an employer's credentials are not moved to a personal VPS by assumption. Confirm the actual provider list before building most collectors.

Each published snapshot has an immutable ID and manifest containing contract version, cut and timezone, source and term revisions, curve model versions, checksums, dataset counts, coverage and stale flags, and superseded snapshot ID if applicable. Atomically publish only after mandatory checks pass. Partial snapshots must identify omissions, and the local desk client rejects them when its required inputs are absent.

## 5 Financial core audit and extension

The auditor receives a frozen source snapshot and dependency manifest from the existing Hermes VPS. Inventory code ownership/licenses, entry points, tested products, mutable state, caches, file/network coupling, numerical methods and Windows dependencies. Capture its actual version and existing tests before any refactoring.

Populate every applicable calculation row in requirements.csv with verified, partial, missing, unverified or explicitly deferred status. For a verified capability cite a function, the conventions it implements, and a meaningful independent test. A function name or an agent's statement is insufficient.

Audit these groups:

- Bonds: clean/dirty PV, accrued interest, yield basis, settlement, Z/G/I/DM/ASW/OAS definitions as applicable, price/yield/spread inversion, modified and spread duration, convexity, amortization, offers, float reset lags and known coupons.
- Curves: raw quote bootstrap, discount and projection separation, RUB RUONIA / RUB KEY / OFZ roles, other currency curves, basis and FX forwarding, consistent bump semantics.
- Products: fixed and floating bonds, IRS/OIS, FX spot/forwards, XCCY, CDS, HTM, and explicit assessment of inflation, MBS/ABS and perpetual/subordinated cases.
- Risk: signed KRD at the eleven display buckets, total DV01, CS01, ofz_spr01, FX delta, JTD and recovery, diagonal and cross gamma, and the relationship between display factors and the underlying pricing curve.
- Revaluation: frozen terms and market, coherent shocks, decompositions that reconcile to total, and performance for the supported product set.

Specify a pure interface for loading terms, building a market context, valuing a trade, calculating risk, and repricing under a versioned scenario. Keep portfolio funding and organizational PnL logic outside generic bond/swap pricers.

A bond marked from a quoted market price may calibrate its own spread to reproduce that price. Specify which spread is frozen when bumping the base curve or credit factor. Recalibrating the spread after every bump can erase the intended risk and is not acceptable by accident.

Choose central derivative estimates or exact bumped PV changes explicitly. The existing contract describes DV01 as a +1 bp PV change while its sums and second-order formulas resemble a derivative convention. Test and document the accepted interpretation before assigning gamma and residual PnL.

Use independent analytical examples, round trips, no-arbitrage and sign checks, cash-flow conservation, and public or locally approved reference prices. Matching the implementation to itself is not independent verification. Test bump convergence and nonlinear scenarios; do not assume a tridiagonal Hessian for a bootstrapped curve. In-house reference comparisons may run entirely on the work PC without exporting outputs.

Extend the candidate library only where the audit identifies a requirement gap. Package a pinned release with a changelog, lockable dependencies, deterministic serialization and Linux/Windows test evidence. Keep provenance for imported code and licenses.

## 6 Public API and access controls

The first usable API supplies published read-only data. Suggested resources are:

| Resource | Purpose |
|---|---|
| /v1/snapshots | List immutable snapshots by date, cut and broad public universe. |
| /v1/snapshots/{id}/manifest | Versions, provenance, completeness, revisions and checksums. |
| /v1/snapshots/{id}/bundle | Download a stable compressed bundle for local ingestion. |
| /v1/instruments and /v1/instruments/{id} | Public terms, aliases, schedules and versions. |
| /v1/quotes, /v1/curves, /v1/fx, /v1/fixings | Public or licensed data, explicit dates and snapshot IDs. |
| /v1/coverage | Required fields, gaps, stale series and quarantined records. |
| /health/live and protected /health/ready | Process liveness and actual database/snapshot readiness. |

Generate an OpenAPI contract, typed client models, pagination and bounds. Support stable error codes, request IDs, conditional download checks, timeouts and rate limits. No portfolio upload or private pricing-job endpoint exists on the external service. Administrative actions and curator proposals use separately authorized routes.

Give every user and client an individual identity. Use revocable scoped bearer credentials for scripts and the work downloader; store only credential hashes server-side and never put tokens in URLs. Named human accounts use an established password/session implementation, secure cookies and CSRF protection for mutations. Roles are reader, curator and administrator. No open registration or shared administrator password. Record the chosen implementation in an architecture decision before coding authentication.

Audit login success/failure, authenticated downloads, token creation/revocation, source changes, correction proposals and approvals, publication, and agent recovery actions. Include time, actor, request/job ID, action, outcome, target revision and reason. Exclude passwords, tokens, authorization headers and sensitive request bodies. Readers, curators and the runtime agent cannot edit or erase the audit history. Distinguish a machine-token request from an interactive human login.

Use Nginx HTTPS and authentication before any data API is exposed publicly. Keep the database and agent control interface off the public listener. Test credential revocation, reader write denial, rate limiting and redaction. A .ru domain is a deployment choice; verify actual HTTPS access from the work PC and its corporate proxy. Never disable certificate verification to make it work.

## 7 Deterministic collection and Hermes operations

Collectors run from versioned code under ordinary scheduled jobs, with bounded retries, exponential backoff, source quotas, idempotent loads, per-source locks, timeouts and resumable checkpoints. Ingestion continues when Hermes or its LLM provider is unavailable.

Run static discovery for a broad public universe. Poll routine quote/fixing sources deterministically; rescan changed/new instruments and event notices more often than stable terms. Configure the actual timetable from source publication times and the accepted desk cut. Backfill in chunks and retain coverage reports. Fetch raw history first; versioning and reconstruction of historical terms are a separate task.

An independent watchdog checks feed timeliness, counts, numeric/structural invariants, disk space, backup success, API readiness, TLS expiry and bounded job duration. It creates a durable incident before asking Hermes to investigate. The existing BB agent or a small independent monitor on another host also checks external readiness, so complete VPS or network failure can be detected.

Hermes can read public evidence, non-secret diagnostics and quarantined source records. It may propose typed reference-data corrections, retry an approved idempotent job, or invoke a narrowly scoped recovery operation. It must not have unrestricted production shell access or raw production database writes. Enforce restrictions through OS/database permissions and typed operations, not prompts alone.

The incident workflow is detected, investigated, proposed, validated, approved when required, published, verified and closed. Attach evidence and old/new values. Routine deterministic source updates may publish after validation; ambiguous cash flows, offer/call interpretation, coupon formulas, and changes to pricing conventions require desk-owner acceptance before affecting prices.

Allow bounded autonomous retries for recognized failures. Changes to pricing code, source hierarchy, database schema, authentication, data rights or deployment go through review and release. Give the agent a finite per-incident attempt limit and daily cost budget; when exhausted, keep valid data and escalate. Unknown LLM credentials or budgets leave that workflow disabled while normal collectors continue.

Use a sandbox for downloaded documents, treat their text as untrusted evidence, and give each tool the minimum source credentials. Do not forward all environment variables to agent tools. Pin the Hermes version and record the install source and configuration. Test loss of the LLM provider, document prompt injection, repeated failures and restart storms.

## 8 Local Windows backend

Use declarative adapters for approved CSV/Excel exports and internal paths. Provide a local setup and mapping screen or guided utility: select columns, confirm units and dates, preview validation, and save private configuration without editing Python. Keep diagnostics locally readable and export no work logs. The schema map specifies columns, units, encodings, date formats, keys, quantity versus face amount, and clean versus dirty values. Archive source hashes locally, validate complete book snapshots including an explicit empty-book manifest, and never execute workbook macros.

Private OTC data includes full leg economics, calendars, schedules, resets, payment conventions, collateral and notional exchanges where relevant. The current compact otc_deals CSV may remain a monitor export, but cannot be treated as a complete universal pricing payload.

Download a broad external snapshot with a read-only client token, validate its manifest, and cache it locally before calculation. Merge internal overrides under explicit precedence and retain both values and the selected reason. Preserve the current internal ~19:00 FX mark as authoritative until DEC04 changes it.

Build a private immutable calculation input bundle containing current/previous positions, trades and cash flows, internal term/rule revisions, selected public snapshot, market cut, model version, configuration, calendar and scenario definitions. Bind the run to input and scenario hashes. A corrected snapshot or configuration produces a new run.

Calculate PV and risks with the financial core. Apply organizational funding and PnL rules once, then publish through the existing tdm SDK. Reuse or refactor tdm funding/date logic rather than independently maintaining two hidden versions. Explicitly supplied funding lines must prevent a second derivation.

For PnL define beginning-of-period holdings and risk date, calendar-day carry, settlements, coupons, amortizations, trade PnL, partial reductions, closures, and official reconciliation. Include positions closed by EOD and intraday round trips when the trade feed allows; never silently drop their PnL. Freeze the attribution order for rates, own spread, FX, interactions, funding and other effects. Explained plus residual must reconcile to the chosen official scope.

Implement TRADING funding, HTM accrual and risk exclusion, DERIVS cash funding, and REPO according to accepted decisions. Distinguish market and CBR bases; label estimates pending official T+1 data. Preserve official components and book totals separately. Amendments produce audited attachment revisions with deterministic history and cache updates.

OCP transfer trades are real internal inputs. Import and reconcile both legs, amount, direction, rate and value date. Calculate summaries and residual alerts locally. Do not fabricate executed transfer trades to force the book to look neutral.

Keep interactive browser approximations for current UI compatibility. Run exact preset and arbitrary scenario revaluation in a local worker, using the already designed scenario_job concept. A job pins base run, market, model and definition hash; has queued/running/done/failed states, cancellation and timeout; never sends its payload to the external service.

Treat existing carry scenarios as an explicit frozen-portfolio approximation: current MV/FX, maturity/offer exits, simplified resets and optional rolldown. Port or independently test the formulas in Python against shared public fixtures. Exact cash-flow forecasts, lag-aware floaters and changing MV are separate capabilities, not implied by matching the prototype.

Operational screens show source, cut, age, model, coverage and approximation state. Missing required data blocks publication or produces an explicitly incomplete run that cannot masquerade as complete risk. Demo mode requires explicit selection. Compare totals across all four views under the same book/entity/currency filters and funding inclusion.

## 9 Delivery sequence

| Stage | Work and dependency | Exit evidence |
|---|---|---|
| 0 Establish control | Import this pack into docs/program, export the current library/collectors, pin versions, choose pilot and public sources, resolve initial methodology decisions. | Export manifest, source registry, decision owners and populated audit inputs. |
| 1 Audit and freeze contracts | Audit the entire library requirement matrix; define public snapshot and private calculation interfaces, numerical conventions, Windows dependencies and release waves. | Function-level gap report, reproducible existing tests, agreed schemas and benchmark plan. |
| 2 Provision foundation | Purchase/configure the selected VPS through the authorized operator. Establish restricted accounts, HTTPS, private database, reproducible services and fresh secrets. | Rebuildable infrastructure and tested access. No market-data agent needs root. |
| 3 Deliver one complete path | Collect one public bond, its full terms and one required curve plus FX/fixings; publish an authenticated snapshot; run a synthetic local portfolio through the candidate core and current UI. | First end-to-end evidence, pinned input snapshot and field lineage. |
| 4 Broaden data and library | Expand collectors, static curation, history and identified library gaps. Work in parallel against the frozen interfaces. | Coverage reports, independent financial tests, Windows package. |
| 5 Deliver desk workflows | Add private file maps, funding/FX/HTM/OCP, T+1 official reconciliation, scenarios and frontend consistency. | Synthetic two-day and T+1 replay across all four screens. |
| 6 Enable bounded Hermes | Attach the exception queue and approved actions after deterministic ingestion and recovery paths exist. | Failure drills, cost limits, evidence/approval trail; collector operation without LLM. |
| 7 Pilot inside perimeter | Configure real file paths and mappings locally; compare trusted internal numbers and inspect all mismatches internally. | Owner acceptance of supported coverage and differences; no private evidence leaves work. |
| 8 Release and operate | Restore test, rollback test, soak period, signed/versioned Windows package and service deployment. | Release manifest, runbooks, accepted open limitations and operational handover. |

Library audit and minimum API/data contracts come before broad implementation. VPS provisioning can run alongside the audit after host choices are accepted. Data and local-backend teams can develop concurrently using public/synthetic snapshots; production integration waits for tested contracts. Install persistent Hermes after the ordinary exception workflow works.

## 10 Agent assignments and project control

Use a coordinator to own backlog state and dependencies; a quant implementer for the library; a data/API implementer for public data; a desk implementer for the Windows integration; a deployment operator such as the existing BB/Hermes agent; and an independent reviewer. One person or harness may fill several roles sequentially, but numerical validation must include independent evidence.

The runtime Hermes agent is not the overall project manager. The coordinator reads committed specifications and task evidence, assigns ready work, and produces a brief summary of completed evidence, current blockers and next ready tasks. Agent memory and chat transcripts are not the task database.

Keep this planning pack in docs/program. Add versioned schemas under contract/market and contract/private, service code under services/market_data, local integration under desk_backend, and infrastructure under infra. Preserve existing tdm and prototype paths initially. Keep the financial package in its existing repository if one exists; otherwise import its cleaned source with provenance into a dedicated package. Pin its release in the control repository instead of copying diverging versions to servers.

A task is one bounded change with an ID, owner, dependencies, allowed paths, deliverables, acceptance tests, reviewer and evidence. Its lifecycle is todo, in_progress, review, done; blocked requires a concrete reason and next action. Done requires a reviewed commit or artifact plus evidence for every acceptance item. Test counts or narrative confidence alone are insufficient.

The coordinator mirrors tasks to GitHub issues if useful, preserving IDs. PRs reference requirement and task IDs and any decision they implement. An architecture or financial-convention change updates DECISIONS.md and the appropriate contract in the same reviewed change. Compatibility-impacting changes get a new version and explicit migration tests.

Do not claim a task is complete because an agent attempted a command or a service returned HTTP 200. Record actual outputs, checksums, reviewed code and observations. Private pilot results are retained only at work; the control repository records a non-sensitive acceptance attestation or status when permitted, without numbers, IDs or logs.

All shell commands presented to the owner must explain every command, subcommand, argument, flag, operator and non-obvious syntax. This rule applies to implementing agents and runbooks. Avoid shell snippets in this pack until there is an actual deployment environment.

## 11 Acceptance and release gates

- Numerical: analytical and independent examples; curve reprice errors; PV/yield/spread round trips; sensitivity sign/scaling, total versus KRD, bump-size convergence, FX interactions, JTD direction and recovery; instrument-specific material-shock tests.
- Time and accounting: Friday-to-Monday accrual with a rate effective on Monday; actual CBR effective dates and publication availability; holidays, leap years, new/closed/round-trip trades, coupons/amortization, OCP transfers, HTM and funding counted exactly once.
- Data: missing versus zero, stale versus unchanged, duplicate/corrected source records, changed static schedules, incomplete history windows, conflicting providers and no historical look-ahead.
- API: reader/curator/admin authorization, revocation, rejected anonymous access, pagination/bounds, rate limits, checksum verification and complete audit trails without credential leakage.
- Boundary: network capture during synthetic desktop replay shows only the permitted broad-data download to the VPS; pricing and UI work offline. No private input, result, instrument request list, telemetry or error upload leaves the desktop.
- Operations: recover the external database, raw archive, published snapshots and configuration onto a clean environment. Proposed initial goals are recovery point no worse than 24 hours and recovery time within 2 hours; verify and revise with the owner.
- Compatibility: existing 1.3 fixtures and migrations remain usable; equivalent metrics agree across all screens; incomplete results cannot claim full coverage.
- Reliability: seven calendar days of VPS ingestion with controlled failure drills and five business days of local pilot. These gates may overlap; synthetic weekend/rate-change tests still run even if no real event occurs during the pilot.

Provisional performance targets for supported vanilla products are PV plus first-order risk for 1,000 bonds and 100 swaps within five minutes, EOD including selected gamma within fifteen minutes, and a single full scenario within thirty seconds for 1,000 supported positions. The audit measures actual hardware and algorithms and revises these targets before they become release commitments. Validate exact scenario responsiveness independently from daily batch runtime.

Use encrypted backups outside the VPS, with keys protected separately, and an exercised rollback to the prior code release and data snapshot. Restore evidence matters more than the existence of a backup job.

## 12 Delivery waves and limitations

R0 proves one public bond and market snapshot through authenticated service, portable core, local tdm publication and frontend.

R1 proves a selected real bond book with the correct organizational funding, FX and PnL basis; a synthetic HTM and derivative cash example; previous-day and T+1 replay; and declared risk coverage. Unsupported real positions prevent claiming complete book coverage.

R2 covers the remainder of confirmed contract 1.3: supported floaters, IRS/OIS, FX forwards, XCCY/CDS where required, wider gamma, historical and exact scenarios, and tested carry approximations. Prioritize using the actual library audit and desk instrument mix.

R3 contains owner-approved special products or new factor contracts, such as inflation risk, complex MBS/ABS and extension risk. Any deferral is explicit per requirement and release; it is not permission to substitute zero sensitivities or an undisclosed proxy.

The project is complete only when every target requirement is verified or explicitly accepted as deferred, supported real inputs have an internal acceptance record, and recovery and operational ownership are established.

## 13 Reference documentation

The current repository baseline and reconciliation details are linked in BASELINE_REVIEW.md.

Use the official [Hermes installation and quickstart documentation](https://hermes-agent.nousresearch.com/docs/getting-started/quickstart) and [security documentation](https://hermes-agent.nousresearch.com/docs/user-guide/security) when provisioning. Hermes supplies scheduling and isolated execution options, but the project still requires independent ingestion and permission boundaries.

Use the [FastAPI security documentation](https://fastapi.tiangolo.com/tutorial/security/), [Nginx authentication documentation](https://docs.nginx.com/nginx/admin-guide/security-controls/configuring-http-basic-authentication/), [Nginx logging documentation](https://docs.nginx.com/nginx/admin-guide/monitoring/logging/), and [PostgreSQL backup documentation](https://www.postgresql.org/docs/current/backup-dump.html) for implementation details. Choose and pin exact supported versions at implementation.

[MOEX ISS](https://www.moex.com/a8531) is a candidate public connector. Its [market-data policy](https://www.moex.com/en/datapolicy/) distinguishes distribution and non-display use; record the actual rights and data coverage rather than inferring them from an accessible API.
