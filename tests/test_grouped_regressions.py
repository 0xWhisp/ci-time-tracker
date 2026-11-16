"""Tests for merging regressions that started at the same commit."""

import json
from datetime import datetime, timedelta, timezone

from ci_time_tracker.analyzer import analyze_logs
from ci_time_tracker.html_report import format_html
from ci_time_tracker.models import BuildLog, StepExecution
from ci_time_tracker.reporter import (
    format_json,
    format_markdown,
    format_text,
    generate_report,
    group_issues,
)

START = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


def shared_cause_logs():
    """20 builds: at build 10, "Run tests" and "Lint" slow down; "Docs" slows at 14."""
    logs = []
    for index in range(20):
        steps = [
            StepExecution(name="build / Run tests", duration_seconds=100.0 if index < 10 else 160.0),
            StepExecution(name="build / Lint", duration_seconds=40.0 if index < 10 else 60.0),
            StepExecution(name="docs / Build", duration_seconds=50.0 if index < 14 else 90.0),
        ]
        logs.append(BuildLog(
            build_id=str(index),
            head_sha=f"c{index:06x}" + "0" * 33,
            steps=steps,
            total_duration=sum(step.duration_seconds for step in steps),
            timestamp=START + timedelta(hours=index),
            metadata={"workflow_name": "CI", "url": f"https://github.com/acme/web/actions/runs/{index}"},
        ))
    return logs


def report():
    return generate_report(analyze_logs(shared_cause_logs(), min_samples=5), title="CI")


class TestGroupIssues:
    def test_regressions_from_one_commit_become_one_issue(self):
        grouped = [issue for issue in group_issues(report().issues) if issue["type"] == "regression"]

        assert len(grouped) == 2
        merged = next(issue for issue in grouped if issue.get("steps"))
        assert merged["step"] == "2 steps"
        assert merged["started_at"]["build_id"] == "10"
        assert [step["step"] for step in merged["steps"]] == ["build / Run tests", "build / Lint"]

    def test_a_lone_regression_is_left_as_it_is(self):
        issues = report().issues
        lone = next(issue for issue in issues if issue["step"] == "docs / Build")

        assert lone in group_issues(issues)

    def test_regressions_without_an_origin_are_not_merged(self):
        issues = [{"type": "regression", "step": name} for name in ("a", "b")]

        assert group_issues(issues) == issues

    def test_json_keeps_one_issue_per_step(self):
        issues = json.loads(format_json(report()))["issues"]

        assert sorted(issue["step"] for issue in issues if issue["type"] == "regression") == [
            "build / Lint", "build / Run tests", "docs / Build",
        ]


class TestRendering:
    def test_text(self):
        text = format_text(report())

        assert "REGRESSION: 2 steps" in text
        assert "Started together at commit c00000a (build 10, 2026-10-01)" in text
        assert "build / Run tests: 1m 40s -> 2m 40s (+60%, +1m 00s)" in text
        assert "REGRESSION: docs / Build" in text

    def test_markdown(self):
        markdown = format_markdown(report())

        assert "- ⚠️ Regression: 2 steps\n" in markdown
        assert "  - build / Lint: 40.0s -> 1m 00s (+50%, +20.0s)" in markdown
        assert "- ⚠️ Regression: `docs / Build`" in markdown

    def test_html_links_the_shared_first_slow_build(self):
        html = format_html(report())

        assert "<h3>Regression: 2 steps</h3>" in html
        assert '<a href="https://github.com/acme/web/actions/runs/10">The first slow build</a>' in html
