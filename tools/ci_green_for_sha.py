#!/usr/bin/env python3
"""TDM#678: is there a green run of the workflow ``ci`` on this exact commit? (release.yml: then its tests are not run twice).

The release runs the same checks that ``ci`` runs on every push to main: the backend tests (the jobs ``test`` and
``test-serial``), the other seeds of the pool's failure property (``parallel-property``) and the root tests (``test``).
When ``ci`` has already passed on the very commit that is released, a second run of them proves nothing new, and costs the
release most of an hour. This tool says which it is, from the GitHub API (``gh api`` with the workflow's ``GITHUB_TOKEN``); it never fails the release:

* a completed run of ``ci`` on this commit that ended ``success`` and whose jobs ``test``, ``test-serial`` and
  ``parallel-property`` all ended ``success`` -> ``skip=true``;
* a run of ``ci`` on this commit that has not finished -> it is waited for (``--wait-minutes``; the release has built the
  bundle on Windows meanwhile, and the run of the push to main started at the same time);
* anything else (no run, a failed one, only a run of a pull request, the API does not answer, a job missing) ->
  ``skip=false``: the release runs everything, as before. The reason is said in one line.

Only a run of the event ``push`` counts. A run of ``pull_request`` has the head commit of the pull request in
``head_sha`` but tests the merge of that head into the base branch of the moment, not this commit's tree; the merge commit
of the pull request in main is a new commit that only the push to main tests.

Prints one line (and a ``::notice::`` annotation) and, when ``$GITHUB_OUTPUT`` is set, ``skip=true|false`` and ``reason=...``.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

WORKFLOW = "ci.yml"
REQUIRED_JOBS = ("test", "test-serial", "parallel-property")
"""The jobs of ci.yml whose tests the release would run again: «test» (lint, the backend tests that can run in parallel, the
root tests), «test-serial» (the backend tests marked ``serial``, with the default seeds of the pool's failure property) and
«parallel-property» (the other seeds of the property, ``-m full_property``)."""
EVENTS = ("push",)


def gh_api(path: str) -> Any:
    out = subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True, timeout=120).stdout
    return json.loads(out)


def runs_of(fetch: Callable[[str], Any], repo: str, sha: str, workflow: str) -> list[dict[str, Any]]:
    doc = fetch(f"repos/{repo}/actions/workflows/{workflow}/runs?head_sha={sha}&per_page=100")
    return sorted((r for r in doc.get("workflow_runs", []) if r.get("head_sha") == sha),
                  key=lambda r: r.get("run_number", 0), reverse=True)


def jobs_green(fetch: Callable[[str], Any], repo: str, run_id: int, required: tuple[str, ...]) -> str | None:
    """None when every required job ended ``success`` in the latest attempt of the run, else what is wrong."""
    doc = fetch(f"repos/{repo}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
    by_name = {j.get("name"): j.get("conclusion") for j in doc.get("jobs", [])}
    bad = [f"{n}: {by_name.get(n, 'missing')}" for n in required if by_name.get(n) != "success"]
    return "; ".join(bad) if bad else None


def decide(fetch: Callable[[str], Any], repo: str, sha: str, *, workflow: str = WORKFLOW,
           required: tuple[str, ...] = REQUIRED_JOBS, wait_s: float = 0, poll_s: float = 30,
           sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
           server: str = "https://github.com") -> tuple[bool, str]:
    """(skip the tests?, the line for the log)."""
    until = clock() + wait_s
    while True:
        runs = [r for r in runs_of(fetch, repo, sha, workflow) if r.get("event") in EVENTS]
        ok = [r for r in runs if r.get("status") == "completed" and r.get("conclusion") == "success"]
        not_green = None
        for r in ok:  # the newest first
            wrong = jobs_green(fetch, repo, r["id"], required)
            if wrong is None:
                url = r.get("html_url") or f"{server}/{repo}/actions/runs/{r['id']}"
                return True, (f"tests skipped: the run ci #{r.get('run_number')} ({url}) on {sha[:12]} is green, "
                              f"the jobs {', '.join(required)} included")
            not_green = not_green or f"the run ci #{r.get('run_number')} on {sha[:12]} is green but its jobs say {wrong}"
        if not_green:
            return False, f"tests run in full: {not_green}"
        pending = [r for r in runs if r.get("status") != "completed"]
        if pending and clock() < until:
            sleep(poll_s)
            continue
        if pending:
            return False, (f"tests run in full: the run ci #{pending[0].get('run_number')} on {sha[:12]} was still "
                           f"{pending[0].get('status')} after {wait_s / 60:g} min")
        if runs:
            r = runs[0]
            return False, f"tests run in full: the run ci #{r.get('run_number')} on {sha[:12]} ended {r.get('conclusion')}"
        other = runs_of(fetch, repo, sha, workflow)
        if other:
            return False, (f"tests run in full: ci has no run of a push on {sha[:12]} (only "
                           f"{', '.join(sorted({str(r.get('event')) for r in other}))}: it tests a merge, not this commit)")
        return False, f"tests run in full: ci has no run on {sha[:12]}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--sha", required=True, help="the commit that is released")
    ap.add_argument("--workflow", default=WORKFLOW)
    ap.add_argument("--wait-minutes", type=float, default=0, help="how long to wait for a run that has not finished")
    ap.add_argument("--poll-seconds", type=float, default=30)
    a = ap.parse_args(argv)
    try:
        skip, line = decide(gh_api, a.repo, a.sha, workflow=a.workflow, wait_s=a.wait_minutes * 60, poll_s=a.poll_seconds,
                            server=os.environ.get("GITHUB_SERVER_URL", "https://github.com"))
    except Exception as e:  # noqa: BLE001 - the API does not answer, no gh, a document of another shape: the safe side is the full run
        detail = str(e)[:200]
        if isinstance(e, subprocess.CalledProcessError) and (err := (e.stderr or "").strip()):
            detail = f"{detail}; {err[:200]}"  # what gh said (no token, no login...): the reason the check did not answer
        skip, line = False, f"tests run in full: the check of the ci run failed ({type(e).__name__}: {detail})"
    print(line, flush=True)
    print(f"::notice title=release tests::{line}", flush=True)
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as f:
            # one line: a line break in the value would end it and start another «name=value» of the output file
            f.write(f"skip={'true' if skip else 'false'}\nreason={' '.join(line.split())}\n")
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"{line}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
