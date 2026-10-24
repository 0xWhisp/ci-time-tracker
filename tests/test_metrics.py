"""Tests for consumed time, cost estimates and queue metrics.

Covers the pricing table, the analyzer's aggregates, matrix grouping and how
the reporter presents them.
"""

import json

import pytest

from ci_time_tracker.analyzer import analyze_logs, normalize_matrix_name
from ci_time_tracker.models import BuildLog, StepExecution
from ci_time_tracker.pricing import (
    DEFAULT_PRICES_USD_PER_MINUTE,
    PricingTable,
    attributed_cost,
    job_cost,
    load_pricing,
)
from ci_time_tracker.reporter import (
    format_cost,
    format_duration,
    format_text,
    generate_report,
)


def build(steps, jobs=None, **kwargs):
    """A BuildLog with the given steps and optional job metadata."""
    metadata = {"jobs": jobs} if jobs is not None else {}
    return BuildLog(steps=steps, metadata=metadata, **kwargs)


def step(name, duration, runner="ubuntu-latest", status="success"):
    return StepExecution(
        name=name,
        duration_seconds=duration,
        status=status,
        runner=runner,
        job_name=name.split(" / ")[0],
    )


class TestPricingTable:
    """Runner label to USD-per-minute lookups."""

    def test_known_runner_labels(self):
        table = PricingTable()

        assert table.price_for("ubuntu-latest") == 0.008
        assert table.price_for("windows-latest") == 0.016
        assert table.price_for("macos-latest") == 0.08

    def test_label_matching_is_case_insensitive(self):
        """GitHub reports labels like "macOS-latest"."""
        assert PricingTable().price_for("macOS-latest") == 0.08

    def test_first_known_label_of_a_list_wins(self):
        assert PricingTable().price_for("ubuntu-latest, x64") == 0.008

    def test_self_hosted_is_free(self):
        """A self-hosted runner costs nothing on GitHub's bill."""
        assert PricingTable().price_for("self-hosted") == 0.0
        assert PricingTable().price_for("self-hosted, linux, x64") == 0.0

    def test_unknown_runner_returns_none_not_zero(self):
        """An unknown rate must not be silently treated as free."""
        assert PricingTable().price_for("mystery-runner") is None
        assert PricingTable().price_for(None) is None
        assert PricingTable().price_for("") is None

    def test_larger_runners_priced_by_core_count(self):
        """Linux and Windows rates scale linearly with cores."""
        table = PricingTable()

        assert table.price_for("ubuntu-latest-8-cores") == pytest.approx(0.032)
        assert table.price_for("windows-latest-4-cores") == pytest.approx(0.032)
        assert table.price_for("linux-16-core") == pytest.approx(0.064)

    def test_core_count_without_known_platform_is_unknown(self):
        assert PricingTable().price_for("bespoke-8-cores") is None


class TestLoadPricing:
    """Overriding rates from a JSON file."""

    def test_file_rates_merge_over_defaults(self, tmp_path):
        path = tmp_path / "pricing.json"
        path.write_text(json.dumps({"my-runner": 0.05, "ubuntu-latest": 0.01}), encoding="utf-8")

        table = load_pricing(path)

        assert table.price_for("my-runner") == 0.05
        assert table.price_for("ubuntu-latest") == 0.01  # overridden
        assert table.price_for("macos-latest") == DEFAULT_PRICES_USD_PER_MINUTE["macos-latest"]
        assert str(path) in table.source

    def test_labels_are_normalized_to_lowercase(self, tmp_path):
        path = tmp_path / "pricing.json"
        path.write_text(json.dumps({"My-Runner": 0.02}), encoding="utf-8")

        assert load_pricing(path).price_for("my-runner") == 0.02

    def test_invalid_json_is_rejected(self, tmp_path):
        path = tmp_path / "pricing.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(ValueError, match="Invalid JSON"):
            load_pricing(path)

    def test_non_object_is_rejected(self, tmp_path):
        path = tmp_path / "pricing.json"
        path.write_text("[1, 2]", encoding="utf-8")

        with pytest.raises(ValueError, match="JSON object"):
            load_pricing(path)

    def test_non_numeric_and_negative_prices_are_rejected(self, tmp_path):
        path = tmp_path / "pricing.json"

        path.write_text(json.dumps({"a": "cheap"}), encoding="utf-8")
        with pytest.raises(ValueError, match="must be a number"):
            load_pricing(path)

        path.write_text(json.dumps({"a": -1}), encoding="utf-8")
        with pytest.raises(ValueError, match="cannot be negative"):
            load_pricing(path)


class TestCostArithmetic:
    """Per-job rounding versus per-step attribution."""

    def test_job_cost_rounds_up_to_the_next_minute(self):
        """GitHub bills each job rounded up to a whole minute."""
        assert job_cost(61.0, 0.008) == pytest.approx(0.016)
        assert job_cost(60.0, 0.008) == pytest.approx(0.008)
        assert job_cost(1.0, 0.008) == pytest.approx(0.008)

    def test_job_cost_of_nothing_is_zero(self):
        assert job_cost(0.0, 0.008) == 0.0

    def test_job_cost_needs_both_inputs(self):
        assert job_cost(None, 0.008) is None
        assert job_cost(60.0, None) is None

    def test_attributed_cost_does_not_round(self):
        """Step attribution is proportional, so rankings stay comparable."""
        assert attributed_cost(90.0, 0.008) == pytest.approx(0.012)
        assert attributed_cost(None, 0.008) is None
        assert attributed_cost(90.0, None) is None


class TestConsumedTime:
    """Total and mean duration per step."""

    def test_total_duration_sums_every_execution(self):
        logs = [
            build([step("test / Run tests", 100.0)]),
            build([step("test / Run tests", 200.0)]),
        ]

        result = analyze_logs(logs)
        stats = result.steps[0]

        assert stats.total_duration == 300.0
        assert stats.mean_duration == 150.0
        assert stats.execution_count == 2

    def test_total_execution_seconds_in_summary(self):
        logs = [build([step("a / one", 10.0), step("b / two", 20.0)])]

        assert analyze_logs(logs).metadata["total_execution_seconds"] == 30.0

    def test_steps_without_durations_have_no_mean(self):
        logs = [build([StepExecution(name="pending", duration_seconds=None)])]
        stats = analyze_logs(logs).steps[0]

        assert stats.total_duration == 0.0
        assert stats.mean_duration is None


class TestCostEstimates:
    """Cost attribution and the honest handling of unknown runners."""

    def test_step_cost_uses_its_runner_rate(self):
        logs = [build([step("test / Run tests", 600.0, runner="ubuntu-latest")])]
        stats = analyze_logs(logs).steps[0]

        # 10 minutes at $0.008/min
        assert stats.estimated_cost_usd == pytest.approx(0.08)
        assert stats.runner == "ubuntu-latest"

    def test_unknown_runner_leaves_cost_unset(self):
        logs = [build([step("test / Run tests", 600.0, runner="mystery-runner")])]
        result = analyze_logs(logs)

        assert result.steps[0].estimated_cost_usd is None

    def test_billed_cost_rounds_each_job_up(self):
        """The build total follows GitHub's per-job rounding."""
        jobs = [
            {"name": "build", "runner": "ubuntu-latest", "duration_seconds": 61.0},
            {"name": "test", "runner": "windows-latest", "duration_seconds": 30.0},
        ]
        logs = [build([step("build / x", 61.0)], jobs=jobs)]

        metadata = analyze_logs(logs).metadata

        # 2 min x $0.008 + 1 min x $0.016
        assert metadata["estimated_cost_usd"] == pytest.approx(0.032)
        assert metadata["priced_jobs"] == 2
        assert metadata["job_count"] == 2

    def test_unpriced_runners_are_reported(self):
        """The report must say the estimate is incomplete, and why."""
        jobs = [{"name": "build", "runner": "mystery-runner", "duration_seconds": 60.0}]
        logs = [build([step("build / x", 60.0, runner="mystery-runner")], jobs=jobs)]

        metadata = analyze_logs(logs).metadata

        assert metadata["unpriced_runners"] == ["mystery-runner"]
        assert "estimated_cost_usd" not in metadata

    def test_custom_pricing_table_is_used(self):
        logs = [build([step("test / Run tests", 60.0, runner="mystery-runner")])]
        pricing = PricingTable(prices={"mystery-runner": 1.0}, source="test table")

        result = analyze_logs(logs, pricing=pricing)

        assert result.steps[0].estimated_cost_usd == pytest.approx(1.0)

    def test_self_hosted_jobs_cost_zero_but_are_priced(self):
        jobs = [{"name": "build", "runner": "self-hosted, linux", "duration_seconds": 600.0}]
        logs = [build([step("build / x", 600.0, runner="self-hosted, linux")], jobs=jobs)]

        metadata = analyze_logs(logs).metadata

        assert metadata["estimated_cost_usd"] == 0.0
        assert "unpriced_runners" not in metadata


class TestQueueTime:
    """Queue time comes from job metadata, not from step durations."""

    def test_queue_totals_and_percentiles(self):
        jobs = [
            {"name": "a", "runner": "ubuntu-latest", "queued_seconds": 10.0, "duration_seconds": 60.0},
            {"name": "b", "runner": "ubuntu-latest", "queued_seconds": 30.0, "duration_seconds": 60.0},
        ]
        logs = [build([step("a / x", 60.0)], jobs=jobs)]

        metadata = analyze_logs(logs).metadata

        assert metadata["total_queue_seconds"] == 40.0
        assert metadata["queue_p50_seconds"] == 10.0
        assert metadata["queue_p90_seconds"] == 30.0

    def test_text_logs_report_no_queue_or_cost(self):
        """Log files carry no job data, so those metrics stay absent."""
        logs = [build([step("build", 60.0, runner=None)])]

        metadata = analyze_logs(logs).metadata

        assert "total_queue_seconds" not in metadata
        assert "job_count" not in metadata
        assert metadata["build_count"] == 1


class TestMatrixGrouping:
    """Merging the legs of a matrix job."""

    def test_normalize_strips_job_parameters_only(self):
        assert normalize_matrix_name("build (3.12, ubuntu) / Run tests") == "build / Run tests"
        # Parentheses in the step's own name are preserved
        assert normalize_matrix_name("build / Run tests (unit)") == "build / Run tests (unit)"
        assert normalize_matrix_name("build (3.12)") == "build"
        assert normalize_matrix_name("plain") == "plain"

    def test_legs_stay_separate_by_default(self):
        logs = [build([
            step("build (3.12, ubuntu-latest) / Run tests", 100.0),
            step("build (3.13, ubuntu-latest) / Run tests", 200.0),
        ])]

        result = analyze_logs(logs)

        assert len(result.steps) == 2

    def test_grouping_merges_legs_into_one_step(self):
        logs = [build([
            step("build (3.12, ubuntu-latest) / Run tests", 100.0),
            step("build (3.13, ubuntu-latest) / Run tests", 200.0),
        ])]

        result = analyze_logs(logs, group_matrix=True)

        assert len(result.steps) == 1
        stats = result.steps[0]
        assert stats.name == "build / Run tests"
        assert stats.execution_count == 2
        assert stats.total_duration == 300.0
        assert result.metadata["grouping"] == "matrix legs merged"


class TestReportPresentation:
    """How the report orders and formats the new metrics."""

    def _report(self):
        logs = [build(
            [
                step("build / Run tests", 300.0),
                step("build / Checkout", 5.0),
                step("build / Install", 100.0),
            ],
            jobs=[{
                "name": "build",
                "runner": "ubuntu-latest",
                "queued_seconds": 12.0,
                "duration_seconds": 405.0,
            }],
        )]
        return generate_report(analyze_logs(logs))

    def test_steps_are_ordered_by_consumed_time(self):
        """Biggest contributors first, in every output format."""
        report = self._report()

        assert [s["name"] for s in report.steps] == [
            "build / Run tests",
            "build / Install",
            "build / Checkout",
        ]

    def test_top_consumers_section_shows_share(self):
        text = format_text(self._report())

        assert "TOP TIME CONSUMERS" in text
        assert "build / Run tests" in text
        assert "74.1%" in text  # 300 of 405 seconds

    def test_summary_formats_durations_and_cost(self):
        text = format_text(self._report())

        assert "total_execution_seconds: 6m 45s" in text
        assert "total_queue_seconds: 12.0s" in text
        assert "estimated_cost_usd: $0.06" in text  # 7 rounded-up minutes
        assert "private-repository rates" in text

    def test_top_limits_the_table_and_says_so(self):
        text = format_text(self._report(), top=1)

        assert "and 2 more steps" in text

    def test_no_truncation_notice_without_top(self):
        assert "more steps" not in format_text(self._report())

    def test_cost_column_shown_per_step(self):
        text = format_text(self._report())

        assert "Cost" in text
        # 300s at $0.008/min, attributed without rounding
        assert "$0.04" in text

    def test_format_duration_units(self):
        assert format_duration(None) == "N/A"
        assert format_duration(45.0) == "45.0s"
        assert format_duration(90.0) == "1m 30s"
        assert format_duration(3900.0) == "1h 05m"

    def test_format_cost_units(self):
        assert format_cost(None) == "N/A"
        assert format_cost(0.0) == "$0.00"
        assert format_cost(1234.5) == "$1,234.50"
