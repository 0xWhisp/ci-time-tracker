"""Tests for the pull request view: one run against its baseline."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from ci_time_tracker.cli import main, parse_args
from ci_time_tracker.gates import evaluate_impact_gates
from ci_time_tracker.github_api import (
    COMMENT_MARKER,
    GitHubClient,
    fetch_impact_inputs,
)
from ci_time_tracker.impact import compare_run, format_impact_markdown
from ci_time_tracker.models import BuildLog, StepExecution

START = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).parent.parent


def step(name, duration, status="success", runner="ubuntu-latest"):
    return StepExecution(name=name, duration_seconds=duration, status=status, runner=runner)


def build(build_id, steps, sha=None, attempt=1, total=None, superseded=False, jobs=None, day=0):
    return BuildLog(
        build_id=str(build_id),
        head_sha=sha or f"{int(build_id):040x}",
        run_attempt=attempt,
        steps=steps,
        total_duration=total,
        timestamp=START + timedelta(days=day),
        metadata={
            "workflow_name": "CI",
            "url": f"https://github.com/acme/web/actions/runs/{build_id}",
            "superseded_attempt": superseded,
            "jobs": jobs or [],
        },
    )


def baseline(count=10, tests=100.0, install=30.0):
    return [
        build(index, [step("build / Run tests", tests), step("build / Install", install)], total=150.0, day=index)
        for index in range(count)
    ]


def run_of(tests=100.0, install=30.0, extra=(), total=150.0, status="success"):
    return build(
        999,
        [step("build / Run tests", tests, status=status), step("build / Install", install), *extra],
        sha="c" * 40,
        total=total,
        day=30,
    )


class TestCompareRun:
    """Step changes, build totals, failures."""

    def test_significant_slowdown_is_flagged(self):
        impact = compare_run([run_of(tests=150.0)], baseline(), "main")
        change = next(c for c in impact.changes if c.name == "build / Run tests")

        assert change.baseline_median == 100.0
        assert change.current == 150.0
        assert change.delta_seconds == 50.0
        assert change.delta_pct == pytest.approx(0.5)
        assert change.significant

    def test_significant_speedup_is_flagged_too(self):
        impact = compare_run([run_of(install=10.0)], baseline(), "main")
        change = next(c for c in impact.changes if c.name == "build / Install")

        assert change.delta_seconds == -20.0
        assert change.significant

    def test_noise_is_not_significant(self):
        """+5s on 100s is neither 25% nor 10 seconds."""
        impact = compare_run([run_of(tests=105.0)], baseline(), "main")

        assert not any(change.significant for change in impact.changes)

    def test_relative_and_absolute_thresholds_both_apply(self):
        """+3s on a 6s step is +50% but under 10 seconds."""
        history = [build(i, [step("build / lint", 6.0)], day=i) for i in range(5)]
        impact = compare_run([build(999, [step("build / lint", 9.0)], day=9)], history, "main")

        assert not impact.changes[0].significant

    def test_changes_are_ordered_by_size(self):
        impact = compare_run([run_of(tests=200.0, install=10.0)], baseline(), "main")

        assert [c.name for c in impact.changes] == ["build / Run tests", "build / Install"]

    def test_new_steps_have_no_baseline(self):
        impact = compare_run([run_of(extra=[step("build / Lint", 5.0)])], baseline(), "main")

        assert impact.new_steps == ["build / Lint"]

    def test_steps_with_too_little_history_are_not_judged(self):
        history = baseline(count=2)
        impact = compare_run([run_of(tests=500.0)], history, "main")

        assert impact.changes == []
        assert impact.new_steps == []
        assert impact.unjudged_steps == 2

    def test_build_totals_against_baseline_medians(self):
        impact = compare_run([run_of(tests=150.0, total=210.0)], baseline(), "main")

        assert impact.baseline_builds == 10
        assert impact.duration == 210.0
        assert impact.baseline_duration == 150.0
        assert impact.compute_seconds == 180.0
        assert impact.baseline_compute_seconds == 130.0

    def test_superseded_attempts_are_not_baseline_builds(self):
        history = baseline(count=5) + [
            build(50, [step("build / Run tests", 900.0)], superseded=True, day=50)
        ]

        impact = compare_run([run_of()], history, "main")

        assert impact.baseline_builds == 5
        assert next(c for c in impact.changes if c.name == "build / Run tests").baseline_median == 100.0

    def test_cost_is_compared_per_build(self):
        jobs = [{"name": "build", "runner": "ubuntu-latest", "duration_seconds": 120.0}]
        history = [build(i, [step("build / Run tests", 100.0)], jobs=jobs, day=i) for i in range(5)]
        current = build(999, [step("build / Run tests", 100.0)], jobs=[{**jobs[0], "duration_seconds": 300.0}], day=9)

        impact = compare_run([current], history, "main")

        assert impact.baseline_cost_usd == pytest.approx(0.016)  # 2 min
        assert impact.cost_usd == pytest.approx(0.040)  # 5 min

    def test_failure_in_known_flaky_step(self):
        """The baseline shows it failing and passing on one commit."""
        history = baseline() + [
            build(40, [step("integration / e2e", 5.0, "failure")], sha="f" * 40, day=40),
            build(41, [step("integration / e2e", 5.0, "success")], sha="f" * 40, day=41),
        ]
        current = run_of(extra=[step("integration / e2e", 5.0, "failure")])

        impact = compare_run([current], history, "main")

        assert len(impact.failures) == 1
        failure = impact.failures[0]
        assert failure.name == "integration / e2e"
        assert failure.known_flaky
        assert failure.evidence["flaky_commits"] == 1

    def test_failure_in_a_stable_step_is_not_flaky(self):
        impact = compare_run([run_of(status="failure")], baseline(), "main")

        assert [(f.name, f.known_flaky) for f in impact.failures] == [("build / Run tests", False)]

    def test_step_that_passed_on_re_run_of_this_run(self):
        first = build(999, [step("build / Run tests", 100.0, "failure")], sha="c" * 40, superseded=True)
        second = build(999, [step("build / Run tests", 100.0, "success")], sha="c" * 40, attempt=2)

        impact = compare_run([first, second], baseline(), "main")

        assert impact.recovered_on_rerun == ["build / Run tests"]
        assert impact.failures == []

    def test_matrix_legs_are_grouped(self):
        history = [
            build(i, [step("build (3.12) / Run tests", 100.0), step("build (3.13) / Run tests", 104.0)], day=i)
            for i in range(5)
        ]
        current = build(999, [step("build (3.12) / Run tests", 160.0), step("build (3.13) / Run tests", 164.0)])

        impact = compare_run([current], history, "main", group_matrix=True)

        assert impact.changes[0].name == "build / Run tests"
        assert impact.changes[0].baseline_median == 102.0
        assert impact.changes[0].current == 162.0

    def test_json_round_trip(self):
        impact = compare_run([run_of(tests=150.0)], baseline(), "main")

        data = json.loads(json.dumps(impact.to_dict()))

        assert data["baseline_branch"] == "main"
        assert data["changes"][0]["name"] == "build / Run tests"


class TestImpactComment:
    """The Markdown comment posted on the pull request."""

    def test_comment_carries_the_marker_and_headline(self):
        text = format_impact_markdown(compare_run([run_of(tests=150.0, total=210.0)], baseline(), "main"))

        assert text.startswith(COMMENT_MARKER)
        assert "## ⏱️ CI time impact" in text
        assert "[run 999](https://github.com/acme/web/actions/runs/999) on `ccccccc` took **3m 30s**" in text
        assert "**🔺 +1m 00s (+40%)** compared with the median of the last 10 runs on `main`" in text

    def test_totals_table(self):
        text = format_impact_markdown(compare_run([run_of(tests=150.0, total=210.0)], baseline(), "main"))

        assert "| Wall clock | 2m 30s | 3m 30s | 🔺 +1m 00s (+40%) |" in text
        assert "| Compute time | 2m 10s | 3m 00s | 🔺 +50.0s (+38%) |" in text

    def test_changed_steps_table(self):
        text = format_impact_markdown(compare_run([run_of(tests=150.0, install=10.0)], baseline(), "main"))

        assert "| build / Run tests | 1m 40s | 2m 30s | 🔺 +50.0s (+50%) |" in text
        assert "| build / Install | 30.0s | 10.0s | 🔻 -20.0s (-67%) |" in text

    def test_quiet_run_says_nothing_changed(self):
        text = format_impact_markdown(compare_run([run_of()], baseline(), "main"))

        assert "No step changed by more than 25% and 10.0s." in text
        assert "| Wall clock | 2m 30s | 2m 30s | ≈ +0.0s (+0%) |" in text

    def test_failure_wording(self):
        history = baseline() + [
            build(40, [step("integration / e2e", 5.0, "failure")], sha="f" * 40, day=40),
            build(41, [step("integration / e2e", 5.0, "success")], sha="f" * 40, day=41),
        ]
        current = run_of(status="failure", extra=[step("integration / e2e", 5.0, "failure")])

        text = format_impact_markdown(compare_run([current], history, "main"))

        # The run's own commit is one of the commits the step was observed on
        assert "- ❌ `integration / e2e`: ⚠️ known to be flaky (failed and passed on the same commit in 1 of 2 commits). A re-run may pass." in text
        assert "- ❌ `build / Run tests`: not known to be flaky." in text

    def test_rerun_wording(self):
        first = build(999, [step("build / Run tests", 100.0, "failure")], sha="c" * 40, superseded=True)
        second = build(999, [step("build / Run tests", 100.0)], sha="c" * 40, attempt=2)

        text = format_impact_markdown(compare_run([first, second], baseline(), "main"))

        assert "🔁 `build / Run tests` failed on an earlier attempt of this run and passed on re-run" in text

    def test_short_baseline_does_not_claim_nothing_changed(self):
        """Seen on this repository's first PR comment: 2 runs on main, 0 compared."""
        text = format_impact_markdown(compare_run([run_of(tests=500.0)], baseline(count=2), "main"))

        assert "Not enough history on `main` yet to compare steps: each needs at least 3 baseline runs." in text
        assert "No step changed" not in text

    def test_partly_judged_steps_are_counted(self):
        history = baseline() + [
            build(50 + i, [step("build / Lint", 5.0)], day=50 + i) for i in range(2)
        ]
        current = run_of(tests=150.0, extra=[step("build / Lint", 5.0)])

        text = format_impact_markdown(compare_run([current], history, "main"))

        assert "| build / Run tests |" in text
        assert "1 step with fewer than 3 runs on `main` was not compared." in text

    def test_new_steps_are_listed(self):
        text = format_impact_markdown(compare_run([run_of(extra=[step("build / Lint", 5.0)])], baseline(), "main"))

        assert "New steps, with no baseline yet: `build / Lint`." in text

    def test_no_baseline_yet(self):
        text = format_impact_markdown(compare_run([run_of()], [], "main"))

        assert "No completed runs of this workflow on `main` to compare with yet." in text
        assert "Wall clock" not in text

    def test_pipes_in_step_names_are_escaped(self):
        history = [build(i, [step("a | b", 100.0)], day=i) for i in range(5)]
        text = format_impact_markdown(compare_run([build(999, [step("a | b", 200.0)])], history, "main"))

        assert "| a \\| b |" in text


class TestImpactGates:
    """--fail-on and --max-duration for a single run."""

    def test_regression_gate_counts_only_slowdowns(self):
        slower = compare_run([run_of(tests=150.0)], baseline(), "main")
        faster = compare_run([run_of(install=10.0)], baseline(), "main")

        assert evaluate_impact_gates(slower, {"regression"}) == ["1 step slower than on main: build / Run tests"]
        assert evaluate_impact_gates(faster, {"regression"}) == []

    def test_flaky_gate(self):
        first = build(999, [step("build / Run tests", 100.0, "failure")], sha="c" * 40, superseded=True)
        second = build(999, [step("build / Run tests", 100.0)], sha="c" * 40, attempt=2)
        impact = compare_run([first, second], baseline(), "main")

        assert evaluate_impact_gates(impact, {"flaky"}) == ["1 flaky step hit: build / Run tests"]

    def test_max_duration_uses_the_run(self):
        impact = compare_run([run_of(total=700.0)], baseline(), "main")

        assert evaluate_impact_gates(impact, max_duration=600.0) == [
            "run duration 11m 40s exceeds the 10m 00s limit"
        ]
        assert evaluate_impact_gates(impact, max_duration=800.0) == []


class FakeGitHub:
    """Serves GET payloads by path and records writes."""

    def __init__(self, routes: list[tuple[str, Any]]):
        self.routes = routes
        self.reads: list[str] = []
        self.writes: list[tuple[str, str, dict | None]] = []

    def transport(self, url, headers):
        self.reads.append(url)
        for pattern, payload in self.routes:
            if re.search(pattern, url):
                return payload, {}
        raise AssertionError(f"Unexpected request: {url}")

    def sender(self, method, url, headers, body):
        self.writes.append((method, url, body))
        return {"html_url": f"https://github.com/acme/web/pull/7#issuecomment-{len(self.writes)}"}


def client_for(fake: FakeGitHub) -> GitHubClient:
    return GitHubClient(repo="acme/web", transport=fake.transport, sender=fake.sender)


class TestGitHubWrites:
    """Finding the pull request and keeping a single comment up to date."""

    def test_find_pull_request_by_head_commit(self):
        fake = FakeGitHub([(r"/commits/abc/pulls", [
            {"number": 3, "state": "closed", "head": {"sha": "abc"}},
            {"number": 7, "state": "open", "head": {"sha": "abc"}},
        ])])

        assert client_for(fake).find_pull_request("abc") == 7

    def test_no_pull_request(self):
        fake = FakeGitHub([(r"/commits/abc/pulls", [])])

        assert client_for(fake).find_pull_request("abc") is None

    def test_comment_is_created_when_none_exists(self):
        fake = FakeGitHub([(r"/issues/7/comments", [{"id": 1, "body": "LGTM"}])])

        action, url = client_for(fake).upsert_comment(7, "report")

        assert action == "created"
        assert fake.writes == [("POST", "https://api.github.com/repos/acme/web/issues/7/comments", {"body": "report"})]
        assert url.endswith("#issuecomment-1")

    def test_existing_comment_is_updated_not_duplicated(self):
        fake = FakeGitHub([(r"/issues/7/comments", [
            {"id": 1, "body": "LGTM"},
            {"id": 42, "body": f"{COMMENT_MARKER}\nold report"},
        ])])

        action, _ = client_for(fake).upsert_comment(7, "new report")

        assert action == "updated"
        assert fake.writes == [
            ("PATCH", "https://api.github.com/repos/acme/web/issues/comments/42", {"body": "new report"})
        ]

    def test_comment_search_follows_pages(self):
        first_page = [{"id": index, "body": "chatter"} for index in range(100)]
        fake = FakeGitHub([
            (r"/issues/7/comments\?per_page=100&page=1", first_page),
            (r"/issues/7/comments\?per_page=100&page=2", [{"id": 500, "body": COMMENT_MARKER}]),
        ])

        action, _ = client_for(fake).upsert_comment(7, "report")

        assert action == "updated"
        assert fake.writes[0][1].endswith("/issues/comments/500")


RUN = {
    "id": 999,
    "name": "CI",
    "workflow_id": 4242,
    "head_branch": "feature",
    "head_sha": "c" * 40,
    "run_attempt": 2,
    "status": "completed",
    "conclusion": "success",
    "run_started_at": "2026-10-01T10:00:00Z",
    "updated_at": "2026-10-01T10:05:00Z",
}

JOBS = {"jobs": [{
    "name": "build",
    "started_at": "2026-10-01T10:00:00Z",
    "completed_at": "2026-10-01T10:02:00Z",
    "labels": ["ubuntu-latest"],
    "steps": [{
        "name": "Run tests",
        "conclusion": "success",
        "started_at": "2026-10-01T10:00:00Z",
        "completed_at": "2026-10-01T10:01:40Z",
    }],
}]}


class TestFetchImpactInputs:
    """Fetching a run, its attempts and its baseline."""

    def _fake(self):
        baseline_runs = [{**RUN, "id": 999, "run_attempt": 1}] + [
            {**RUN, "id": 100 + index, "run_attempt": 1, "head_branch": "main"} for index in range(3)
        ]
        return FakeGitHub([
            (r"/actions/runs/999$", RUN),
            (r"/repos/acme/web$", {"default_branch": "main"}),
            (r"/attempts/\d+/jobs", JOBS),
            (r"/actions/workflows/4242/runs", {"workflow_runs": baseline_runs}),
        ])

    def test_run_attempts_baseline_and_default_branch(self):
        fake = self._fake()

        run, run_logs, logs, branch = fetch_impact_inputs("acme/web", 999, transport=fake.transport)

        assert run["id"] == 999
        assert branch == "main"
        assert [log.run_attempt for log in run_logs] == [1, 2]
        assert run_logs[0].metadata["superseded_attempt"] is True
        # The run itself is never part of its own baseline
        assert sorted(log.build_id for log in logs) == ["100", "101", "102"]

    def test_baseline_is_the_same_workflow_on_the_baseline_branch(self):
        fake = self._fake()

        fetch_impact_inputs("acme/web", 999, baseline_branch="develop", transport=fake.transport)

        listing = next(url for url in fake.reads if "/workflows/4242/runs" in url)
        assert "branch=develop" in listing
        assert not any(url.endswith("/repos/acme/web") for url in fake.reads)


class TestImpactCli:
    """--run-id mode end to end, with the API faked out."""

    def test_run_id_requires_github(self):
        with pytest.raises(SystemExit):
            parse_args(["--logs", "x", "--run-id", "1"])

    def test_comment_requires_run_id(self):
        with pytest.raises(SystemExit):
            parse_args(["--github", "acme/web", "--comment"])

    def _patch(self, monkeypatch, current, history, *argv):
        monkeypatch.setattr("sys.argv", ["ci-time-tracker", "--github", "acme/web", "--no-cache", "--run-id", "999", *argv])
        monkeypatch.setattr(
            "ci_time_tracker.cli.fetch_impact_inputs",
            lambda **kwargs: ({"id": 999, "head_sha": "c" * 40}, [current], history, "main"),
        )

    def test_prints_the_comparison(self, monkeypatch, capsys):
        self._patch(monkeypatch, run_of(tests=150.0), baseline())

        assert main() == 0
        assert "## ⏱️ CI time impact" in capsys.readouterr().out

    def test_json_format(self, monkeypatch, capsys):
        self._patch(monkeypatch, run_of(tests=150.0), baseline(), "--format", "json")

        assert main() == 0
        assert json.loads(capsys.readouterr().out)["changes"][0]["name"] == "build / Run tests"

    def test_comment_goes_to_the_run_pull_request(self, monkeypatch, capsys):
        fake = FakeGitHub([
            (r"/commits/c+/pulls", [{"number": 7, "state": "open", "head": {"sha": "c" * 40}}]),
            (r"/issues/7/comments", []),
        ])
        monkeypatch.setattr(
            "ci_time_tracker.cli.GitHubClient",
            lambda repo, token=None: GitHubClient(repo=repo, transport=fake.transport, sender=fake.sender),
        )
        self._patch(monkeypatch, run_of(tests=150.0), baseline(), "--comment")

        assert main() == 0
        method, url, body = fake.writes[0]
        assert method == "POST" and url.endswith("/issues/7/comments")
        assert body["body"].startswith(COMMENT_MARKER)
        assert "Comment created on pull request #7" in capsys.readouterr().err

    def test_comment_without_pull_request_warns(self, monkeypatch, capsys):
        fake = FakeGitHub([(r"/commits/c+/pulls", [])])
        monkeypatch.setattr(
            "ci_time_tracker.cli.GitHubClient",
            lambda repo, token=None: GitHubClient(repo=repo, transport=fake.transport, sender=fake.sender),
        )
        self._patch(monkeypatch, run_of(), baseline(), "--comment")

        assert main() == 0
        assert fake.writes == []
        assert "no open pull request found" in capsys.readouterr().err

    def test_regression_gate_exits_4(self, monkeypatch, capsys):
        self._patch(monkeypatch, run_of(tests=150.0), baseline(), "--fail-on", "regression")

        assert main() == 4
        assert "1 step slower than on main" in capsys.readouterr().err


class TestActionDefinition:
    """The composite action users install from the Marketplace."""

    def test_inputs_never_reach_a_script_unescaped(self):
        """Interpolating ${{ inputs.* }} inside run: is a shell injection risk."""
        action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))

        for step_definition in action["runs"]["steps"]:
            script = step_definition.get("run", "")
            assert "${{" not in script, f"step {step_definition.get('name')!r} interpolates into its script"

    def test_every_input_is_passed_to_the_tool(self):
        action = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
        report = next(s for s in action["runs"]["steps"] if s.get("name") == "Report CI time impact")
        passed = " ".join(str(value) for value in report["env"].values())

        for name in action["inputs"]:
            assert f"inputs.{name}" in passed, f"input {name!r} is never used"

    def test_dogfood_workflow_never_checks_out_pull_request_code(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/pr-impact.yml").read_text(encoding="utf-8"))
        checkout = next(s for s in workflow["jobs"]["comment"]["steps"] if "checkout" in s.get("uses", ""))

        assert "with" not in checkout or "ref" not in checkout["with"]
