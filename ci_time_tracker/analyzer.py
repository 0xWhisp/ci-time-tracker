"""Analyzer module for ci-time-tracker.

This module provides functionality to analyze CI/CD pipeline configurations
and build logs, computing statistics, detecting slow steps, and identifying
flaky steps.
"""

from ci_time_tracker.models import (
    AnalysisResult,
    BuildLog,
    PipelineConfig,
    PipelineStep,
    StepStatistics,
)


def compute_percentiles(durations: list[float]) -> dict[str, float | None]:
    """Compute p50, p90, p95, p99 percentiles for a list of durations.
    
    Uses the nearest-rank method for percentile calculation, where pN is
    the smallest value such that at least N% of the data is less than or
    equal to that value.
    
    Args:
        durations: List of duration values in seconds. Must be non-empty.
        
    Returns:
        Dictionary with keys 'p50', 'p90', 'p95', 'p99' and their computed values.
        Returns None values if durations list is empty.
    """
    if not durations:
        return {"p50": None, "p90": None, "p95": None, "p99": None}
    
    sorted_durations = sorted(durations)
    n = len(sorted_durations)
    
    def percentile(p: float) -> float:
        """Calculate percentile using nearest-rank method."""
        if n == 1:
            return sorted_durations[0]
        # Calculate rank (1-indexed position)
        rank = (p / 100.0) * n
        # Use ceiling to get the index (convert to 0-indexed)
        index = int(rank)
        if index >= n:
            index = n - 1
        elif index < 1:
            index = 0
        else:
            index = index - 1 if rank == int(rank) else index
        # Clamp to valid range
        index = max(0, min(index, n - 1))
        return sorted_durations[index]
    
    return {
        "p50": percentile(50),
        "p90": percentile(90),
        "p95": percentile(95),
        "p99": percentile(99),
    }


def analyze_config(
    config: PipelineConfig, 
    estimates: dict[str, float] | None = None
) -> AnalysisResult:
    """Analyze pipeline configuration and return summary.
    
    Processes a parsed pipeline configuration to create an analysis result
    containing all steps with their estimated durations (if provided).
    
    Args:
        config: Parsed pipeline configuration
        estimates: Optional dictionary mapping step names to estimated
                   durations in seconds
                   
    Returns:
        AnalysisResult in config mode with steps and total estimated duration
    """
    estimates = estimates or {}
    
    # Associate estimates with steps
    steps_with_estimates: list[PipelineStep] = []
    total_estimated = 0.0
    has_estimates = False
    
    for step in config.steps:
        # Look up estimate by step name
        estimated_duration = estimates.get(step.name)
        
        # Create new step with estimate if found
        step_with_estimate = PipelineStep(
            name=step.name,
            stage=step.stage,
            command=step.command,
            estimated_duration=estimated_duration,
        )
        steps_with_estimates.append(step_with_estimate)
        
        if estimated_duration is not None:
            total_estimated += estimated_duration
            has_estimates = True
    
    return AnalysisResult(
        mode="config",
        steps=steps_with_estimates,
        total_estimated_duration=total_estimated if has_estimates else None,
        slowest_steps=[],
        flaky_steps=[],
        metadata={
            "provider": config.provider,
            "stages": config.stages,
            "step_count": len(steps_with_estimates),
            **config.metadata,
        },
    )


def analyze_logs(logs: list[BuildLog]) -> AnalysisResult:
    """Analyze multiple build logs and compute statistics.
    
    Aggregates step execution data across multiple build logs to compute
    duration percentiles, detect slow steps, and identify flaky steps.
    
    Args:
        logs: List of parsed build logs
        
    Returns:
        AnalysisResult in log mode with step statistics and detected issues
    """
    if not logs:
        return AnalysisResult(
            mode="log",
            steps=[],
            total_estimated_duration=None,
            slowest_steps=[],
            flaky_steps=[],
            metadata={"build_count": 0},
        )
    
    # Aggregate step data by name
    step_data: dict[str, dict] = {}
    
    for log in logs:
        for step in log.steps:
            if step.name not in step_data:
                step_data[step.name] = {
                    "durations": [],
                    "success_count": 0,
                    "failure_count": 0,
                    "execution_count": 0,
                    "retry_count": 0,
                }
            
            data = step_data[step.name]
            data["execution_count"] += 1
            
            if step.duration_seconds is not None:
                data["durations"].append(step.duration_seconds)
            
            if step.status == "success":
                data["success_count"] += 1
            elif step.status == "failure":
                data["failure_count"] += 1
            
            if step.is_retry:
                data["retry_count"] += 1
    
    # Build step statistics
    step_stats: list[StepStatistics] = []
    
    for name, data in step_data.items():
        percentiles = compute_percentiles(data["durations"])
        
        execution_count = data["execution_count"]
        failure_count = data["failure_count"]
        failure_rate = failure_count / execution_count if execution_count > 0 else 0.0
        
        stats = StepStatistics(
            name=name,
            execution_count=execution_count,
            success_count=data["success_count"],
            failure_count=failure_count,
            durations=data["durations"],
            p50=percentiles["p50"],
            p90=percentiles["p90"],
            p95=percentiles["p95"],
            p99=percentiles["p99"],
            is_slow=False,  # Will be set by detect_slow_steps
            is_flaky=False,  # Will be set by detect_flaky_steps
            failure_rate=failure_rate,
        )
        step_stats.append(stats)
    
    # Detect slow and flaky steps
    slow_steps = detect_slow_steps(step_stats)
    flaky_steps = detect_flaky_steps(step_stats)
    
    # Update flags on step statistics
    for stats in step_stats:
        stats.is_slow = stats.name in slow_steps
        stats.is_flaky = stats.name in flaky_steps
    
    return AnalysisResult(
        mode="log",
        steps=step_stats,
        total_estimated_duration=None,
        slowest_steps=slow_steps,
        flaky_steps=flaky_steps,
        metadata={
            "build_count": len(logs),
            "step_count": len(step_stats),
        },
    )


def detect_slow_steps(stats: list[StepStatistics]) -> list[str]:
    """Identify steps exceeding p90 by more than 50%.
    
    A step is flagged as slow if any of its recorded durations exceeds
    the p90 percentile by more than 50%.
    
    Args:
        stats: List of step statistics with computed percentiles
        
    Returns:
        List of step names that are flagged as slow
    """
    slow_steps: list[str] = []
    
    for step in stats:
        if step.p90 is None or not step.durations:
            continue
        
        threshold = step.p90 * 1.5  # p90 + 50%
        
        # Check if any duration exceeds the threshold
        for duration in step.durations:
            if duration > threshold:
                slow_steps.append(step.name)
                break
    
    return slow_steps


def detect_flaky_steps(stats: list[StepStatistics]) -> list[str]:
    """Identify steps with failure rate between 5% and 95%.
    
    A step is flagged as flaky if its failure rate is strictly between
    5% and 95% (exclusive), indicating intermittent failures.
    
    Args:
        stats: List of step statistics with failure counts
        
    Returns:
        List of step names that are flagged as flaky
    """
    flaky_steps: list[str] = []
    
    for step in stats:
        # Flaky if failure rate is strictly between 5% and 95%
        if 0.05 < step.failure_rate < 0.95:
            flaky_steps.append(step.name)
    
    return flaky_steps
