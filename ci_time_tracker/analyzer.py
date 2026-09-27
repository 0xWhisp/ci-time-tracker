"""Analyzer module for ci-time-tracker.

This module provides functionality to analyze CI/CD pipeline configurations
and build logs, computing statistics, detecting slow steps, and identifying
flaky steps.
"""

import re
from collections import Counter
from typing import Any

from ci_time_tracker.models import (
    AnalysisResult,
    BuildLog,
    PipelineConfig,
    PipelineStep,
    StepStatistics,
)
from ci_time_tracker.pricing import PricingTable, attributed_cost, job_cost


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


# Matrix parameters as GitHub appends them to a job name: "build (3.12, windows-latest)"
_MATRIX_PARAMS_PATTERN = re.compile(r'\s*\([^()]*\)')


def normalize_matrix_name(name: str) -> str:
    """Strip matrix parameters from a step's job prefix.
    
    GitHub names every leg of a matrix separately, so "Run tests" becomes
    "build (3.12, windows-latest) / Run tests", one step per leg. Grouping
    the legs answers how much a step costs across the whole matrix, at the
    price of mixing runners with different rates.
    
    Only the job prefix is stripped, so parentheses in a step's own name
    are left alone.
    """
    job, separator, step = name.partition(" / ")
    if not separator:
        return _MATRIX_PARAMS_PATTERN.sub("", name).strip()
    
    return f"{_MATRIX_PARAMS_PATTERN.sub('', job).strip()}{separator}{step}"


def analyze_logs(
    logs: list[BuildLog],
    pricing: PricingTable | None = None,
    group_matrix: bool = False,
) -> AnalysisResult:
    """Analyze multiple build logs and compute statistics.
    
    Aggregates step execution data across multiple build logs to compute
    duration percentiles and consumed time, detect slow steps, identify flaky
    steps, and estimate cost where the runner's rate is known.
    
    Args:
        logs: List of parsed build logs
        pricing: Optional runner pricing table; defaults to the built-in rates
        group_matrix: Aggregate the legs of a matrix job into one step
        
    Returns:
        AnalysisResult in log mode with step statistics and detected issues
    """
    pricing = pricing or PricingTable()
    
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
            name = normalize_matrix_name(step.name) if group_matrix else step.name
            
            if name not in step_data:
                step_data[name] = {
                    "durations": [],
                    "success_count": 0,
                    "failure_count": 0,
                    "execution_count": 0,
                    "retry_count": 0,
                    "runners": Counter(),
                    "cost": 0.0,
                    "priced_executions": 0,
                }
            
            data = step_data[name]
            data["execution_count"] += 1
            
            if step.duration_seconds is not None:
                data["durations"].append(step.duration_seconds)
            
            if step.status == "success":
                data["success_count"] += 1
            elif step.status == "failure":
                data["failure_count"] += 1
            
            if step.is_retry:
                data["retry_count"] += 1
            
            if step.runner:
                data["runners"][step.runner] += 1
            
            cost = attributed_cost(step.duration_seconds, pricing.price_for(step.runner))
            if cost is not None:
                data["cost"] += cost
                data["priced_executions"] += 1
    
    # Build step statistics
    step_stats: list[StepStatistics] = []
    
    for name, data in step_data.items():
        percentiles = compute_percentiles(data["durations"])
        
        execution_count = data["execution_count"]
        failure_count = data["failure_count"]
        failure_rate = failure_count / execution_count if execution_count > 0 else 0.0
        
        durations = data["durations"]
        total_duration = sum(durations)
        most_common_runner = data["runners"].most_common(1)
        
        stats = StepStatistics(
            name=name,
            execution_count=execution_count,
            success_count=data["success_count"],
            failure_count=failure_count,
            durations=durations,
            p50=percentiles["p50"],
            p90=percentiles["p90"],
            p95=percentiles["p95"],
            p99=percentiles["p99"],
            is_slow=False,  # Will be set by detect_slow_steps
            is_flaky=False,  # Will be set by detect_flaky_steps
            failure_rate=failure_rate,
            total_duration=total_duration,
            mean_duration=total_duration / len(durations) if durations else None,
            runner=most_common_runner[0][0] if most_common_runner else None,
            estimated_cost_usd=data["cost"] if data["priced_executions"] else None,
        )
        step_stats.append(stats)
    
    # Detect slow and flaky steps
    slow_steps = detect_slow_steps(step_stats)
    flaky_steps = detect_flaky_steps(step_stats)
    
    # Update flags on step statistics
    for stats in step_stats:
        stats.is_slow = stats.name in slow_steps
        stats.is_flaky = stats.name in flaky_steps
    
    metadata: dict[str, Any] = {
        "build_count": len(logs),
        "step_count": len(step_stats),
        "total_execution_seconds": sum(stats.total_duration for stats in step_stats),
    }
    if group_matrix:
        metadata["grouping"] = "matrix legs merged"
    metadata.update(aggregate_job_metrics(logs, pricing))
    
    return AnalysisResult(
        mode="log",
        steps=step_stats,
        total_estimated_duration=None,
        slowest_steps=slow_steps,
        flaky_steps=flaky_steps,
        metadata=metadata,
    )


def aggregate_job_metrics(
    logs: list[BuildLog],
    pricing: PricingTable | None = None,
) -> dict[str, Any]:
    """Aggregate job-level queue time and billed cost across builds.
    
    Only providers that report jobs (the API ingestion) carry this data, so
    text logs yield an empty dict. Cost uses GitHub's per-job rounding, which
    makes it slightly higher than the sum of the steps' attributed costs.
    
    Args:
        logs: List of build logs whose metadata may carry a "jobs" list
        pricing: Optional runner pricing table
        
    Returns:
        Metadata entries for queue time and cost, empty when no jobs are known
    """
    pricing = pricing or PricingTable()
    
    queue_times: list[float] = []
    billed_cost = 0.0
    priced_jobs = 0
    unpriced_runners: set[str] = set()
    job_count = 0
    
    for log in logs:
        for job in log.metadata.get("jobs") or []:
            job_count += 1
            
            queued = job.get("queued_seconds")
            if queued is not None and queued >= 0:
                queue_times.append(float(queued))
            
            price = pricing.price_for(job.get("runner"))
            if price is None:
                if job.get("runner"):
                    unpriced_runners.add(str(job["runner"]))
                continue
            
            cost = job_cost(job.get("duration_seconds"), price)
            if cost is not None:
                billed_cost += cost
                priced_jobs += 1
    
    if not job_count:
        return {}
    
    metrics: dict[str, Any] = {"job_count": job_count}
    
    if queue_times:
        percentiles = compute_percentiles(queue_times)
        metrics["total_queue_seconds"] = sum(queue_times)
        metrics["queue_p50_seconds"] = percentiles["p50"]
        metrics["queue_p90_seconds"] = percentiles["p90"]
    
    if priced_jobs:
        metrics["estimated_cost_usd"] = round(billed_cost, 4)
        metrics["priced_jobs"] = priced_jobs
        metrics["pricing_source"] = pricing.source
    
    if unpriced_runners:
        metrics["unpriced_runners"] = sorted(unpriced_runners)
    
    return metrics


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
