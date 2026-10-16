# GitHub project ownership — 15 October 2026

| Local project | GitHub repository | Account |
|---|---|---|
| rub-fixed-income-core | https://github.com/FiveLimaGolf/rub-fixed-income-core | FiveLimaGolf |
| trading_desk_monitor | https://github.com/FlyingScotman/trading_desk_monitor | FlyingScotman |
| desk_monitor_backend | The same trading_desk_monitor repository, docs/program/ | FlyingScotman |
| pnl_tool | None: local-only files, no Git repository | Not applicable |

Core remains in FiveLimaGolf for now. At the end of this development phase, the owner intends to transfer or copy core into FlyingScotman. That future operation has not been performed or scheduled.

## Local working folders

The existing trading_desk_monitor checkout keeps its current branch and files. The sibling desk_monitor_backend directory is a linked worktree on codex/backend-project-docs-20261015, displaying only docs/program/. It shares Git history and origin with trading_desk_monitor; this is not a separate GitHub repository. Its local archive and migration manifest are excluded from Git.

PNL and its report profiler stay in pnl_tool as ordinary local files. Do not create a repository for them or publish their data. Their temporary local Git metadata from initial setup was backed up outside the project and removed from its working folder.

## Git and GitHub CLI authentication

On this Mac, core Git uses the FiveLimaGolf account and TDM Git uses FlyingScotman. Credentials remain in the system credential store used by gh. A local credential helper selects the named account independently of the globally active gh account; it passes credentials only between local processes and does not save them in project files. Git author name/email are configured per repository.

For GitHub CLI operations, `gh fs` selects FlyingScotman and `gh flg` selects FiveLimaGolf for that invocation; append the usual gh subcommand and its arguments. Here `gh` is GitHub CLI; `fs` and `flg` are local account aliases. They do not change the global active account. Ordinary `gh` uses FlyingScotman by default. A new computer needs its own authentication setup.

The unrelated pi_test folder is outside this setup.

## Update — 16 October 2026

trading_desk_monitor is also pushed to the private repository https://github.com/FiveLimaGolf/trading-desk-monitor (git remote `flg`; default branch `claude/clever-ramanujan-s4k3ed`). `origin` still points to FlyingScotman. Which account is canonical is open question Q5 in [../architecture/ROADMAP.md](../architecture/ROADMAP.md).

For pushes to `flg`, the remote URL carries the `FiveLimaGolf` username, and the local repository config has a second credential helper entry for the FiveLimaGolf account. The FlyingScotman helper declines requests for other usernames, so git then falls through to the FiveLimaGolf helper.
