"""Tests for recommendations, public-repository pricing and API robustness."""

import base64
import http.client
import io
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

import ci_time_tracker.github_api as github_api
from ci_time_tracker.analyzer import analyze_logs
from ci_time_tracker.cli import _visibility_pricing, main
from ci_time_tracker.github_api import (
    GitHubAPIError,
    GitHubClient,
    build_log_from_run,
    fetch_build_logs,
)
from ci_time_tracker.impact import compare_run, format_impact_markdown
from ci_time_tracker.models import BuildLog, StepExecution
from ci_time_tracker.pricing import PricingTable
from ci_time_tracker.recommendations import (
    MIN_TIME_AT_STAKE_SECONDS,
    _job_caches,
    parse_workflow,
    recommend,
    recommend_cancelling_superseded_runs,
    recommend_dependency_caches,
    recommend_expensive_matrix_legs,
)
from ci_time_tracker.reporter import format_markdown, format_text, generate_report

START = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
PATH = ".github/workflows/ci.yml"

WORKFLOW_NO_CACHE = parse_workflow("""
name: CI
on: [push, pull_request]
jobs:
  test:
    name: Tests (${{ matrix.os }})
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Install
        run: pip install -e ".[dev]"
      - name: Test
        run: pytest
""")


def build(index, steps=(), jobs=(), event="push", branch="main", duration=600.0, minutes_apart=60,
          head_repo="acme/web", private=True, superseded=False):
    return BuildLog(
        build_id=str(index),
        head_sha=f"{index:040x}",
        steps=list(steps),
        total_duration=duration,
        timestamp=START + timedelta(minutes=index * minutes_apart),
        metadata={
            "workflow_name": "CI",
            "workflow_path": PATH,
            "event": event,
            "branch": branch,
            "head_repository": head_repo,
            "repository_private": private,
            "superseded_attempt": superseded,
            "jobs": list(jobs),
        },
    )


def install_step(duration, job="Tests (ubuntu-latest)", runner="ubuntu-latest"):
    return StepExecution(
        name=f"{job} / Install", duration_seconds=duration, job_name=job, runner=runner,
    )


class TestParseWorkflow:
    def test_bare_on_key_is_restored(self):
        """YAML 1.1 reads `on:` as the boolean True."""
        assert parse_workflow("on: push\njobs: {}")["on"] == "push"

    def test_invalid_or_non_mapping_yaml_is_none(self):
        assert parse_workflow("jobs: [") is None
        assert parse_workflow("- a list") is None


class TestJobCaches:
    @pytest.mark.parametrize("steps", [
        [{"uses": "actions/setup-python@v6", "with": {"cache": "pip"}}],
        [{"uses": "actions/setup-node@v4", "with": {"cache": "npm"}}],
        [{"uses": "actions/cache@v4", "with": {"path": "~/.cache"}}],
        [{"uses": "actions/setup-go@v5"}],  # caches by default
        [{"uses": "ruby/setup-ruby@v1", "with": {"bundler-cache": True}}],
        [{"uses": "astral-sh/setup-uv@v6"}],
        [{"uses": "some-org/fancy-cache-action@v1"}],
    ])
    def test_recognized_caches(self, steps):
        assert _job_caches(steps)

    @pytest.mark.parametrize("steps", [
        [{"uses": "actions/setup-python@v6", "with": {"python-version": "3.12"}}],
        [{"uses": "actions/setup-go@v5", "with": {"cache": False}}],
        [{"run": "pip install ."}],
    ])
    def test_no_cache(self, steps):
        assert not _job_caches(steps)


class TestDependencyCache:
    def test_slow_uncached_install_is_recommended(self):
        logs = [build(i, [install_step(60.0)]) for i in range(12)]

        [item] = recommend_dependency_caches(logs, {PATH: WORKFLOW_NO_CACHE}, PricingTable())

        assert item.kind == "dependency-cache"
        assert item.title == "Cache pip dependencies in `Tests`"
        assert item.job == "Tests"
        assert "median of 1m 00s per run (12m 00s over 12 runs)" in item.evidence
        assert item.suggestion == "Set `cache: pip` on actions/setup-python."
        assert item.time_at_stake_seconds == 720.0
        assert item.cost_at_stake_usd == pytest.approx(12 * 0.006)

    def test_job_with_a_cache_is_left_alone(self):
        workflow = parse_workflow("""
jobs:
  test:
    name: Tests (${{ matrix.os }})
    steps:
      - uses: actions/setup-python@v6
        with: {cache: pip}
      - name: Install
        run: pip install .
""")
        logs = [build(i, [install_step(60.0)]) for i in range(12)]

        assert recommend_dependency_caches(logs, {PATH: workflow}, PricingTable()) == []

    def test_fast_install_is_not_worth_it(self):
        logs = [build(i, [install_step(8.0)]) for i in range(12)]

        assert recommend_dependency_caches(logs, {PATH: WORKFLOW_NO_CACHE}, PricingTable()) == []

    def test_too_few_runs_to_judge(self):
        logs = [build(i, [install_step(60.0)]) for i in range(3)]

        assert recommend_dependency_caches(logs, {PATH: WORKFLOW_NO_CACHE}, PricingTable()) == []

    def test_unnamed_step_matches_its_run_command(self):
        workflow = parse_workflow("jobs:\n  build:\n    steps:\n      - run: npm ci\n")
        step = StepExecution(name="build / Run npm ci", duration_seconds=90.0, job_name="build", runner="ubuntu-latest")
        logs = [build(i, [step]) for i in range(10)]

        [item] = recommend_dependency_caches(logs, {PATH: workflow}, PricingTable())

        assert item.suggestion == "Set `cache: npm` on actions/setup-node."

    def test_missing_workflow_means_no_recommendation(self):
        logs = [build(i, [install_step(60.0)]) for i in range(12)]

        assert recommend_dependency_caches(logs, {}, PricingTable()) == []


def job(name, runner, duration, conclusion="success"):
    return {"name": name, "runner": runner, "duration_seconds": duration, "conclusion": conclusion}


class TestCancelSupersededRuns:
    def _overlapping_prs(self, count=4, **kwargs):
        """PR runs 5 minutes apart that each take 10 minutes."""
        return [
            build(i, event="pull_request", branch="feature", minutes_apart=5, duration=600.0,
                  jobs=[job("test", "ubuntu-latest", 600.0)], **kwargs)
            for i in range(count)
        ]

    def test_obsolete_runs_are_counted(self):
        [item] = recommend_cancelling_superseded_runs(
            self._overlapping_prs(), {PATH: WORKFLOW_NO_CACHE}, PricingTable()
        )

        assert item.kind == "cancel-superseded-runs"
        # 3 runs each kept running 5 of their 10 minutes after the next push
        assert "3 pull request runs" in item.evidence
        assert item.time_at_stake_seconds == pytest.approx(900.0)
        assert "cancel-in-progress" in item.suggestion

    def test_workflow_that_already_cancels_is_left_alone(self):
        workflow = parse_workflow(
            "concurrency:\n  group: ci-${{ github.ref }}\n  cancel-in-progress: true\njobs: {}\n"
        )

        assert recommend_cancelling_superseded_runs(self._overlapping_prs(), {PATH: workflow}, PricingTable()) == []

    def test_runs_that_did_not_overlap(self):
        logs = [
            build(i, event="pull_request", branch="feature", minutes_apart=30, duration=600.0,
                  jobs=[job("test", "ubuntu-latest", 600.0)])
            for i in range(4)
        ]

        assert recommend_cancelling_superseded_runs(logs, {PATH: WORKFLOW_NO_CACHE}, PricingTable()) == []

    def test_pushes_to_the_default_branch_are_not_counted(self):
        """Every commit on main should usually be tested; only PR runs count."""
        logs = [
            build(i, event="push", branch="main", minutes_apart=5, duration=600.0,
                  jobs=[job("test", "ubuntu-latest", 600.0)])
            for i in range(4)
        ]

        assert recommend_cancelling_superseded_runs(logs, {PATH: WORKFLOW_NO_CACHE}, PricingTable()) == []

    def test_same_branch_name_in_different_forks_is_not_compared(self):
        logs = [
            build(i, event="pull_request", branch="main", minutes_apart=5, duration=600.0,
                  head_repo=f"fork{i}/web", jobs=[job("test", "ubuntu-latest", 600.0)])
            for i in range(4)
        ]

        assert recommend_cancelling_superseded_runs(logs, {PATH: WORKFLOW_NO_CACHE}, PricingTable()) == []


class TestExpensiveMatrixLegs:
    def _builds(self, count=12, windows_alone_fails_at=None):
        logs = []
        for i in range(count):
            windows = "failure" if i == windows_alone_fails_at else "success"
            logs.append(build(i, jobs=[
                job("Tests (ubuntu-latest)", "ubuntu-latest", 120.0),
                job("Tests (windows-latest)", "windows-latest", 300.0, windows),
            ]))
        return logs

    def test_legs_that_never_failed_alone(self):
        [item] = recommend_expensive_matrix_legs(self._builds(), PricingTable())

        assert item.kind == "expensive-matrix-legs"
        assert item.title == "Run the Windows legs of `Tests` less often"
        assert "In 12 builds, the Windows legs never failed when the Linux legs passed" in item.evidence
        assert "71% of the job's compute" in item.evidence  # 300 of 420 seconds
        assert item.time_at_stake_seconds == 3600.0
        assert item.cost_at_stake_usd == pytest.approx(12 * 5 * 0.010)

    def test_a_failure_of_their_own_keeps_them(self):
        assert recommend_expensive_matrix_legs(self._builds(windows_alone_fails_at=4), PricingTable()) == []

    def test_too_few_builds_to_trust(self):
        assert recommend_expensive_matrix_legs(self._builds(count=6), PricingTable()) == []

    def test_public_repository_has_no_cost_at_stake(self):
        """Standard runners are free for public repositories: time only."""
        [item] = recommend_expensive_matrix_legs(self._builds(), PricingTable(public_repository=True))

        assert item.cost_at_stake_usd is None
        assert "$" not in item.evidence


class TestRecommend:
    def test_small_findings_are_dropped(self):
        """Seen in the field test: 2.5 minutes at stake is not worth a line."""
        logs = [build(i, [install_step(25.0)]) for i in range(6)]  # 150s at stake

        assert recommend(logs, {PATH: WORKFLOW_NO_CACHE}) == []
        assert MIN_TIME_AT_STAKE_SECONDS == 600.0

    def test_ordered_by_time_at_stake(self):
        logs = [
            build(i, [install_step(60.0)], jobs=[
                job("Tests (ubuntu-latest)", "ubuntu-latest", 120.0),
                job("Tests (windows-latest)", "windows-latest", 300.0),
            ])
            for i in range(12)
        ]

        items = recommend(logs, {PATH: WORKFLOW_NO_CACHE})

        assert [item.kind for item in items] == ["expensive-matrix-legs", "dependency-cache"]


class TestReportRendering:
    def _report(self):
        logs = [build(i, [install_step(60.0)]) for i in range(12)]
        result = analyze_logs(logs)
        result.recommendations = [item.to_dict() for item in recommend(logs, {PATH: WORKFLOW_NO_CACHE})]
        return generate_report(result)

    def test_text_section(self):
        text = format_text(self._report())

        assert "RECOMMENDATIONS" in text
        assert "1. Cache pip dependencies in `Tests` (.github/workflows/ci.yml)" in text
        assert "Suggestion: Set `cache: pip` on actions/setup-python." in text
        assert "At stake: 12m 00s, $0.07" in text
        assert "an upper bound on the saving" in text

    def test_markdown_section(self):
        markdown = format_markdown(self._report())

        assert "### Recommendations" in markdown
        assert "1. **Cache pip dependencies in `Tests`** · `.github/workflows/ci.yml`" in markdown
        assert "*At stake: 12m 00s, $0.07.*" in markdown

    def test_multiline_suggestion_becomes_a_yaml_block(self):
        report = self._report()
        report.recommendations = [{
            "kind": "cancel-superseded-runs", "title": "Cancel", "workflow": PATH, "job": None,
            "evidence": "e", "suggestion": "Add to the workflow:\nconcurrency:\n  group: x",
            "time_at_stake_seconds": 900.0, "cost_at_stake_usd": None,
        }]

        markdown = format_markdown(report)

        assert "   ```yaml\n   concurrency:\n     group: x\n   ```" in markdown

    def test_json_round_trip_keeps_recommendations(self):
        from ci_time_tracker.models import Report

        report = self._report()

        assert Report.from_dict(report.to_dict()).recommendations == report.recommendations


class TestPublicRepositoryPricing:
    def test_visibility_comes_from_the_runs(self):
        public, visibility = _visibility_pricing([build(1, private=False)], None)
        private, private_visibility = _visibility_pricing([build(1, private=True)], None)
        unknown, unknown_visibility = _visibility_pricing([BuildLog()], None)

        assert (public.public_repository, visibility) == (True, "public")
        assert (private.public_repository, private_visibility) == (False, "private")
        assert (unknown.public_repository, unknown_visibility) == (False, None)

    def test_standard_runners_free_larger_runners_billed(self):
        table = PricingTable(public_repository=True)

        assert table.price_for("ubuntu-24.04") == 0.0
        assert table.price_for("macos-26") == 0.0
        assert table.price_for("macos-15-xlarge") == 0.102
        assert table.price_for("ubuntu-24.04-8-cores") == 0.022

    def test_pricing_file_still_wins(self, tmp_path):
        import json

        from ci_time_tracker.pricing import load_pricing

        path = tmp_path / "pricing.json"
        path.write_text(json.dumps({"ubuntu-latest": 0.5}), encoding="utf-8")
        table = load_pricing(path)
        from dataclasses import replace

        assert replace(table, public_repository=True).price_for("ubuntu-latest") == 0.5

    def test_report_note_for_public_repository(self):
        jobs = [job("test", "ubuntu-latest", 600.0)]
        result = analyze_logs([build(1, jobs=jobs)], PricingTable(public_repository=True))
        result.metadata["repository_visibility"] = "public"

        text = format_text(generate_report(result))

        assert "estimated_cost_usd: $0.00" in text
        assert "public repository, so standard runners are free" in text

    def test_impact_footer_for_public_repository(self):
        history = [build(i, [install_step(60.0)]) for i in range(5)]
        current = build(99, [install_step(60.0)])

        text = format_impact_markdown(compare_run([current], history, "main", PricingTable(public_repository=True)))

        assert "Public repository: standard runners are free" in text


class FakeResponse(io.BytesIO):
    headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://api.github.com/x", code, "error", headers or {}, None)


class TestRetries:
    """Reads survive transient failures; real errors still stop the run."""

    def _urlopen(self, monkeypatch, outcomes):
        calls = []
        sleeps = []

        def urlopen(request, timeout=None):
            calls.append(request.full_url)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return FakeResponse(outcome.encode("utf-8"))

        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        monkeypatch.setattr(github_api, "_sleep", sleeps.append)
        return calls, sleeps

    def test_server_error_is_retried(self, monkeypatch):
        calls, sleeps = self._urlopen(monkeypatch, [http_error(502), '{"ok": true}'])

        payload, _ = github_api._urllib_transport("https://api.github.com/x", {})

        assert payload == {"ok": True}
        assert len(calls) == 2
        assert sleeps == [1.0]

    def test_network_error_is_retried(self, monkeypatch):
        calls, _ = self._urlopen(monkeypatch, [urllib.error.URLError("reset"), '{"ok": true}'])

        assert github_api._urllib_transport("https://api.github.com/x", {})[0] == {"ok": True}

    @pytest.mark.parametrize("error", [
        http.client.IncompleteRead(b"", 302024),  # seen on pydantic's jobs pages
        http.client.RemoteDisconnected("closed"),
        ConnectionResetError("reset by peer"),
    ])
    def test_a_cut_connection_is_retried(self, monkeypatch, error):
        calls, _ = self._urlopen(monkeypatch, [error, '{"ok": true}'])

        assert github_api._urllib_transport("https://api.github.com/x", {})[0] == {"ok": True}
        assert len(calls) == 2

    def test_a_cut_connection_that_persists_is_an_api_error(self, monkeypatch):
        self._urlopen(monkeypatch, [http.client.IncompleteRead(b"", 10)] * 3)

        with pytest.raises(GitHubAPIError, match="Could not reach"):
            github_api._urllib_transport("https://api.github.com/x", {})

    def test_gives_up_after_three_attempts(self, monkeypatch):
        calls, _ = self._urlopen(monkeypatch, [http_error(502)] * 3)

        with pytest.raises(GitHubAPIError) as error:
            github_api._urllib_transport("https://api.github.com/x", {})

        assert error.value.status == 502
        assert len(calls) == 3

    def test_secondary_rate_limit_waits_as_told(self, monkeypatch):
        _, sleeps = self._urlopen(monkeypatch, [http_error(403, {"Retry-After": "7"}), '{"ok": true}'])

        github_api._urllib_transport("https://api.github.com/x", {})

        assert sleeps == [7.0]

    def test_exhausted_rate_limit_is_not_retried(self, monkeypatch):
        """Waiting out the hourly limit would hang the run."""
        calls, _ = self._urlopen(monkeypatch, [http_error(403, {"X-RateLimit-Remaining": "0"})])

        with pytest.raises(GitHubAPIError, match="rate limit"):
            github_api._urllib_transport("https://api.github.com/x", {})
        assert len(calls) == 1

    def test_not_found_is_not_retried(self, monkeypatch):
        calls, _ = self._urlopen(monkeypatch, [http_error(404)])

        with pytest.raises(GitHubAPIError):
            github_api._urllib_transport("https://api.github.com/x", {})
        assert len(calls) == 1


RUN = {
    "id": 7, "name": "CI", "path": PATH, "run_attempt": 1, "status": "completed",
    "head_sha": "a" * 40, "head_branch": "feature", "event": "pull_request",
    "head_repository": {"full_name": "fork/web"}, "repository": {"private": False},
    "run_started_at": "2026-10-01T10:00:00Z", "updated_at": "2026-10-01T10:05:00Z",
}


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[str] = []

    def __call__(self, url, headers):
        self.calls.append(url)
        return self.handler(url), {}


class TestLargeAndFailingRuns:
    """Seen on astral-sh/uv: a 120-job run that GitHub cannot page by 100."""

    def test_large_run_is_paged_in_smaller_pieces(self):
        def handler(url):
            if "/jobs" in url and "per_page=100" in url:
                raise GitHubAPIError("timeout", status=502)
            if "/jobs" in url:
                return {"jobs": [{"name": "j", "steps": []}] * (30 if "page=1" in url else 5)}
            return {"workflow_runs": [RUN]}

        transport = FakeTransport(handler)
        logs = fetch_build_logs("acme/web", limit=1, transport=transport)

        assert len(logs[0].metadata["jobs"]) == 35
        assert any("per_page=30" in url for url in transport.calls)

    def test_run_that_keeps_failing_is_skipped_not_fatal(self):
        runs = [RUN, {**RUN, "id": 8}]

        def handler(url):
            if "/runs/7/" in url:
                raise GitHubAPIError("server error", status=500)
            if "/jobs" in url:
                return {"jobs": []}
            return {"workflow_runs": runs}

        skipped: list[str] = []
        logs = fetch_build_logs("acme/web", limit=2, transport=FakeTransport(handler), skipped=skipped)

        assert [log.build_id for log in logs] == ["8"]
        assert skipped == ["7"]

    def test_access_errors_still_stop_the_run(self):
        def handler(url):
            if "/jobs" in url:
                raise GitHubAPIError("forbidden", status=403)
            return {"workflow_runs": [RUN]}

        with pytest.raises(GitHubAPIError):
            fetch_build_logs("acme/web", limit=1, transport=FakeTransport(handler))


class TestWorkflowFiles:
    def test_run_metadata_for_recommendations(self):
        log = build_log_from_run(RUN, [])

        assert log.metadata["workflow_path"] == PATH
        assert log.metadata["head_repository"] == "fork/web"
        assert log.metadata["repository_private"] is False

    def test_workflow_file_is_decoded(self):
        content = base64.b64encode(b"name: CI\n").decode()
        client = GitHubClient(repo="acme/web", transport=FakeTransport(
            lambda url: {"encoding": "base64", "content": content}
        ))

        assert client.fetch_workflow_file(PATH) == "name: CI\n"

    def test_missing_workflow_file_is_none(self):
        def handler(url):
            raise GitHubAPIError("not found", status=404)

        client = GitHubClient(repo="acme/web", transport=FakeTransport(handler))

        assert client.fetch_workflow_file("dynamic/dependabot/x") is None


class TestCli:
    def test_github_mode_attaches_recommendations(self, monkeypatch, capsys):
        logs = [
            build(i, [install_step(60.0)], jobs=[job("Tests (ubuntu-latest)", "ubuntu-latest", 120.0)])
            for i in range(12)
        ]
        workflow_yaml = base64.b64encode(b"""
jobs:
  test:
    name: Tests (${{ matrix.os }})
    steps:
      - name: Install
        run: pip install .
""").decode()

        monkeypatch.setattr("ci_time_tracker.cli.fetch_build_logs", lambda **kwargs: logs)
        monkeypatch.setattr(
            "ci_time_tracker.cli.GitHubClient",
            lambda repo, token=None: GitHubClient(repo=repo, transport=FakeTransport(
                lambda url: {"encoding": "base64", "content": workflow_yaml}
            )),
        )
        monkeypatch.setattr("sys.argv", ["ci-time-tracker", "--github", "acme/web", "--no-cache"])

        assert main() == 0
        assert "Cache pip dependencies in `Tests`" in capsys.readouterr().out

    def test_estimates_are_deprecated(self, monkeypatch, capsys, tmp_path):
        config = tmp_path / "ci.yml"
        config.write_text("on: push\njobs:\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n", encoding="utf-8")
        estimates = tmp_path / "estimates.json"
        estimates.write_text("{}", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["ci-time-tracker", "--config", str(config), "--estimates", str(estimates)])

        assert main() == 0
        assert "--estimates is deprecated" in capsys.readouterr().err
