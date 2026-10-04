"""Tests for evidence-based flaky and regression detection.

Covers same-commit flaky detection, re-run recoveries, the failure-rate
fallback, change-point regression detection, and how both reach the report.
"""

from datetime import datetime, timedelta, timezone

import pytest

from ci_time_tracker.analyzer import analyze_logs, detect_flaky_steps
from ci_time_tracker.cli import parse_args
from ci_time_tracker.detection import detect_flaky_by_commit, detect_regression
from ci_time_tracker.models import BuildLog, StepExecution
from ci_time_tracker.reporter import format_text, generate_report

START = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def step(name, status="success", duration=10.0):
    return StepExecution(name=name, status=status, duration_seconds=duration)


def build(build_id, sha, steps, attempt=1, workflow="CI", day=0):
    return BuildLog(
        build_id=str(build_id),
        head_sha=sha,
        run_attempt=attempt,
        steps=steps,
        timestamp=START + timedelta(days=day),
        metadata={"workflow_name": workflow},
    )


def history(durations, name="build / Run tests"):
    """One build per duration, each on its own commit, oldest first."""
    return [
        build(index, f"{index:040x}", [step(name, duration=duration)], day=index)
        for index, duration in enumerate(durations)
    ]


def samples(durations):
    return [(log.steps[0].duration_seconds, log) for log in history(durations)]


class TestFlakyByCommit:
    """A step is flaky when the same code both failed and passed."""

    def test_failure_and_success_on_the_same_commit_is_flaky(self):
        logs = [
            build(1, "a" * 40, [step("test / Run tests", "failure")]),
            build(2, "a" * 40, [step("test / Run tests", "success")]),
            build(3, "b" * 40, [step("test / Run tests", "success")]),
        ]

        evidence = detect_flaky_by_commit(logs)

        assert evidence["test / Run tests"] == {
            "method": "same-commit",
            "flaky_commits": 1,
            "observed_commits": 2,
            "rerun_recoveries": 0,
            "example_commits": ["aaaaaaa"],
        }

    def test_breakage_fixed_by_a_later_commit_is_not_flaky(self):
        """The old failure-rate rule flagged this; it is a real break and fix."""
        logs = [
            build(1, "a" * 40, [step("test / Run tests", "success")]),
            build(2, "b" * 40, [step("test / Run tests", "failure")]),
            build(3, "b" * 40, [step("test / Run tests", "failure")]),
            build(4, "c" * 40, [step("test / Run tests", "success")]),
        ]

        assert detect_flaky_by_commit(logs) == {}

        # The failure-rate heuristic cannot tell the difference
        result = analyze_logs([BuildLog(steps=log.steps) for log in logs])
        assert result.flaky_steps == ["test / Run tests"]
        assert result.metadata["flaky_detection"] == "failure-rate"

    def test_passing_on_re_run_counts_as_recovery(self):
        logs = [
            build(7, "a" * 40, [step("test / Run tests", "failure")], attempt=1),
            build(7, "a" * 40, [step("test / Run tests", "success")], attempt=2),
        ]

        evidence = detect_flaky_by_commit(logs)["test / Run tests"]

        assert evidence["rerun_recoveries"] == 1
        assert evidence["flaky_commits"] == 1

    def test_failing_again_on_re_run_is_not_a_recovery(self):
        logs = [
            build(7, "a" * 40, [step("test / Run tests", "failure")], attempt=1),
            build(7, "a" * 40, [step("test / Run tests", "failure")], attempt=2),
        ]

        assert detect_flaky_by_commit(logs) == {}

    def test_platform_specific_failure_in_a_matrix_is_not_flaky(self):
        """A leg that always fails on Windows is broken there, not flaky."""
        logs = [
            build(1, "a" * 40, [
                step("build (windows-latest) / Run tests", "failure"),
                step("build (ubuntu-latest) / Run tests", "success"),
            ]),
        ]

        result = analyze_logs(logs, group_matrix=True)

        assert result.steps[0].name == "build / Run tests"
        assert result.flaky_steps == []

    def test_flaky_matrix_leg_flags_the_grouped_step(self):
        logs = [
            build(1, "a" * 40, [step("build (windows-latest) / Run tests", "failure")]),
            build(2, "a" * 40, [step("build (windows-latest) / Run tests", "success")]),
        ]

        result = analyze_logs(logs, group_matrix=True)

        assert result.flaky_steps == ["build / Run tests"]

    def test_same_step_name_in_different_workflows_is_not_compared(self):
        logs = [
            build(1, "a" * 40, [step("build / Run tests", "failure")], workflow="Nightly"),
            build(2, "a" * 40, [step("build / Run tests", "success")], workflow="CI"),
        ]

        assert detect_flaky_by_commit(logs) == {}

    def test_skipped_steps_carry_no_evidence(self):
        logs = [
            build(1, "a" * 40, [step("test / Upload", "skipped")]),
            build(2, "a" * 40, [step("test / Upload", "success")]),
        ]

        assert detect_flaky_by_commit(logs) == {}

    def test_analysis_records_the_method_used(self):
        logs = [
            build(1, "a" * 40, [step("t", "failure")]),
            build(2, "a" * 40, [step("t", "success")]),
        ]

        result = analyze_logs(logs)

        assert result.metadata["flaky_detection"] == "same-commit"
        assert result.steps[0].flaky_evidence["flaky_commits"] == 1

    def test_rate_heuristic_still_bounded_for_text_logs(self):
        """Without commits the old 5%-95% rule applies, as before."""
        from ci_time_tracker.models import StepStatistics

        stats = [StepStatistics(name="s", execution_count=10, failure_count=1, failure_rate=0.1)]

        assert detect_flaky_steps(stats) == ["s"]


class TestRegressionDetection:
    """Change-point detection over a step's chronological durations."""

    def test_lasting_jump_is_a_regression(self):
        regression = detect_regression(samples([100.0] * 10 + [150.0] * 10))

        assert regression is not None
        assert regression["baseline_median"] == 100.0
        assert regression["current_median"] == 150.0
        assert regression["increase_seconds"] == 50.0
        assert regression["increase_pct"] == pytest.approx(0.5)
        assert regression["baseline_builds"] == 10
        assert regression["current_builds"] == 10

    def test_start_points_at_the_first_slow_build(self):
        """The split is placed where the regime changes, not merely near it."""
        regression = detect_regression(samples([100.0] * 20 + [150.0] * 30))

        assert regression["baseline_builds"] == 20
        started = regression["started_at"]
        assert started["build_id"] == "20"
        assert started["head_sha"] == f"{20:040x}"[:7]
        assert started["timestamp"].startswith("2026-09-21")

    def test_noise_without_a_shift_is_not_a_regression(self):
        noisy = [100.0, 104.0, 97.0, 101.0, 99.0, 103.0, 98.0, 102.0, 100.0, 96.0, 105.0, 99.0]

        assert detect_regression(samples(noisy)) is None

    def test_too_little_history_is_not_judged(self):
        assert detect_regression(samples([100.0] * 4 + [200.0] * 5)) is None

    def test_small_absolute_increase_is_ignored(self):
        """1s -> 2s doubles but is not worth anyone's attention."""
        assert detect_regression(samples([1.0] * 10 + [2.0] * 10)) is None

    def test_increase_below_threshold_is_ignored(self):
        steps = [100.0] * 10 + [115.0] * 10

        assert detect_regression(samples(steps)) is None
        assert detect_regression(samples(steps), threshold=0.10) is not None

    def test_spike_that_recovered_is_not_reported(self):
        """Only regressions that still hold for the latest builds matter."""
        steps = [100.0] * 10 + [200.0] * 6 + [100.0] * 10

        assert detect_regression(samples(steps)) is None

    def test_improvement_is_not_a_regression(self):
        assert detect_regression(samples([150.0] * 10 + [100.0] * 10)) is None

    def test_analysis_orders_builds_by_time_before_detecting(self):
        """Logs out of order still yield the right regression and start."""
        logs = history([100.0] * 10 + [150.0] * 10)
        shuffled = logs[::2] + logs[1::2]

        result = analyze_logs(shuffled)

        assert result.regressed_steps == ["build / Run tests"]
        assert result.steps[0].regression["started_at"]["build_id"] == "10"

    def test_grouped_matrix_uses_one_sample_per_build(self):
        """Legs are combined per build before looking for a change point."""
        logs = [
            build(index, f"{index:040x}", [
                step("build (3.12) / Run tests", duration=duration),
                step("build (3.13) / Run tests", duration=duration + 4.0),
            ], day=index)
            for index, duration in enumerate([100.0] * 10 + [150.0] * 10)
        ]

        result = analyze_logs(logs, group_matrix=True)

        assert result.regressed_steps == ["build / Run tests"]
        assert result.steps[0].regression["baseline_median"] == 102.0

    def test_metadata_says_how_many_steps_were_checked(self):
        logs = history([100.0] * 10)
        logs[0].steps.append(step("rare / step"))

        metadata = analyze_logs(logs).metadata

        assert metadata["regression_checked_steps"] == 1
        assert metadata["regression_min_builds"] == 10


class TestReportedIssues:
    """How regressions and flaky evidence read in the report."""

    def test_regression_issue_explains_size_and_origin(self):
        result = analyze_logs(history([100.0] * 10 + [150.0] * 10))
        text = format_text(generate_report(result))

        assert "[WARNING] REGRESSION: build / Run tests" in text
        assert "Median 1m 40s -> 2m 30s (+50%, +50.0s)" in text
        assert f"Started at commit {f'{10:040x}'[:7]} (build 10, 2026-09-11)" in text
        assert "REGR" in text

    def test_flaky_issue_cites_commits_and_re_runs(self):
        logs = [
            build(7, "a" * 40, [step("test / Run tests", "failure")], attempt=1),
            build(7, "a" * 40, [step("test / Run tests", "success")], attempt=2),
        ]
        text = format_text(generate_report(analyze_logs(logs)))

        assert "[ERROR] FLAKY: test / Run tests" in text
        assert "Failed and passed on the same commit in 1 of 1 commits" in text
        assert "Passed on re-run after failing in 1 run(s)" in text
        assert "e.g. aaaaaaa" in text

    def test_rate_based_flaky_issue_admits_its_limits(self):
        logs = [BuildLog(steps=[step("t", status)]) for status in ["failure", "success", "success"]]
        text = format_text(generate_report(analyze_logs(logs)))

        assert "Failure rate: 33.3%" in text
        assert "cannot tell flakiness from real breakage" in text

    def test_json_issue_carries_the_evidence(self):
        report = generate_report(analyze_logs(history([100.0] * 10 + [150.0] * 10)))
        issue = report.issues[0]

        assert issue["type"] == "regression"
        assert issue["increase_pct"] == pytest.approx(0.5)
        assert issue["started_at"]["build_id"] == "10"


class TestRegressionThresholdOption:
    """--regression-threshold takes a percentage."""

    def test_default_is_25_percent(self):
        assert parse_args(["--logs", "x"]).regression_threshold == pytest.approx(0.25)

    def test_percentage_becomes_a_fraction(self):
        args = parse_args(["--logs", "x", "--regression-threshold", "10"])

        assert args.regression_threshold == pytest.approx(0.10)

    @pytest.mark.parametrize("value", ["0", "-5", "fast"])
    def test_invalid_values_are_rejected(self, value):
        with pytest.raises(SystemExit):
            parse_args(["--logs", "x", "--regression-threshold", value])
