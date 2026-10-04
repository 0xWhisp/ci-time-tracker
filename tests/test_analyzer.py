"""Tests for analyzer module.

Tests cover:
- Percentile computation accuracy (Property 3)
- Flaky step detection bounds (Property 5)
- Estimate association correctness (Property 2)
- Requirements: 1.5, 2.2, 2.3, 3.1, 3.2
"""

import math
from hypothesis import given, settings, strategies as st, assume

from ci_time_tracker.analyzer import (
    compute_percentiles, 
    analyze_config
)
from ci_time_tracker.models import (
    StepStatistics, 
    PipelineStep, 
    PipelineConfig
)


# Hypothesis strategies for generating valid duration lists

# Generate positive durations (non-zero to avoid edge cases with all-zero lists)
durations = st.floats(min_value=0.1, max_value=3600.0, allow_nan=False, allow_infinity=False)

# Generate non-empty lists of durations
duration_lists = st.lists(durations, min_size=1, max_size=100)


def reference_percentile(sorted_data: list[float], p: float) -> float:
    """Reference implementation of nearest-rank percentile.
    
    Uses the ceiling of (p/100 * n) as the rank, then returns the value
    at that rank (1-indexed, converted to 0-indexed).
    
    This matches the statistical definition where pN is the smallest value
    such that at least N% of the data is less than or equal to that value.
    """
    n = len(sorted_data)
    if n == 1:
        return sorted_data[0]
    
    # Calculate rank using nearest-rank method
    rank = math.ceil((p / 100.0) * n)
    # Clamp to valid range (1 to n for 1-indexed)
    rank = max(1, min(rank, n))
    # Convert to 0-indexed
    index = rank - 1
    return sorted_data[index]


class TestPercentileComputation:
    """Property tests for percentile computation accuracy.
    
    **Feature: ci-time-tracker, Property 3: Percentile computation accuracy**
    """

    @given(durations=duration_lists)
    @settings(max_examples=100)
    def test_percentile_accuracy(self, durations: list[float]) -> None:
        """
        **Feature: ci-time-tracker, Property 3: Percentile computation accuracy**
        
        *For any* non-empty list of duration values, the computed percentiles 
        (p50, p90, p95, p99) SHALL match the statistical definition where pN 
        is the value below which N% of observations fall.
        
        **Validates: Requirements 2.2**
        """
        result = compute_percentiles(durations)
        sorted_durations = sorted(durations)
        
        # Verify each percentile matches the reference implementation
        assert result["p50"] is not None
        assert result["p90"] is not None
        assert result["p95"] is not None
        assert result["p99"] is not None
        
        # Compare with reference implementation
        expected_p50 = reference_percentile(sorted_durations, 50)
        expected_p90 = reference_percentile(sorted_durations, 90)
        expected_p95 = reference_percentile(sorted_durations, 95)
        expected_p99 = reference_percentile(sorted_durations, 99)
        
        assert math.isclose(result["p50"], expected_p50, rel_tol=1e-9), \
            f"p50 mismatch: got {result['p50']}, expected {expected_p50}"
        assert math.isclose(result["p90"], expected_p90, rel_tol=1e-9), \
            f"p90 mismatch: got {result['p90']}, expected {expected_p90}"
        assert math.isclose(result["p95"], expected_p95, rel_tol=1e-9), \
            f"p95 mismatch: got {result['p95']}, expected {expected_p95}"
        assert math.isclose(result["p99"], expected_p99, rel_tol=1e-9), \
            f"p99 mismatch: got {result['p99']}, expected {expected_p99}"

    @given(durations=duration_lists)
    @settings(max_examples=100)
    def test_percentiles_are_ordered(self, durations: list[float]) -> None:
        """
        Verify that percentiles maintain proper ordering: p50 <= p90 <= p95 <= p99.
        
        This is a sanity check that follows from the definition of percentiles.
        """
        result = compute_percentiles(durations)
        
        assert result["p50"] is not None
        assert result["p90"] is not None
        assert result["p95"] is not None
        assert result["p99"] is not None
        
        assert result["p50"] <= result["p90"], \
            f"p50 ({result['p50']}) should be <= p90 ({result['p90']})"
        assert result["p90"] <= result["p95"], \
            f"p90 ({result['p90']}) should be <= p95 ({result['p95']})"
        assert result["p95"] <= result["p99"], \
            f"p95 ({result['p95']}) should be <= p99 ({result['p99']})"

    @given(durations=duration_lists)
    @settings(max_examples=100)
    def test_percentiles_within_data_range(self, durations: list[float]) -> None:
        """
        Verify that all percentiles fall within the range of input data.
        """
        result = compute_percentiles(durations)
        min_val = min(durations)
        max_val = max(durations)
        
        for key in ["p50", "p90", "p95", "p99"]:
            assert result[key] is not None
            assert min_val <= result[key] <= max_val, \
                f"{key} ({result[key]}) should be within [{min_val}, {max_val}]"

    def test_empty_list_returns_none(self) -> None:
        """Verify empty list returns None for all percentiles."""
        result = compute_percentiles([])
        assert result["p50"] is None
        assert result["p90"] is None
        assert result["p95"] is None
        assert result["p99"] is None

    def test_single_value_returns_that_value(self) -> None:
        """Verify single-element list returns that value for all percentiles."""
        result = compute_percentiles([42.0])
        assert result["p50"] == 42.0
        assert result["p90"] == 42.0
        assert result["p95"] == 42.0
        assert result["p99"] == 42.0

    def test_known_percentiles(self) -> None:
        """Verify percentiles for a known distribution."""
        # 10 values: 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
        durations = [float(i) for i in range(1, 11)]
        result = compute_percentiles(durations)
        
        # For n=10:
        # p50: ceil(0.5 * 10) = 5, index 4, value 5.0
        # p90: ceil(0.9 * 10) = 9, index 8, value 9.0
        # p95: ceil(0.95 * 10) = 10, index 9, value 10.0
        # p99: ceil(0.99 * 10) = 10, index 9, value 10.0
        assert result["p50"] == 5.0
        assert result["p90"] == 9.0
        assert result["p95"] == 10.0
        assert result["p99"] == 10.0


# Hypothesis strategies for step detection tests

# Generate valid step names (non-empty strings)
step_names = st.text(
    alphabet=st.characters(whitelist_categories=('L', 'N', 'Pd', 'Pc')),
    min_size=1, max_size=50
).filter(lambda s: s.strip())


class TestFlakyStepDetection:
    """Property tests for flaky step detection.
    
    **Feature: ci-time-tracker, Property 5: Flaky step detection bounds**
    """

    @given(
        name=step_names,
        failure_rate=st.floats(min_value=0.05, max_value=0.95, allow_nan=False, allow_infinity=False).filter(
            lambda x: 0.05 < x < 0.95
        )
    )
    @settings(max_examples=100)
    def test_step_with_failure_rate_between_5_and_95_percent_is_flagged_flaky(
        self, name: str, failure_rate: float
    ) -> None:
        """
        **Feature: ci-time-tracker, Property 5: Flaky step detection bounds**
        
        *For any* step statistics where the failure rate is strictly between 5% and 95%,
        the step SHALL be flagged as flaky.
        
        **Validates: Requirements 3.1, 3.2**
        """
        from ci_time_tracker.analyzer import detect_flaky_steps
        
        # Generate execution count large enough to produce meaningful failure rates
        execution_count = 100
        failure_count = int(execution_count * failure_rate)
        success_count = execution_count - failure_count
        
        # Ensure we're strictly in the flaky range
        actual_failure_rate = failure_count / execution_count
        assume(0.05 < actual_failure_rate < 0.95)
        
        stats = StepStatistics(
            name=name,
            execution_count=execution_count,
            success_count=success_count,
            failure_count=failure_count,
            durations=[10.0] * execution_count,
            p50=10.0,
            p90=10.0,
            p95=10.0,
            p99=10.0,
            is_regression=False,
            is_flaky=False,
            failure_rate=actual_failure_rate,
        )
        
        flaky_steps = detect_flaky_steps([stats])
        
        assert name in flaky_steps, \
            f"Step '{name}' should be flagged as flaky: failure rate {actual_failure_rate:.2%} is between 5% and 95%"

    @given(
        name=step_names,
        # Generate failure rates outside the flaky range
        failure_rate=st.one_of(
            st.floats(min_value=0.0, max_value=0.05, allow_nan=False, allow_infinity=False),
            st.floats(min_value=0.95, max_value=1.0, allow_nan=False, allow_infinity=False)
        )
    )
    @settings(max_examples=100)
    def test_step_with_failure_rate_outside_5_to_95_percent_is_not_flagged_flaky(
        self, name: str, failure_rate: float
    ) -> None:
        """
        **Feature: ci-time-tracker, Property 5: Flaky step detection bounds**
        
        *For any* step statistics where the failure rate is at or below 5%, or at or
        above 95%, the step SHALL NOT be flagged as flaky.
        
        **Validates: Requirements 3.1, 3.2**
        """
        from ci_time_tracker.analyzer import detect_flaky_steps
        
        # Generate execution count
        execution_count = 100
        failure_count = int(execution_count * failure_rate)
        success_count = execution_count - failure_count
        
        actual_failure_rate = failure_count / execution_count
        
        # Ensure we're outside the flaky range
        assume(actual_failure_rate <= 0.05 or actual_failure_rate >= 0.95)
        
        stats = StepStatistics(
            name=name,
            execution_count=execution_count,
            success_count=success_count,
            failure_count=failure_count,
            durations=[10.0] * execution_count,
            p50=10.0,
            p90=10.0,
            p95=10.0,
            p99=10.0,
            is_regression=False,
            is_flaky=False,
            failure_rate=actual_failure_rate,
        )
        
        flaky_steps = detect_flaky_steps([stats])
        
        assert name not in flaky_steps, \
            f"Step '{name}' should NOT be flagged as flaky: failure rate {actual_failure_rate:.2%} is outside 5%-95%"

    @given(
        name=step_names,
        execution_count=st.integers(min_value=20, max_value=1000),
    )
    @settings(max_examples=100)
    def test_boundary_at_exactly_5_percent_is_not_flaky(
        self, name: str, execution_count: int
    ) -> None:
        """
        **Feature: ci-time-tracker, Property 5: Flaky step detection bounds**
        
        *For any* step where the failure rate is exactly 5%, the step SHALL NOT
        be flagged as flaky (threshold is strictly greater than 5%).
        
        **Validates: Requirements 3.1, 3.2**
        """
        from ci_time_tracker.analyzer import detect_flaky_steps
        
        # Set failure rate to exactly 5%
        failure_count = int(execution_count * 0.05)
        success_count = execution_count - failure_count
        actual_failure_rate = failure_count / execution_count
        
        # Only test if we can achieve exactly or very close to 5%
        assume(abs(actual_failure_rate - 0.05) < 0.001)
        
        stats = StepStatistics(
            name=name,
            execution_count=execution_count,
            success_count=success_count,
            failure_count=failure_count,
            durations=[10.0] * execution_count,
            p50=10.0,
            p90=10.0,
            p95=10.0,
            p99=10.0,
            is_regression=False,
            is_flaky=False,
            failure_rate=actual_failure_rate,
        )
        
        flaky_steps = detect_flaky_steps([stats])
        
        assert name not in flaky_steps, \
            f"Step '{name}' should NOT be flagged as flaky: failure rate {actual_failure_rate:.2%} is at boundary (5%)"

    @given(
        name=step_names,
        execution_count=st.integers(min_value=20, max_value=1000),
    )
    @settings(max_examples=100)
    def test_boundary_at_exactly_95_percent_is_not_flaky(
        self, name: str, execution_count: int
    ) -> None:
        """
        **Feature: ci-time-tracker, Property 5: Flaky step detection bounds**
        
        *For any* step where the failure rate is exactly 95%, the step SHALL NOT
        be flagged as flaky (threshold is strictly less than 95%).
        
        **Validates: Requirements 3.1, 3.2**
        """
        from ci_time_tracker.analyzer import detect_flaky_steps
        
        # Set failure rate to exactly 95%
        failure_count = int(execution_count * 0.95)
        success_count = execution_count - failure_count
        actual_failure_rate = failure_count / execution_count
        
        # Only test if we can achieve exactly or very close to 95%
        # But ensure we're not less than 95% (which would be flaky)
        assume(actual_failure_rate >= 0.95)
        
        stats = StepStatistics(
            name=name,
            execution_count=execution_count,
            success_count=success_count,
            failure_count=failure_count,
            durations=[10.0] * execution_count,
            p50=10.0,
            p90=10.0,
            p95=10.0,
            p99=10.0,
            is_regression=False,
            is_flaky=False,
            failure_rate=actual_failure_rate,
        )
        
        flaky_steps = detect_flaky_steps([stats])
        
        # The actual threshold is strict inequality (< 0.95), so exactly 95% should NOT be flaky
        # But if actual_failure_rate is slightly less than 0.95, it SHOULD be flaky
        if actual_failure_rate < 0.95:
            # This shouldn't happen given our assume, but handle edge case
            pass  # Could be flaky or not
        else:
            # actual_failure_rate >= 0.95, should NOT be flaky
            assert name not in flaky_steps, \
                f"Step '{name}' should NOT be flagged as flaky: failure rate {actual_failure_rate:.2%} is at or above boundary (95%)"

    def test_step_with_no_executions_is_not_flagged_flaky(self) -> None:
        """Steps with no execution data should not be flagged as flaky."""
        from ci_time_tracker.analyzer import detect_flaky_steps
        
        stats = StepStatistics(
            name="empty-step",
            execution_count=0,
            success_count=0,
            failure_count=0,
            durations=[],
            p50=None,
            p90=None,
            p95=None,
            p99=None,
            is_regression=False,
            is_flaky=False,
            failure_rate=0.0,
        )
        
        flaky_steps = detect_flaky_steps([stats])
        assert "empty-step" not in flaky_steps

    def test_step_with_100_percent_success_is_not_flagged_flaky(self) -> None:
        """Steps with 100% success rate (0% failure) should not be flagged as flaky."""
        from ci_time_tracker.analyzer import detect_flaky_steps
        
        stats = StepStatistics(
            name="always-success",
            execution_count=100,
            success_count=100,
            failure_count=0,
            durations=[10.0] * 100,
            p50=10.0,
            p90=10.0,
            p95=10.0,
            p99=10.0,
            is_regression=False,
            is_flaky=False,
            failure_rate=0.0,
        )
        
        flaky_steps = detect_flaky_steps([stats])
        assert "always-success" not in flaky_steps

    def test_step_with_100_percent_failure_is_not_flagged_flaky(self) -> None:
        """Steps with 100% failure rate should not be flagged as flaky."""
        from ci_time_tracker.analyzer import detect_flaky_steps
        
        stats = StepStatistics(
            name="always-fails",
            execution_count=100,
            success_count=0,
            failure_count=100,
            durations=[10.0] * 100,
            p50=10.0,
            p90=10.0,
            p95=10.0,
            p99=10.0,
            is_regression=False,
            is_flaky=False,
            failure_rate=1.0,
        )
        
        flaky_steps = detect_flaky_steps([stats])
        assert "always-fails" not in flaky_steps


class TestEstimateAssociation:
    """Property tests for estimate association.
    
    **Feature: ci-time-tracker, Property 2: Estimate association correctness**
    """

    @given(
        step_names=st.lists(step_names, min_size=1, max_size=20, unique=True),
        estimate_durations=st.lists(
            st.floats(min_value=1.0, max_value=3600.0, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=20
        )
    )
    @settings(max_examples=100)
    def test_estimates_are_correctly_associated_with_steps(
        self, step_names: list[str], estimate_durations: list[float]
    ) -> None:
        """
        **Feature: ci-time-tracker, Property 2: Estimate association correctness**
        
        *For any* set of pipeline steps and user-provided estimates, each step
        SHALL be correctly associated with its corresponding estimate by name.
        
        **Validates: Requirements 1.5**
        """
        # Create pipeline steps
        steps = [PipelineStep(name=name, stage="test") for name in step_names]
        
        # Create pipeline config
        config = PipelineConfig(
            provider="github",
            steps=steps,
            stages=["test"]
        )
        
        # Create estimates dictionary - associate some (but not all) steps with estimates
        # Use min length to avoid index errors
        num_estimates = min(len(step_names), len(estimate_durations))
        estimates = {step_names[i]: estimate_durations[i] for i in range(num_estimates)}
        
        # Analyze config with estimates
        result = analyze_config(config, estimates)
        
        # Verify each step in the result has the correct estimate
        assert len(result.steps) == len(step_names), \
            f"Expected {len(step_names)} steps in result, got {len(result.steps)}"
        
        for i, step in enumerate(result.steps):
            expected_name = step_names[i]
            assert step.name == expected_name, \
                f"Step {i} has wrong name: expected '{expected_name}', got '{step.name}'"
            
            if expected_name in estimates:
                expected_duration = estimates[expected_name]
                assert step.estimated_duration == expected_duration, \
                    f"Step '{expected_name}' has wrong estimate: expected {expected_duration}, got {step.estimated_duration}"
            else:
                assert step.estimated_duration is None, \
                    f"Step '{expected_name}' should have no estimate, but got {step.estimated_duration}"

    @given(
        step_names=st.lists(step_names, min_size=1, max_size=20, unique=True),
        estimate_durations=st.lists(
            st.floats(min_value=1.0, max_value=3600.0, allow_nan=False, allow_infinity=False),
            min_size=1,
            max_size=20
        )
    )
    @settings(max_examples=100)
    def test_total_estimated_duration_is_sum_of_all_estimates(
        self, step_names: list[str], estimate_durations: list[float]
    ) -> None:
        """
        **Feature: ci-time-tracker, Property 2: Estimate association correctness**
        
        *For any* set of steps with estimates, the total estimated duration SHALL
        equal the sum of all individual step estimates.
        
        **Validates: Requirements 1.5**
        """
        # Create pipeline steps
        steps = [PipelineStep(name=name, stage="test") for name in step_names]
        
        # Create pipeline config
        config = PipelineConfig(
            provider="github",
            steps=steps,
            stages=["test"]
        )
        
        # Create estimates for all steps
        num_estimates = min(len(step_names), len(estimate_durations))
        estimates = {step_names[i]: estimate_durations[i] for i in range(num_estimates)}
        
        # Analyze config with estimates
        result = analyze_config(config, estimates)
        
        # Calculate expected total
        expected_total = sum(estimates.values())
        
        # Verify total estimated duration
        if estimates:
            assert result.total_estimated_duration is not None, \
                "Total estimated duration should not be None when estimates are provided"
            assert math.isclose(result.total_estimated_duration, expected_total, rel_tol=1e-9), \
                f"Total estimated duration mismatch: expected {expected_total}, got {result.total_estimated_duration}"
        else:
            assert result.total_estimated_duration is None, \
                "Total estimated duration should be None when no estimates are provided"

    @given(
        step_names=st.lists(step_names, min_size=1, max_size=20, unique=True),
    )
    @settings(max_examples=100)
    def test_steps_without_estimates_have_none_duration(
        self, step_names: list[str]
    ) -> None:
        """
        **Feature: ci-time-tracker, Property 2: Estimate association correctness**
        
        *For any* step without a provided estimate, the step's estimated_duration
        SHALL be None.
        
        **Validates: Requirements 1.5**
        """
        # Create pipeline steps
        steps = [PipelineStep(name=name, stage="test") for name in step_names]
        
        # Create pipeline config
        config = PipelineConfig(
            provider="github",
            steps=steps,
            stages=["test"]
        )
        
        # Analyze config WITHOUT estimates
        result = analyze_config(config, estimates=None)
        
        # Verify all steps have None for estimated_duration
        for step in result.steps:
            assert step.estimated_duration is None, \
                f"Step '{step.name}' should have None estimate, but got {step.estimated_duration}"
        
        # Verify total estimated duration is None
        assert result.total_estimated_duration is None, \
            "Total estimated duration should be None when no estimates are provided"

    def test_empty_estimates_dict_results_in_no_associations(self) -> None:
        """Passing an empty estimates dictionary should result in no associations."""
        steps = [
            PipelineStep(name="build", stage="build"),
            PipelineStep(name="test", stage="test"),
        ]
        config = PipelineConfig(provider="github", steps=steps, stages=["build", "test"])
        
        result = analyze_config(config, estimates={})
        
        for step in result.steps:
            assert step.estimated_duration is None
        assert result.total_estimated_duration is None

    def test_estimate_for_nonexistent_step_is_ignored(self) -> None:
        """Estimates for steps not in the config should be ignored."""
        steps = [
            PipelineStep(name="build", stage="build"),
            PipelineStep(name="test", stage="test"),
        ]
        config = PipelineConfig(provider="github", steps=steps, stages=["build", "test"])
        
        # Provide estimate for a step that doesn't exist
        estimates = {
            "build": 100.0,
            "nonexistent": 50.0,  # This should be ignored
        }
        
        result = analyze_config(config, estimates)
        
        # Only build should have an estimate
        build_step = [s for s in result.steps if s.name == "build"][0]
        test_step = [s for s in result.steps if s.name == "test"][0]
        
        assert build_step.estimated_duration == 100.0
        assert test_step.estimated_duration is None
        assert result.total_estimated_duration == 100.0