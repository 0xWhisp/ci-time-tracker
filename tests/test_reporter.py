"""Property-based tests for reporter module.

Tests the Report JSON serialization/deserialization round-trip property.
"""

import json
from datetime import datetime, timezone

from hypothesis import given, settings, strategies as st

from ci_time_tracker.models import Report


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
