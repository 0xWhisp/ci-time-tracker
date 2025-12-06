"""Property-based tests for reporter module.

Tests the Report JSON serialization/deserialization round-trip property
and report completeness.
"""

import json
from datetime import datetime, timezone

from hypothesis import given, settings, strategies as st

from ci_time_tracker.models import (
    AnalysisResult,
    PipelineStep,
    Report,
    StepStatistics,
)
from ci_time_tracker.reporter import (
    generate_report,
    format_json,
    format_text,
    parse_json_report,
)


# Hypothesis strategies for generating valid Report components

# Generate valid step names (non-empty strings with reasonable characters)
step_names = st.text(
    alphabet=st.characters(
        whitelist_categories=('L', 'N', 'Pd', 'Pc'),
        whitelist_characters='-_'
    ),
    min_size=1,
    max_size=50
).filter(lambda s: s.strip())

# Generate valid report titles
report_titles = st.text(
    alphabet=st.characters(whitelist_categories=('L', 'N', 'P', 'Z')),
    min_size=1,
    max_size=100
).filter(lambda s: s.strip())

# Generate valid mode strings
report_modes = st.sampled_from(["config", "log"])

# Generate valid datetime objects (with timezone info for consistent serialization)
valid_datetimes = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 12, 31),
    timezones=st.just(timezone.utc)
)

# Generate JSON-serializable primitive values
json_primitives = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-2**31, max_value=2**31),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=100),
)

# Generate JSON-serializable dictionaries (limited depth to avoid recursion issues)
json_dicts = st.dictionaries(
    keys=st.text(min_size=1, max_size=20).filter(lambda s: s.strip()),
    values=json_primitives,
    max_size=10
)

# Generate step dictionaries (representing step data in reports)
step_dicts = st.fixed_dictionaries({
    "name": step_names,
}).map(lambda d: {
    **d,
    **{k: v for k, v in {
        "execution_count": st.integers(min_value=0, max_value=1000).example(),
        "success_count": st.integers(min_value=0, max_value=1000).example(),
        "p50": st.floats(min_value=0, max_value=3600, allow_nan=False).example(),
    }.items()}
})

# Simpler step dict strategy
simple_step_dicts = st.fixed_dictionaries({
    "name": step_names,
    "execution_count": st.integers(min_value=0, max_value=1000),
    "success_count": st.integers(min_value=0, max_value=1000),
    "failure_count": st.integers(min_value=0, max_value=1000),
    "p50": st.floats(min_value=0, max_value=3600, allow_nan=False, allow_infinity=False),
    "p90": st.floats(min_value=0, max_value=3600, allow_nan=False, allow_infinity=False),
    "is_slow": st.booleans(),
    "is_flaky": st.booleans(),
    "failure_rate": st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
})

# Generate issue dictionaries
issue_dicts = st.fixed_dictionaries({
    "type": st.sampled_from(["slow", "flaky"]),
    "step": step_names,
    "message": st.text(min_size=1, max_size=200).filter(lambda s: s.strip()),
})

# Generate summary dictionaries
summary_dicts = st.fixed_dictionaries({
    "total_builds": st.integers(min_value=0, max_value=10000),
    "total_steps": st.integers(min_value=0, max_value=100),
    "slow_steps_count": st.integers(min_value=0, max_value=100),
    "flaky_steps_count": st.integers(min_value=0, max_value=100),
})

# Generate complete Report objects
report_strategy = st.builds(
    Report,
    title=report_titles,
    mode=report_modes,
    generated_at=valid_datetimes,
    summary=summary_dicts,
    steps=st.lists(simple_step_dicts, max_size=10),
    issues=st.lists(issue_dicts, max_size=5),
)


class TestReportJsonRoundTrip:
    """Property tests for Report JSON serialization round-trip.
    
    **Feature: ci-time-tracker, Property 6: Report JSON round-trip**
    """

    @given(report=report_strategy)
    @settings(max_examples=100)
    def test_report_json_round_trip(self, report: Report) -> None:
        """
        **Feature: ci-time-tracker, Property 6: Report JSON round-trip**
        
        *For any* valid Report object, serializing to JSON and then 
        deserializing SHALL produce a Report object equivalent to the original.
        
        **Validates: Requirements 5.5**
        """
        # Serialize to dict, then to JSON string
        report_dict = report.to_dict()
        json_str = json.dumps(report_dict)
        
        # Deserialize from JSON string back to dict, then to Report
        parsed_dict = json.loads(json_str)
        restored_report = Report.from_dict(parsed_dict)
        
        # Verify equivalence
        assert restored_report.title == report.title
        assert restored_report.mode == report.mode
        assert restored_report.generated_at == report.generated_at
        assert restored_report.summary == report.summary
        assert restored_report.steps == report.steps
        assert restored_report.issues == report.issues


# Additional strategies for report completeness tests

# Generate valid StepStatistics
step_statistics_strategy = st.builds(
    StepStatistics,
    name=step_names,
    execution_count=st.integers(min_value=1, max_value=100),
    success_count=st.integers(min_value=0, max_value=100),
    failure_count=st.integers(min_value=0, max_value=100),
    durations=st.lists(
        st.floats(min_value=0.1, max_value=3600, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=50
    ),
    p50=st.floats(min_value=0.1, max_value=3600, allow_nan=False, allow_infinity=False),
    p90=st.floats(min_value=0.1, max_value=3600, allow_nan=False, allow_infinity=False),
    p95=st.floats(min_value=0.1, max_value=3600, allow_nan=False, allow_infinity=False),
    p99=st.floats(min_value=0.1, max_value=3600, allow_nan=False, allow_infinity=False),
    is_slow=st.booleans(),
    is_flaky=st.booleans(),
    failure_rate=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)

# Generate valid PipelineStep
pipeline_step_strategy = st.builds(
    PipelineStep,
    name=step_names,
    stage=st.one_of(st.none(), st.text(min_size=1, max_size=20)),
    command=st.one_of(st.none(), st.text(min_size=1, max_size=100)),
    estimated_duration=st.one_of(
        st.none(),
        st.floats(min_value=0.1, max_value=3600, allow_nan=False, allow_infinity=False)
    ),
)


class TestReportCompleteness:
    """Property tests for report completeness.
    
    **Feature: ci-time-tracker, Property 7: Report completeness**
    """

    @given(
        step_stats=st.lists(step_statistics_strategy, min_size=1, max_size=20)
    )
    @settings(max_examples=100)
    def test_log_mode_report_contains_all_steps_with_required_fields(
        self, step_stats: list[StepStatistics]
    ) -> None:
        """
        **Feature: ci-time-tracker, Property 7: Report completeness**
        
        *For any* analysis result in log mode, the generated report SHALL contain
        all steps with all required fields (execution counts, percentiles, flags).
        
        **Validates: Requirements 5.1, 5.2, 5.3**
        """
        # Create analysis result in log mode
        result = AnalysisResult(
            mode="log",
            steps=step_stats,
            slowest_steps=[s.name for s in step_stats if s.is_slow],
            flaky_steps=[s.name for s in step_stats if s.is_flaky],
            metadata={"build_count": len(step_stats)},
        )
        
        # Generate report
        report = generate_report(result)
        
        # Verify report has all steps
        assert len(report.steps) == len(step_stats), \
            f"Report should contain {len(step_stats)} steps, but has {len(report.steps)}"
        
        # Verify each step has required fields
        for i, step_dict in enumerate(report.steps):
            original_step = step_stats[i]
            
            # Required fields
            assert "name" in step_dict
            assert step_dict["name"] == original_step.name
            
            assert "execution_count" in step_dict
            assert step_dict["execution_count"] == original_step.execution_count
            
            assert "success_count" in step_dict
            assert step_dict["success_count"] == original_step.success_count
            
            assert "failure_count" in step_dict
            assert step_dict["failure_count"] == original_step.failure_count
            
            assert "failure_rate" in step_dict
            assert step_dict["failure_rate"] == original_step.failure_rate
            
            # Percentiles
            assert "p50" in step_dict
            assert step_dict["p50"] == original_step.p50
            
            assert "p90" in step_dict
            assert step_dict["p90"] == original_step.p90
            
            assert "p95" in step_dict
            assert step_dict["p95"] == original_step.p95
            
            assert "p99" in step_dict
            assert step_dict["p99"] == original_step.p99
            
            # Flags
            assert "is_slow" in step_dict
            assert step_dict["is_slow"] == original_step.is_slow
            
            assert "is_flaky" in step_dict
            assert step_dict["is_flaky"] == original_step.is_flaky

    @given(
        pipeline_steps=st.lists(pipeline_step_strategy, min_size=1, max_size=20)
    )
    @settings(max_examples=100)
    def test_config_mode_report_contains_all_steps_with_required_fields(
        self, pipeline_steps: list[PipelineStep]
    ) -> None:
        """
        **Feature: ci-time-tracker, Property 7: Report completeness**
        
        *For any* analysis result in config mode, the generated report SHALL contain
        all steps with all required fields (name, stage, estimated duration).
        
        **Validates: Requirements 5.1, 5.2, 5.3**
        """
        # Create analysis result in config mode
        result = AnalysisResult(
            mode="config",
            steps=pipeline_steps,
            metadata={"provider": "github", "step_count": len(pipeline_steps)},
        )
        
        # Generate report
        report = generate_report(result)
        
        # Verify report has all steps
        assert len(report.steps) == len(pipeline_steps), \
            f"Report should contain {len(pipeline_steps)} steps, but has {len(report.steps)}"
        
        # Verify each step has required fields
        for i, step_dict in enumerate(report.steps):
            original_step = pipeline_steps[i]
            
            # Required fields
            assert "name" in step_dict
            assert step_dict["name"] == original_step.name
            
            assert "stage" in step_dict
            assert step_dict["stage"] == original_step.stage
            
            assert "estimated_duration" in step_dict
            assert step_dict["estimated_duration"] == original_step.estimated_duration

    @given(
        step_stats=st.lists(step_statistics_strategy, min_size=1, max_size=20)
    )
    @settings(max_examples=100)
    def test_report_issues_match_flagged_steps(
        self, step_stats: list[StepStatistics]
    ) -> None:
        """
        **Feature: ci-time-tracker, Property 7: Report completeness**
        
        *For any* analysis result, the issues list in the report SHALL contain
        entries for all steps flagged as slow or flaky.
        
        **Validates: Requirements 5.1, 5.2, 5.3**
        """
        # Create analysis result
        slow_steps = [s.name for s in step_stats if s.is_slow]
        flaky_steps = [s.name for s in step_stats if s.is_flaky]
        
        result = AnalysisResult(
            mode="log",
            steps=step_stats,
            slowest_steps=slow_steps,
            flaky_steps=flaky_steps,
            metadata={"build_count": len(step_stats)},
        )
        
        # Generate report
        report = generate_report(result)
        
        # Count slow and flaky issues
        slow_issues = [issue for issue in report.issues if issue["type"] == "slow"]
        flaky_issues = [issue for issue in report.issues if issue["type"] == "flaky"]
        
        # Verify counts match
        assert len(slow_issues) == len(slow_steps), \
            f"Expected {len(slow_steps)} slow issues, got {len(slow_issues)}"
        
        assert len(flaky_issues) == len(flaky_steps), \
            f"Expected {len(flaky_steps)} flaky issues, got {len(flaky_issues)}"
        
        # Verify all slow steps are in issues
        slow_issue_steps = {issue["step"] for issue in slow_issues}
        assert slow_issue_steps == set(slow_steps), \
            f"Slow issue steps {slow_issue_steps} don't match expected {set(slow_steps)}"
        
        # Verify all flaky steps are in issues
        flaky_issue_steps = {issue["step"] for issue in flaky_issues}
        assert flaky_issue_steps == set(flaky_steps), \
            f"Flaky issue steps {flaky_issue_steps} don't match expected {set(flaky_steps)}"


class TestReporterUnitTests:
    """Unit tests for reporter functions.
    
    Tests specific functionality and edge cases for formatting and parsing.
    """

    def test_generate_report_creates_valid_report(self) -> None:
        """Test that generate_report creates a valid Report object."""
        # Create simple analysis result
        step_stats = [
            StepStatistics(
                name="build",
                execution_count=10,
                success_count=10,
                failure_count=0,
                durations=[10.0, 12.0, 11.0],
                p50=11.0,
                p90=12.0,
                p95=12.0,
                p99=12.0,
                is_slow=False,
                is_flaky=False,
                failure_rate=0.0,
            )
        ]
        
        result = AnalysisResult(
            mode="log",
            steps=step_stats,
            metadata={"build_count": 1},
        )
        
        report = generate_report(result)
        
        assert report.title == "CI Time Tracker Report (log mode)"
        assert report.mode == "log"
        assert len(report.steps) == 1
        assert report.steps[0]["name"] == "build"

    def test_format_json_produces_valid_json(self) -> None:
        """Test that format_json produces valid, parseable JSON."""
        report = Report(
            title="Test Report",
            mode="log",
            generated_at=datetime(2024, 1, 1, 12, 0, 0),
            summary={"build_count": 5},
            steps=[{"name": "test", "execution_count": 5}],
            issues=[],
        )
        
        json_str = format_json(report)
        
        # Verify it's valid JSON
        parsed = json.loads(json_str)
        assert parsed["title"] == "Test Report"
        assert parsed["mode"] == "log"
        assert len(parsed["steps"]) == 1

    def test_format_text_produces_readable_output(self) -> None:
        """Test that format_text produces human-readable text."""
        report = Report(
            title="Test Report",
            mode="log",
            generated_at=datetime(2024, 1, 1, 12, 0, 0),
            summary={"build_count": 5, "step_count": 1},
            steps=[{
                "name": "test",
                "execution_count": 5,
                "p50": 10.0,
                "p90": 15.0,
                "p95": 16.0,
                "p99": 17.0,
                "is_slow": False,
                "is_flaky": False,
            }],
            issues=[],
        )
        
        text = format_text(report)
        
        # Verify key sections are present
        assert "Test Report" in text
        assert "SUMMARY" in text
        assert "STEPS" in text
        assert "test" in text
        assert "build_count: 5" in text

    def test_format_text_includes_issues(self) -> None:
        """Test that format_text includes issues section when issues exist."""
        report = Report(
            title="Test Report",
            mode="log",
            generated_at=datetime(2024, 1, 1, 12, 0, 0),
            summary={"build_count": 5},
            steps=[],
            issues=[
                {
                    "type": "slow",
                    "step": "slow-step",
                    "severity": "warning",
                    "p90": 100.0,
                    "max_duration": 200.0,
                },
                {
                    "type": "flaky",
                    "step": "flaky-step",
                    "severity": "error",
                    "failure_rate": 0.25,
                    "failure_count": 5,
                    "execution_count": 20,
                },
            ],
        )
        
        text = format_text(report)
        
        # Verify issues are displayed
        assert "ISSUES" in text
        assert "slow-step" in text
        assert "flaky-step" in text
        assert "25.0%" in text  # Failure rate

    def test_parse_json_report_deserializes_correctly(self) -> None:
        """Test that parse_json_report correctly deserializes JSON."""
        json_str = json.dumps({
            "title": "Test Report",
            "mode": "log",
            "generated_at": "2024-01-01T12:00:00",
            "summary": {"build_count": 5},
            "steps": [{"name": "test"}],
            "issues": [],
        })
        
        report = parse_json_report(json_str)
        
        assert report.title == "Test Report"
        assert report.mode == "log"
        assert report.generated_at == datetime(2024, 1, 1, 12, 0, 0)
        assert report.summary["build_count"] == 5
        assert len(report.steps) == 1

    def test_flaky_step_reporting_includes_failure_rate_and_count(self) -> None:
        """Test that flaky step issues include failure rate and count.
        
        **Validates: Requirements 3.4**
        """
        step_stats = [
            StepStatistics(
                name="flaky-test",
                execution_count=20,
                success_count=15,
                failure_count=5,
                durations=[10.0] * 20,
                p50=10.0,
                p90=10.0,
                p95=10.0,
                p99=10.0,
                is_slow=False,
                is_flaky=True,
                failure_rate=0.25,
            )
        ]
        
        result = AnalysisResult(
            mode="log",
            steps=step_stats,
            flaky_steps=["flaky-test"],
            metadata={"build_count": 1},
        )
        
        report = generate_report(result)
        
        # Find the flaky issue
        flaky_issues = [i for i in report.issues if i["type"] == "flaky"]
        assert len(flaky_issues) == 1
        
        flaky_issue = flaky_issues[0]
        assert flaky_issue["step"] == "flaky-test"
        assert flaky_issue["failure_rate"] == 0.25
        assert flaky_issue["failure_count"] == 5
        assert flaky_issue["execution_count"] == 20
