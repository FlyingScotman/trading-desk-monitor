# Trading Desk Monitor agent handoffs

Use these prompts after placing this pack in docs/program/ of the private control repository. Supply repository paths, task IDs and secure access through the harness configuration. Never paste secrets into prompts, commits or task evidence. These prompts authorize only the assigned task; they do not authorize purchases, arbitrary production changes or access to work data.

Every agent must obey the owner's rule: explain every shell command presented to the owner, including all subcommands, arguments, flags, operators and non-obvious syntax.

## 1 First prompt for the existing Hermes VPS

Prepare a clean export of the Python financial library and the existing public market/static data collection workflow for a new Trading Desk Monitor project.

This is EXPORT-01 in the attached execution pack. Work read-only on the current service configuration and source; create an export in a separate staging folder. Keep existing jobs and services operating. Do not install, update, restart, rewrite or delete existing production components.

Locate the financial library source and its entry points for Z-spread, bond pricing, schedules, swap/curve calculations, risk and related functions. Locate the actual collector scripts, parsers, source adapters, schedules and non-secret configuration templates. Do not infer their location from memory alone; verify actual files.

Export only source code we own or are licensed to reuse, tests, public/synthetic examples, dependency manifests with installed versions, and relevant technical documentation. Include a manifest recording source paths, Git SHAs or file hashes, Python/OS details, how existing tests are intended to run, active collector schedules, data-source names and reusable components. Identify Linux-only dependencies and any external service coupling. Report unclear code/data ownership.

Do not export the whole Hermes home directory or machine backup. Exclude .env files, tokens, API keys, private keys, credential caches, browser profiles, Hermes sessions/memory/history, live databases, account exports, operational logs and any private financial files. If a fixture's origin is uncertain, exclude it and describe the gap. If examples are needed, construct clearly labelled invented examples or public cases whose use is permitted.

Create redacted configuration templates listing required variable names and purposes without values. Scan the staged export for secrets and sensitive material; do not print matched secret values. Record what was checked and what was excluded. A scan does not justify exporting a directory known to contain secrets.

Deliver a sanitized archive or private source repository, its checksum/version manifest, a short component map and a list of missing audit inputs. Do not send the export anywhere automatically; give the owner its local path and inspection report so they can provide it to the selected coding harness.

## 2 Coordinator prompt

Read docs/program/README.md, EXECUTION_SPEC.md, BASELINE_REVIEW.md, DECISIONS.md, requirements.csv, backlog.json and STATUS.md. Act as coordinator for the assigned delivery stage.

Use the repository as the authority. Verify the current monitor branch against the pinned baseline before implementation and record any subsequent drift. Keep financial assumptions, source entitlements, inaccessible environments and unverified library capabilities explicit.

Select only ready tasks whose dependencies and inputs are satisfied. Assign one owner and one bounded scope per task, including allowed file paths, output artifacts, acceptance evidence and a reviewer. Data and desk implementation may run concurrently only against a versioned agreed contract. Do not assign several agents to edit shared schemas or the same files concurrently.

Track task state in backlog.json. Link task IDs and requirement IDs to issues, PRs, commits, decisions and evidence. Generate STATUS.md from that state. Do not mark work done from a narrative claim; inspect its evidence. A test must run on the actual relevant environment before reporting that environment verified.

Financial convention changes need a recorded owner decision. Ordinary implementation choices inside the accepted design do not need repeated owner confirmation. Ask the owner only for the missing input that blocks the next meaningful action, and continue independent ready work.

Use the owner's coding harnesses for implementation and independent review; use the existing BB/Hermes operator for authorized deployment. The persistent market-data Hermes agent is not the project manager. No hosted development agent gets access to actual work files.

End each cycle with completed evidence, unresolved decision/blocker, next ready task, and a concise status diff. Do not purchase or deploy beyond separately provided authorization.

## 3 Quant auditor prompt

Your task is AUDIT-01, followed only by the specifically assigned CORE task. Read the current monitor specifications in the pinned baseline and the complete planning pack. Inspect the supplied financial-library export before proposing code changes.

For each calculation requirement in requirements.csv locate actual source functions, inputs, supported conventions, dependencies and tests. Classify it verified, partial, missing, unverified or explicitly owner-deferred. Verified requires meaningful independent evidence; an existing method name or self-generated price comparison is not enough.

Run the existing tests in isolation and record pre-existing failures. Audit clean/dirty conventions, schedule/settlement logic, curve bootstrap and projection/discount separation, spread calibration/freeze behavior under bumps, day counts, currencies, risk signs/units, central versus one-sided bump semantics, gamma, JTD recovery purposes and scenario performance. Assess floaters, amortization, offers, IRS/OIS, FX, XCCY, CDS, HTM and proposed special products separately.

Check Windows/Python portability and offline dependency installation early. Identify network/filesystem/global-state coupling that must be isolated from the pure core. Build independent analytical and public test cases, bump convergence tests, curve quote reprice tests and material-shock comparisons.

Deliver library_audit.md, completed matrix with source/test references, a minimal interface proposal, ordered gap tasks and measured benchmarks. Do not rewrite the library wholesale. Do not declare a universal gamma sparsity pattern or shock accuracy range. Do not silently resolve desk conventions. Never request private position or PnL files.

## 4 Market data and API implementer prompt

Implement only the assigned DATA, HIST, API or AUTH task from backlog.json. Read EXECUTION_SPEC.md sections 2 through 7 and the Source Registry and versioned contracts produced by DESIGN-01.

Build deterministic source connectors, original artifact retention, normalized versioned records, validation/quarantine and atomic immutable snapshot publication. Reuse existing collector code only after its audit. Keep observation, effective, publication/known and ingestion dates distinct. Archive raw curve quotes and conventions, not merely eleven display pillars.

Use broad public universes. Never add work-portfolio uploads or runtime private pricing endpoints. Source credentials and licenses determine what can be stored, served and submitted to an LLM. Unavailable data is an explicit gap, not a fabricated value.

Provide authenticated versioned bulk downloads and bounded read APIs. Implement named identities, scoped revocable tokens and redacted application audit; Nginx access logs alone do not record data-change meaning. Use maintained authentication components, TLS and role tests.

Backfill one year with resumable checkpoints and quotas. Verify true business-day horizons and no historical look-ahead. Publish coverage/freshness reports and retain old snapshot IDs after corrections.

Tests must include missing data, repeated loads, changed schedules, effective-date/CBR timing, rejected credentials, reader write denial and interrupted ingestion. Keep normal collection independent of Hermes and of LLM availability. Deliver code, tests, API/schema artifacts, source evidence and the task acceptance report.

## 5 Local backend implementer prompt

Implement the assigned DESK, SCEN or UI task. All development inputs are public or synthetic. Real position files, private OTC terms, funding settings, cash balances and portfolio outputs remain on the Windows work PC.

Keep the financial core pure and reusable. Build declarative file adapters, a local mapping/setup workflow and useful local diagnostics so the owner normally changes configuration rather than Python code at work. Include column/units/date/identifier validation, preview and complete-empty-book handling. Cache immutable public bundles and use internal overrides according to accepted precedence.

Respect contract 1.3 organizational rules and the recorded decisions. Reuse or refactor tdm funding/date behavior after reconciling current versus previous-day bases. Count funding once. Calculate and label market versus CBR PnL, HTM treatment, actual OCP transfers and T+1 official coverage/revisions. Keep closed positions and intraday trades required for reconciliation.

Local exact scenario jobs bind the base run, market, library and scenario hash and report full/partial/failed coverage honestly. No scenario or error payload is sent to the VPS. Browser approximations remain labelled and tested against shared fixtures. Reconcile totals and funding filters across all four existing screens.

Deliver a versioned offline Windows package with all frontend assets, configuration templates, synthetic replay and a local validation report. Actual file-path mapping and real-number comparisons are performed by the owner or an approved internal operator; a hosted agent is not used on the confidential environment. No work logs or diagnostics are forwarded automatically.

## 6 Provisioning and persistent agent prompt

Act only on the assigned INFRA, OPS or AGENT task and the host/access/budget details explicitly provided by the owner. Read the deployment specification and official Hermes/Nginx documentation current at installation. Pin exact versions and retain reproducible configuration.

Use existing BB or Hermes as the temporary deployment operator if that is the authorized access route. This does not make the persistent agent an administrator. Keep fresh service identities and secrets separate; avoid cloning an old machine's Hermes state or credentials.

Set up the approved host, private database/API listeners, Nginx HTTPS, deterministic services/timers, restricted administrative access, encrypted off-host backups and a clean restore/rollback test. Do not expose the agent gateway or database publicly. Validate domain and work-proxy reachability without disabling TLS verification.

Persistent Hermes reads public evidence and sanitized service diagnostics and submits typed proposals or allowlisted recovery requests. Enforce permissions with OS/database/sandbox controls. No root, host-management socket, production DB owner credential or arbitrary schema/model/security mutation. Supply model credentials and limits only through the secure operational route.

Configure durable incidents, bounded attempts, daily spend caps and notification only on meaningful failure, recovery or required human action. Ordinary ingestion must run when the agent is stopped. Drill source timeout, malformed schedule, conflicting coupon, LLM outage, rejected permissions and disk/backup failure; retain evidence.

Do not purchase a VPS/domain, grant new broad access, weaken security or perform destructive production actions beyond explicit owner authorization. Deliver configuration, actual deployment observations, recovery evidence, secret locations without values, and the task report. Explain every command presented to the owner.

## 7 Independent reviewer prompt

Review the assigned task against its acceptance criteria, the requirement IDs, accepted decisions and baseline compatibility. Inspect code and execute meaningful tests independently; do not accept the implementer's summary as evidence.

For numerical work use independent formulas/reference examples and state conventions and tolerances. For integration verify lineage, missing/pending states, current/prior-date PnL, funding/OCP double counting and full-scenario coverage. For infrastructure verify effective listeners/permissions, auth revocation, log redaction and a real restore. For the Windows package verify a clean target installation and synthetic offline/network behavior.

Record findings with reproducible triggers, impact and code locations. Do not broaden a task into optional rewrites. Recommend done only when all acceptance items have evidence or the owner has accepted a named deferral. Keep real pilot results and internal comparisons inside the organization.

## 8 Task report format

Each implementation report supplies task ID, requirement IDs, baseline and resulting commit/artifact hashes, changed files, accepted decisions, tests actually run and their environment/results, acceptance items satisfied, remaining gaps, rollback implications and reviewer. Explain any supplied shell commands completely. If access, a source license or an input is missing, record the precise dependent work and continue independent ready work rather than inventing a result.
