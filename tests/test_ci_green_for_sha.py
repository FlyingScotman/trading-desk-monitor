"""TDM#678: tools/ci_green_for_sha.py decides whether release.yml may skip the tests (a green run of ci on the same commit).
The API is a fake; what matters is the safe side: anything but a green push run with both jobs green is a full run."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("ci_green_for_sha", Path(__file__).parents[1] / "tools" / "ci_green_for_sha.py")
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)

SHA = "a" * 40
REPO = "o/r"


def run(n, *, event="push", status="completed", conclusion="success", sha=SHA):
    return {"id": 1000 + n, "run_number": n, "event": event, "status": status, "conclusion": conclusion, "head_sha": sha,
            "html_url": f"https://github.com/o/r/actions/runs/{1000 + n}"}


def jobs(**by):
    base = {"test": "success", "test-serial": "success", "parallel-property": "success", "js": "success", "windows-gate": "success"}
    base.update(by)
    return {"jobs": [{"name": k, "conclusion": v} for k, v in base.items() if v is not None]}


class Api:
    def __init__(self, runs, job_docs=None):
        self.runs, self.job_docs, self.calls = runs, job_docs or {}, []

    def __call__(self, path):
        self.calls.append(path)
        if "/runs?head_sha=" in path:
            assert f"head_sha={SHA}" in path and "/workflows/ci.yml/" in path
            return {"workflow_runs": self.runs}
        run_id = int(path.split("/runs/")[1].split("/")[0])
        return self.job_docs.get(run_id, jobs())


def decide(api, **kw):
    return tool.decide(api, REPO, SHA, **kw)


def test_a_green_push_run_with_both_jobs_green_skips_the_tests():
    skip, line = decide(Api([run(7)]))
    assert skip is True
    assert line.startswith("tests skipped: the run ci #7 (https://github.com/o/r/actions/runs/1007)")
    assert "\n" not in line


def test_the_newest_green_run_is_taken_and_a_failed_newer_one_does_not_matter_when_an_older_is_green():
    skip, line = decide(Api([run(8, conclusion="failure"), run(7)]))
    assert skip is True and "#7" in line


def test_no_run_means_the_full_run():
    skip, line = decide(Api([]))
    assert skip is False and line.startswith("tests run in full:") and "no run" in line


def test_a_failed_run_means_the_full_run():
    skip, line = decide(Api([run(7, conclusion="failure")]))
    assert skip is False and "ended failure" in line


def test_a_run_of_a_pull_request_does_not_count_it_tests_a_merge_not_this_commit():
    skip, line = decide(Api([run(7, event="pull_request")]))
    assert skip is False and "pull_request" in line and "merge" in line


def test_a_run_of_another_commit_is_ignored():
    skip, _ = decide(Api([run(7, sha="b" * 40)]))
    assert skip is False


@pytest.mark.parametrize("bad", [{"test": "failure"}, {"test-serial": "cancelled"}, {"parallel-property": "skipped"}, {"parallel-property": None}, {"test": None}])
def test_a_green_run_whose_required_job_is_not_green_or_missing_means_the_full_run(bad):
    skip, line = decide(Api([run(7)], {1007: jobs(**bad)}))
    assert skip is False and "its jobs say" in line


def test_a_run_in_progress_is_waited_for_and_then_counts():
    runs = [run(7, status="in_progress", conclusion=None)]
    api, now, slept = Api(runs), [0.0], []

    def sleep(s):
        slept.append(s)
        now[0] += s
        if len(slept) == 3:  # the run finishes
            runs[0] = run(7)

    skip, line = decide(api, wait_s=600, poll_s=30, sleep=sleep, clock=lambda: now[0])
    assert skip is True and slept == [30, 30, 30] and "#7" in line


def test_a_run_that_does_not_finish_in_time_means_the_full_run():
    now = [0.0]

    def sleep(s):
        now[0] += s

    skip, line = decide(Api([run(7, status="queued", conclusion=None)]), wait_s=90, poll_s=30, sleep=sleep,
                        clock=lambda: now[0])
    assert skip is False and "still queued after 1.5 min" in line


def test_no_waiting_by_default():
    skip, line = decide(Api([run(7, status="in_progress", conclusion=None)]), sleep=lambda s: pytest.fail("slept"))
    assert skip is False and "still in_progress" in line


def test_main_never_fails_and_writes_the_outputs(tmp_path, monkeypatch, capsys):
    out, summary = tmp_path / "out", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    def boom(path):
        raise OSError("api down")

    monkeypatch.setattr(tool, "gh_api", boom)
    assert tool.main(["--repo", REPO, "--sha", SHA]) == 0
    text = capsys.readouterr().out
    assert "tests run in full: the check of the ci run failed (OSError: api down)" in text
    assert out.read_text().startswith("skip=false\nreason=tests run in full:")
    assert "tests run in full" in summary.read_text()

    out.write_text("")
    monkeypatch.setattr(tool, "gh_api", Api([run(7)]))
    assert tool.main(["--repo", REPO, "--sha", SHA]) == 0
    assert out.read_text().startswith("skip=true\nreason=tests skipped:")


def test_a_failed_gh_says_what_it_printed_and_the_output_stays_one_line(tmp_path, monkeypatch, capsys):
    import subprocess

    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    def boom(path):
        raise subprocess.CalledProcessError(1, ["gh", "api", path], stderr="gh: Not Found (HTTP 404)\nskip=true\n" + "x" * 400)

    monkeypatch.setattr(tool, "gh_api", boom)
    assert tool.main(["--repo", REPO, "--sha", SHA]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert "CalledProcessError" in first and "gh: Not Found (HTTP 404)" in first
    assert len(first) < 450  # the stderr is cut to 200 characters
    lines = out.read_text().splitlines()
    assert len(lines) == 2 and lines[0] == "skip=false" and lines[1].startswith("reason=tests run in full:")
    assert "gh: Not Found (HTTP 404) skip=true" in lines[1]  # the line break became a space: no second «skip=» line


def test_the_documents_are_json_the_tool_reads(monkeypatch):
    """gh api prints JSON: the tool parses its stdout."""
    class P:
        stdout = json.dumps({"workflow_runs": []})

    monkeypatch.setattr(tool.subprocess, "run", lambda *a, **k: P())
    assert tool.gh_api("repos/o/r/actions/workflows/ci.yml/runs") == {"workflow_runs": []}
