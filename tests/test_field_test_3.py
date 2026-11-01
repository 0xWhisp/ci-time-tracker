"""Fixes from the third field test (TypeScript, react, tokio, prometheus,
spring-boot, django, rubocop): regressions on one branch, and matrix-leg
recommendations grouped per workflow and platform."""

from concurrent.futures import Future
from datetime import datetime, timedelta, timezone

from ci_time_tracker.analyzer import analyze_logs
from ci_time_tracker.cli import _default_branch_if_mixed
from ci_time_tracker.github_api import GitHubAPIError, GitHubClient
from ci_time_tracker.html_report import format_html
from ci_time_tracker.models import BuildLog, StepExecution
from ci_time_tracker.pricing import PricingTable
from ci_time_tracker.recommendations import recommend_expensive_matrix_legs
from ci_time_tracker.reporter import format_markdown, generate_report

START = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
PATH = ".github/workflows/ci.yml"


def build(index, duration, branch):
    return BuildLog(
        build_id=str(index),
        head_sha=f"{index:040x}",
        steps=[StepExecution(name="build / Run tests", duration_seconds=duration)],
        total_duration=duration,
        timestamp=START + timedelta(hours=index),
        metadata={"branch": branch, "workflow_name": "CI"},
    )


def interleaved(main_durations, pr_durations):
    """Main and pull request builds alternating in time, as an unfiltered run sees them."""
    logs = []
    for index, (main, pr) in enumerate(zip(main_durations, pr_durations, strict=True)):
        logs.append(build(2 * index, main, "main"))
        logs.append(build(2 * index + 1, pr, f"feature-{index}"))
    return logs


class TestRegressionsOnOneBranch:
    """Seen on rubocop: 26 master builds mixed with 74 from pull requests."""

    def test_pull_request_builds_cannot_fake_a_regression(self):
        # Main is steady; pull request branches start fast (fresh) then slow
        logs = interleaved([100.0] * 12, [60.0] * 6 + [200.0] * 6)

        result = analyze_logs(logs)

        assert result.regressed_steps == []
        assert result.metadata["regression_branch"] == "main"

    def test_a_real_regression_on_main_is_still_found(self):
        logs = interleaved([100.0] * 6 + [160.0] * 6, [100.0] * 12)

        result = analyze_logs(logs)

        assert result.regressed_steps == ["build / Run tests"]

    def test_the_given_default_branch_wins_over_the_busiest(self):
        logs = [build(i, 100.0, "main") for i in range(3)] + [
            build(10 + i, 100.0 if i < 6 else 160.0, "master") for i in range(12)
        ] + [build(30 + i, 100.0, "feature") for i in range(14)]

        result = analyze_logs(logs, regression_branch="master")

        assert result.metadata["regression_branch"] == "master"
        assert result.regressed_steps == ["build / Run tests"]

    def test_an_unknown_given_branch_falls_back_to_the_busiest(self):
        logs = interleaved([100.0] * 12, [100.0] * 12) + [build(100, 100.0, "main")]

        result = analyze_logs(logs, regression_branch="trunk")

        assert result.metadata["regression_branch"] == "main"

    def test_a_single_branch_needs_no_choice(self):
        result = analyze_logs([build(i, 100.0, "main") for i in range(12)])

        assert "regression_branch" not in result.metadata

    def test_reports_say_which_branch(self):
        report = generate_report(analyze_logs(interleaved([100.0] * 12, [100.0] * 12)))

        assert "steps with ≥10 builds on `main`" in format_markdown(report)
        assert "builds on main." in format_html(report)


class FakeClient:
    """Stands in for GitHubClient(repo, token) in the CLI."""

    calls = 0

    def __init__(self, repo, token=None, fail=False):
        self.fail = fail

    def fetch_default_branch(self):
        FakeClient.calls += 1
        if self.fail:
            raise GitHubAPIError("boom", status=500)
        return "master"


class TestDefaultBranchLookup:
    def test_single_branch_makes_no_request(self, monkeypatch):
        FakeClient.calls = 0
        monkeypatch.setattr("ci_time_tracker.cli.GitHubClient", FakeClient)

        assert _default_branch_if_mixed("acme/web", [build(1, 1.0, "main"), build(2, 1.0, "main")]) is None
        assert FakeClient.calls == 0

    def test_mixed_branches_ask_github(self, monkeypatch):
        monkeypatch.setattr("ci_time_tracker.cli.GitHubClient", FakeClient)

        assert _default_branch_if_mixed("acme/web", [build(1, 1.0, "master"), build(2, 1.0, "fix")]) == "master"

    def test_a_failed_lookup_leaves_the_choice_to_the_analysis(self, monkeypatch):
        monkeypatch.setattr(
            "ci_time_tracker.cli.GitHubClient", lambda repo, token=None: FakeClient(repo, fail=True)
        )

        assert _default_branch_if_mixed("acme/web", [build(1, 1.0, "main"), build(2, 1.0, "fix")]) is None

    def test_a_lookup_under_way_is_used(self, monkeypatch):
        FakeClient.calls = 0
        monkeypatch.setattr("ci_time_tracker.cli.GitHubClient", FakeClient)
        lookup = Future()
        lookup.set_result("trunk")

        mixed = [build(1, 1.0, "main"), build(2, 1.0, "fix")]
        assert _default_branch_if_mixed("acme/web", mixed, lookup) == "trunk"
        assert _default_branch_if_mixed("acme/web", mixed[:1], lookup) is None
        assert FakeClient.calls == 0

    def test_client_reads_the_default_branch(self):
        client = GitHubClient(repo="acme/web", transport=lambda url, headers: ({"default_branch": "trunk"}, {}))

        assert client.fetch_default_branch() == "trunk"


def legs_build(index, jobs):
    """A build whose Linux and Windows legs of each job all passed."""
    job_entries = []
    for job in jobs:
        job_entries.append({"name": f"{job} (ubuntu)", "runner": "ubuntu-latest", "duration_seconds": 100.0, "conclusion": "success"})
        job_entries.append({"name": f"{job} (windows)", "runner": "windows-latest", "duration_seconds": 300.0, "conclusion": "success"})
    return BuildLog(
        build_id=str(index),
        metadata={"workflow_path": PATH, "jobs": job_entries},
    )


class TestMatrixLegsGrouped:
    """Seen on tokio: eighteen near-identical recommendations, one per job."""

    def test_one_recommendation_per_workflow_and_platform(self):
        jobs = ["test a", "test b", "test c", "test d", "test e"]
        logs = [legs_build(i, jobs) for i in range(12)]

        [item] = recommend_expensive_matrix_legs(logs, PricingTable())

        assert item.title == "Run the Windows legs of 5 jobs less often"
        assert item.job is None
        assert "the Windows legs of `test a`, `test b`, `test c` and 2 more" in item.evidence
        assert "75% of those jobs' compute" in item.evidence
        assert item.time_at_stake_seconds == 5 * 12 * 300.0

    def test_jobs_are_listed_by_time_at_stake(self):
        logs = [legs_build(i, ["small", "big"]) for i in range(12)]
        for log in logs:
            for job in log.metadata["jobs"]:
                if job["name"] == "big (windows)":
                    job["duration_seconds"] = 900.0

        [item] = recommend_expensive_matrix_legs(logs, PricingTable())

        assert "the Windows legs of `big`, `small` never failed" in item.evidence

    def test_a_job_that_failed_alone_is_left_out_of_the_group(self):
        logs = [legs_build(i, ["steady", "caught-a-bug"]) for i in range(12)]
        logs[3].metadata["jobs"][3]["conclusion"] = "failure"  # caught-a-bug (windows)

        [item] = recommend_expensive_matrix_legs(logs, PricingTable())

        assert item.title == "Run the Windows legs of `steady` less often"
        assert "caught-a-bug" not in item.evidence
