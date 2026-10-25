"""Tests for the self-contained HTML report."""

import re
from datetime import datetime, timedelta, timezone

import pytest

from ci_time_tracker.analyzer import analyze_logs
from ci_time_tracker.cli import main
from ci_time_tracker.html_report import _axis, _tick_label, format_html
from ci_time_tracker.models import AnalysisResult, BuildLog, PipelineStep, StepExecution
from ci_time_tracker.reporter import generate_report

START = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)


def build(index, duration, workflow="CI", steps=None, conclusion="success"):
    return BuildLog(
        build_id=str(index),
        head_sha=f"{index:040x}",
        steps=steps if steps is not None else [StepExecution(name="build / Run tests", duration_seconds=duration)],
        total_duration=duration,
        conclusion=conclusion,
        timestamp=START + timedelta(hours=index),
        metadata={
            "workflow_name": workflow,
            "url": f"https://github.com/acme/web/actions/runs/{index}",
        },
    )


def page(logs, **summary_extra):
    result = analyze_logs(logs)
    result.metadata.update(summary_extra)
    return format_html(generate_report(result))


class TestDocument:
    def test_complete_self_contained_document(self):
        html = page([build(i, 600.0) for i in range(6)], repository="acme/web")

        assert html.startswith("<!DOCTYPE html>")
        assert "<title>CI time report: acme/web</title>" in html
        assert "prefers-color-scheme: dark" in html
        # No scripts and nothing loaded from elsewhere: it works offline
        assert "<script" not in html.lower()
        assert not re.search(r'<(link|img|iframe)\b|\bsrc=', html)

    def test_data_is_escaped(self):
        """Step and workflow names come from pull requests: never trust them."""
        hostile = '<script>alert("x")</script>'
        logs = [
            build(i, 600.0, workflow=hostile, steps=[StepExecution(name=hostile, duration_seconds=600.0)])
            for i in range(6)
        ]

        html = page(logs, repository=hostile)

        assert "<script>" not in html
        assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in html


class TestSummaryCards:
    def test_cards(self):
        html = page([build(i, 600.0) for i in range(6)])

        assert '<div class="label">Builds</div><div class="value">6</div>' in html
        assert '<div class="label">Median build</div><div class="value">10m 00s</div>' in html

    def test_median_build_says_when_it_spans_workflows(self):
        logs = [build(i, 600.0) for i in range(4)] + [build(10 + i, 10.0, workflow="Lock") for i in range(4)]

        assert "across 2 workflows" in page(logs)

    def test_public_repository_cost_note(self):
        logs = [build(i, 600.0) for i in range(3)]
        for log in logs:
            log.metadata["jobs"] = [{"name": "b", "runner": "ubuntu-latest", "duration_seconds": 600.0}]

        html = page(logs, repository_visibility="public")

        assert "public repository: standard runners are free" in html


class TestBuildDurationCharts:
    def test_one_chart_per_workflow(self):
        """A 10-second workflow next to a 10-minute one would only zigzag."""
        logs = [build(i, 600.0) for i in range(6)] + [build(20 + i, 10.0, workflow="Lock") for i in range(6)]

        html = page(logs)

        assert html.count('aria-label="Build duration over time"') == 2
        # The workflow with more time comes first
        assert html.index("<h3>CI") < html.index("<h3>Lock")

    def test_outlier_does_not_flatten_the_chart(self):
        durations = [600.0 + i for i in range(19)] + [12000.0]
        logs = [build(i, duration) for i, duration in enumerate(durations)]

        html = page(logs)

        assert "1 build above the chart, up to 3h 20m (▲)" in html
        assert "<polygon" in html  # the outlier, at the top edge

    def test_points_link_to_their_runs_and_say_what_they_are(self):
        html = page([build(i, 600.0) for i in range(3)])

        assert '<a href="https://github.com/acme/web/actions/runs/1">' in html
        assert "build 1 · 10m 00s · success" in html

    def test_failures_are_marked(self):
        logs = [build(i, 600.0) for i in range(3)] + [build(3, 600.0, conclusion="failure")]

        assert 'class="fail"' in page(logs)

    def test_single_build_is_not_charted(self):
        assert "Not enough builds with a duration to chart." in page([build(1, 600.0)])


class TestAxis:
    @pytest.mark.parametrize("durations, top, step", [
        ([600.0, 610.0, 620.0], 900.0, 300.0),  # ~11 min of room, 5-minute ticks
        ([12.0, 14.0, 15.0], 20.0, 5.0),
    ])
    def test_round_ticks(self, durations, top, step):
        assert _axis(durations) == (top, step)

    def test_outliers_are_left_above_the_axis(self):
        top, _ = _axis([600.0] * 19 + [12000.0])

        assert top < 12000.0

    @pytest.mark.parametrize("seconds, label", [
        (0, "0s"), (30, "30s"), (300, "5m"), (3600, "1h"), (5400, "1h30m"),
    ])
    def test_compact_tick_labels(self, seconds, label):
        assert _tick_label(seconds) == label


class TestSections:
    def test_issues_link_to_the_first_slow_build(self):
        logs = [build(i, 100.0 if i < 10 else 150.0) for i in range(20)]
        for log in logs:
            log.steps = [StepExecution(name="build / Run tests", duration_seconds=log.total_duration)]

        html = page(logs)

        assert "Regression:" in html
        assert '<a href="https://github.com/acme/web/actions/runs/10">The first slow build</a>' in html

    def test_clean_run(self):
        assert "No flaky steps or regressions found." in page([build(i, 600.0) for i in range(3)])

    def test_recommendations_with_a_yaml_snippet(self):
        result = analyze_logs([build(i, 600.0) for i in range(3)])
        result.recommendations = [{
            "kind": "cancel-superseded-runs", "title": "Cancel superseded runs", "workflow": ".github/workflows/ci.yml",
            "job": None, "evidence": "3 runs kept running.", "suggestion": "Add:\nconcurrency:\n  group: x",
            "time_at_stake_seconds": 900.0, "cost_at_stake_usd": 1.5,
        }]

        html = format_html(generate_report(result))

        assert "<h3>Cancel superseded runs</h3>" in html
        assert "<pre>concurrency:\n  group: x</pre>" in html
        assert "At stake: 15m 00s, $1.50" in html

    def test_step_table_respects_top(self):
        steps = [StepExecution(name=f"build / step {i}", duration_seconds=float(i + 1)) for i in range(5)]
        result = analyze_logs([build(1, 600.0, steps=steps)])

        html = format_html(generate_report(result), top=2)

        assert "All steps (5)" in html
        assert "…and 3 more steps." in html

    def test_config_mode(self):
        report = generate_report(AnalysisResult(
            mode="config", steps=[PipelineStep(name="Build", stage="build", estimated_duration=90.0)]
        ))

        html = format_html(report)

        assert "<td>Build</td><td>build</td><td>1m 30s</td>" in html


class TestTimelineData:
    def test_timeline_in_the_json(self):
        result = analyze_logs([build(i, 600.0) for i in range(2)])
        entry = generate_report(result).to_dict()["timeline"][0]

        assert entry == {
            "build_id": "0",
            "timestamp": "2026-10-01T10:00:00+00:00",
            "duration": 600.0,
            "conclusion": "success",
            "head_sha": "0000000",
            "workflow": "CI",
            "branch": None,
            "url": "https://github.com/acme/web/actions/runs/0",
        }

    def test_superseded_attempts_are_not_builds(self):
        logs = [build(1, 600.0), build(1, 300.0)]
        logs[0].metadata["superseded_attempt"] = True

        assert [entry["duration"] for entry in analyze_logs(logs).timeline] == [300.0]


class TestCli:
    def test_html_format(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["ci-time-tracker", "--github", "acme/web", "--no-cache", "--format", "html"])
        monkeypatch.setattr("ci_time_tracker.cli.fetch_build_logs", lambda **kwargs: [build(i, 600.0) for i in range(4)])

        assert main() == 0
        out = capsys.readouterr().out
        assert out.startswith("<!DOCTYPE html>")
        assert "CI time report: acme/web" in out
