"""Tests for Markdown output and the CI quality gates."""

from datetime import datetime, timedelta, timezone

import pytest

from ci_time_tracker.analyzer import analyze_logs
from ci_time_tracker.cli import main, parse_args
from ci_time_tracker.gates import evaluate_gates
from ci_time_tracker.models import AnalysisResult, BuildLog, PipelineStep, StepExecution
from ci_time_tracker.reporter import format_markdown, generate_report

START = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)


def regressed_and_flaky_logs():
    """20 builds: "Run tests" regresses at build 10; "e2e" flips on one commit."""
    logs = []
    for index in range(20):
        duration = 100.0 if index < 10 else 150.0
        logs.append(BuildLog(
            build_id=str(index),
            head_sha=f"{index:040x}",
            steps=[StepExecution(name="build / Run tests", duration_seconds=duration)],
            total_duration=duration + 20.0,
            timestamp=START + timedelta(days=index),
            metadata={"workflow_name": "CI"},
        ))
    # The same commit, built twice: once failing, once passing
    for index, status in ((20, "failure"), (21, "success")):
        logs.append(BuildLog(
            build_id=str(index),
            head_sha="f" * 40,
            steps=[StepExecution(name="integration / e2e", status=status, duration_seconds=5.0)],
            timestamp=START + timedelta(days=index),
            metadata={"workflow_name": "CI"},
        ))
    return logs


@pytest.fixture
def result():
    return analyze_logs(regressed_and_flaky_logs())


class TestGates:
    """evaluate_gates turns findings into failure reasons."""

    def test_no_gates_never_fail(self, result):
        assert evaluate_gates(result) == []

    def test_flaky_gate(self, result):
        assert evaluate_gates(result, {"flaky"}) == ["1 flaky step: integration / e2e"]

    def test_regression_gate(self, result):
        assert evaluate_gates(result, {"regression"}) == ["1 regressed step: build / Run tests"]

    def test_both_gates_report_both_reasons(self, result):
        assert len(evaluate_gates(result, {"flaky", "regression"})) == 2

    def test_gates_pass_without_findings(self):
        clean = AnalysisResult(mode="log", metadata={"build_p50_seconds": 60.0})

        assert evaluate_gates(clean, {"flaky", "regression"}, max_duration=120.0) == []

    def test_max_duration_uses_the_median_build(self, result):
        # Builds take 120s (first half) and 170s (second half); median 120s
        assert result.metadata["build_p50_seconds"] == 120.0
        assert evaluate_gates(result, max_duration=120.0) == []
        assert evaluate_gates(result, max_duration=100.0) == [
            "median build duration 2m 00s exceeds the 1m 40s limit"
        ]

    def test_max_duration_without_build_durations_is_not_checked(self):
        assert evaluate_gates(AnalysisResult(mode="log"), max_duration=1.0) == []

    def test_long_lists_are_summarized(self):
        many = AnalysisResult(mode="log", flaky_steps=[f"step {i}" for i in range(8)])

        assert evaluate_gates(many, {"flaky"}) == [
            "8 flaky steps: step 0, step 1, step 2, step 3, step 4 and 3 more"
        ]

    def test_superseded_attempts_do_not_count_as_builds(self):
        logs = [
            BuildLog(build_id="1", total_duration=60.0),
            BuildLog(build_id="1", total_duration=999.0, metadata={"superseded_attempt": True}),
        ]

        assert analyze_logs(logs).metadata["build_p50_seconds"] == 60.0


class TestGateOptions:
    """Parsing --fail-on and --max-duration."""

    def test_defaults_gate_nothing(self):
        args = parse_args(["--logs", "x"])

        assert args.fail_on == set()
        assert args.max_duration is None

    def test_fail_on_accepts_a_comma_separated_list(self):
        args = parse_args(["--logs", "x", "--fail-on", "flaky, Regression"])

        assert args.fail_on == {"flaky", "regression"}

    @pytest.mark.parametrize("value", ["slow", "flaky,typo", ","])
    def test_fail_on_rejects_unknown_findings(self, value):
        with pytest.raises(SystemExit):
            parse_args(["--logs", "x", "--fail-on", value])

    @pytest.mark.parametrize("value", ["0", "-1", "soon"])
    def test_max_duration_must_be_positive(self, value):
        with pytest.raises(SystemExit):
            parse_args(["--logs", "x", "--max-duration", value])


class TestGateExitCode:
    """main() exits with 4 when a gate fails, after writing the report."""

    def _run(self, monkeypatch, *extra):
        monkeypatch.setattr("sys.argv", ["ci-time-tracker", "--github", "acme/web", "--no-cache", *extra])
        monkeypatch.setattr(
            "ci_time_tracker.cli.fetch_build_logs",
            lambda **kwargs: regressed_and_flaky_logs(),
        )
        return main()

    def test_failing_gate_exits_4_and_still_prints_the_report(self, monkeypatch, capsys):
        exit_code = self._run(monkeypatch, "--fail-on", "flaky")
        captured = capsys.readouterr()

        assert exit_code == 4
        assert "TOP TIME CONSUMERS" in captured.out
        assert "Gate failed:" in captured.err
        assert "integration / e2e" in captured.err

    def test_passing_gate_exits_0(self, monkeypatch):
        assert self._run(monkeypatch, "--max-duration", "600") == 0

    def test_findings_without_gates_exit_0(self, monkeypatch):
        """Reporting issues is not failing; only --fail-on fails the run."""
        assert self._run(monkeypatch) == 0

    def test_max_duration_without_data_warns(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", [
            "ci-time-tracker", "--github", "acme/web", "--no-cache", "--max-duration", "1",
        ])
        # A build without a total duration gives the gate nothing to judge
        monkeypatch.setattr(
            "ci_time_tracker.cli.fetch_build_logs",
            lambda **kwargs: [BuildLog(steps=[StepExecution(name="s", duration_seconds=1.0)])],
        )

        assert main() == 0
        assert "--max-duration not checked" in capsys.readouterr().err


class TestMarkdown:
    """Markdown output for run summaries and PR comments."""

    @pytest.fixture
    def markdown(self, result):
        return format_markdown(generate_report(result))

    def test_headline_numbers(self, markdown):
        assert markdown.startswith("## CI Time Tracker Report (log mode)")
        assert "**22** builds" in markdown
        assert "median build **2m 00s**" in markdown

    def test_issues_come_with_their_evidence(self, markdown):
        assert "- ⚠️ Regression: `build / Run tests`" in markdown
        assert "  - Median 1m 40s -> 2m 30s (+50%, +50.0s)" in markdown
        assert "- ❌ Flaky: `integration / e2e`" in markdown
        assert "  - Failed and passed on the same commit in 1 of 1 commits" in markdown

    def test_clean_run_says_so(self):
        logs = [BuildLog(steps=[StepExecution(name="s", duration_seconds=1.0)])]

        assert "✅ No flaky steps or regressions found." in format_markdown(generate_report(analyze_logs(logs)))

    def test_top_consumers_table(self, markdown):
        assert "| # | Step | Total | Share | Runs | Cost |" in markdown
        assert "| 1 | build / Run tests | 41m 40s |" in markdown

    def test_step_table_is_collapsed_and_flagged(self, markdown):
        assert "<details><summary>All steps (2)</summary>" in markdown
        assert "</details>" in markdown
        assert "| build / Run tests | 20 |" in markdown
        assert "| REGR |" in markdown
        assert "| FLAKY |" in markdown

    def test_top_limits_the_step_table(self, result):
        markdown = format_markdown(generate_report(result), top=1)

        assert "…and 1 more steps." in markdown

    def test_pipes_in_step_names_do_not_break_tables(self):
        logs = [BuildLog(steps=[StepExecution(name="a | b", duration_seconds=3.0)])]
        markdown = format_markdown(generate_report(analyze_logs(logs)))

        assert "a \\| b" in markdown

    def test_method_notes(self, markdown):
        assert "flaky = failed and passed on the same commit" in markdown
        assert "regressions checked on 1 steps with ≥10 builds" in markdown

    def test_config_mode_table(self):
        result = AnalysisResult(
            mode="config",
            steps=[PipelineStep(name="Build", stage="build", estimated_duration=90.0)],
        )

        markdown = format_markdown(generate_report(result))

        assert "| Step | Stage | Estimate |" in markdown
        assert "| Build | build | 1m 30s |" in markdown

    def test_output_survives_a_cp1252_stdout(self, monkeypatch):
        """Windows pipes and redirects default to cp1252, which has no emoji.

        Redirecting into $GITHUB_STEP_SUMMARY on a Windows runner crashed with
        a 'charmap' codec error before output was forced to UTF-8.
        """
        import io
        import sys

        raw = io.BytesIO()
        windows_pipe = io.TextIOWrapper(raw, encoding="cp1252")
        monkeypatch.setattr(sys, "stdout", windows_pipe)
        monkeypatch.setattr("sys.argv", [
            "ci-time-tracker", "--github", "acme/web", "--no-cache", "--format", "markdown",
        ])
        monkeypatch.setattr(
            "ci_time_tracker.cli.fetch_build_logs",
            lambda **kwargs: regressed_and_flaky_logs(),
        )

        assert main() == 0
        windows_pipe.flush()
        assert "❌ Flaky" in raw.getvalue().decode("utf-8")

    def test_cli_markdown_format(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", [
            "ci-time-tracker", "--github", "acme/web", "--no-cache", "--format", "markdown",
        ])
        monkeypatch.setattr(
            "ci_time_tracker.cli.fetch_build_logs",
            lambda **kwargs: regressed_and_flaky_logs(),
        )

        assert main() == 0
        assert capsys.readouterr().out.startswith("## CI Time Tracker Report")
