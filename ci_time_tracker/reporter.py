"""Reporter module for ci-time-tracker.

This module provides functionality to generate and format reports from
analysis results, supporting both human-readable text and JSON outputs.
"""

import json
from datetime import datetime
from typing import Any

from ci_time_tracker.models import (
    AnalysisResult,
    PipelineStep,
    Report,
    StepStatistics,
)


def generate_report(result: AnalysisResult, title: str | None = None) -> Report:
    """Generate a Report from an AnalysisResult.
    
    Args:
        result: The analysis result to convert into a report
        title: Optional custom title for the report
        
    Returns:
        Report object ready for formatting and output
    """
    if title is None:
        title = f"CI Time Tracker Report ({result.mode} mode)"
    
    # Build summary
    summary: dict[str, Any] = {
        "mode": result.mode,
        **result.metadata,
    }
    
    if result.total_estimated_duration is not None:
        summary["total_estimated_duration"] = result.total_estimated_duration
    
    # Build steps list
    steps_data: list[dict[str, Any]] = []
    
    for step in result.steps:
        if isinstance(step, StepStatistics):
            # Log mode - include statistics
            step_dict = {
                "name": step.name,
                "execution_count": step.execution_count,
                "success_count": step.success_count,
                "failure_count": step.failure_count,
                "failure_rate": step.failure_rate,
                "total_duration": step.total_duration,
                "mean_duration": step.mean_duration,
                "p50": step.p50,
                "p90": step.p90,
                "p95": step.p95,
                "p99": step.p99,
                "is_regression": step.is_regression,
                "is_flaky": step.is_flaky,
                "runner": step.runner,
                "estimated_cost_usd": step.estimated_cost_usd,
                "regression": step.regression,
                "flaky_evidence": step.flaky_evidence,
            }
        elif isinstance(step, PipelineStep):
            # Config mode - include step info
            step_dict = {
                "name": step.name,
                "stage": step.stage,
                "estimated_duration": step.estimated_duration,
            }
            if step.command:
                step_dict["command"] = step.command
        else:
            # Fallback for unknown types
            step_dict = {"name": str(step)}
        
        steps_data.append(step_dict)
    
    # Order log-mode steps by the time they consume, so the biggest
    # contributors come first in every output format.
    if result.mode == "log":
        steps_data.sort(key=lambda step: step.get("total_duration") or 0.0, reverse=True)
    
    # Build issues list
    issues: list[dict[str, Any]] = []
    
    for regressed_step in result.regressed_steps:
        # Find the corresponding step to get more details
        step_info = next((s for s in result.steps if s.name == regressed_step), None)

        issue = {
            "type": "regression",
            "step": regressed_step,
            "severity": "warning",
        }

        if step_info and isinstance(step_info, StepStatistics) and step_info.regression:
            issue.update(step_info.regression)

        issues.append(issue)

    for flaky_step in result.flaky_steps:
        # Find the corresponding step to get more details
        step_info = next((s for s in result.steps if s.name == flaky_step), None)
        
        issue = {
            "type": "flaky",
            "step": flaky_step,
            "severity": "error",
        }
        
        if step_info and isinstance(step_info, StepStatistics):
            issue["failure_rate"] = step_info.failure_rate
            issue["failure_count"] = step_info.failure_count
            issue["execution_count"] = step_info.execution_count
            if step_info.flaky_evidence:
                issue.update(step_info.flaky_evidence)
        
        issues.append(issue)
    
    return Report(
        title=title,
        mode=result.mode,
        generated_at=datetime.now(),
        summary=summary,
        steps=steps_data,
        issues=issues,
    )


def format_json(report: Report) -> str:
    """Format a Report as JSON.
    
    Args:
        report: The report to format
        
    Returns:
        JSON string representation of the report
    """
    return json.dumps(report.to_dict(), indent=2)


# Report width per mode: the log table carries more columns than the config one
_LOG_WIDTH = 110
_CONFIG_WIDTH = 80

# How many steps the "top time consumers" ranking shows
_TOP_CONSUMERS = 5


def format_duration(seconds: float | None) -> str:
    """Format a duration in seconds for humans (45.0s, 12m 30s, 3h 05m)."""
    if seconds is None:
        return "N/A"
    
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    
    minutes, remainder = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_cost(usd: float | None) -> str:
    """Format a cost in US dollars, or N/A when it could not be estimated."""
    if usd is None:
        return "N/A"
    return f"${usd:,.2f}"


def _format_summary_value(key: str, value: Any) -> str:
    """Format a summary value according to what the key measures."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if key.endswith(("_seconds", "_duration")):
            return format_duration(value)
        if key.endswith("_usd"):
            return format_cost(value)
    
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    
    return str(value)


def _format_top_consumers(steps: list[dict[str, Any]], width: int) -> list[str]:
    """Rank the steps that consume the most time across all builds.
    
    Percentiles say how long a step takes; this says where the time actually
    goes, which is what a CI budget is spent on.
    """
    consumers = sorted(
        (step for step in steps if step.get("total_duration")),
        key=lambda step: step["total_duration"],
        reverse=True,
    )[:_TOP_CONSUMERS]
    
    if not consumers:
        return []
    
    total = sum(step.get("total_duration") or 0.0 for step in steps)
    lines = ["TOP TIME CONSUMERS", "-" * width]
    
    for rank, step in enumerate(consumers, start=1):
        consumed = step["total_duration"]
        share = f"{consumed / total * 100:.1f}%" if total else "N/A"
        cost = step.get("estimated_cost_usd")
        cost_str = format_cost(cost) if cost is not None else ""
        
        lines.append(
            f"  {rank}. {step['name'][:44]:<44} {format_duration(consumed):>9} "
            f"{share:>7} {step.get('execution_count', 0):>5} runs {cost_str:>9}"
        )
    
    lines.append("")
    return lines


def _describe_origin(started: dict[str, Any]) -> str:
    """Describe where a regression started: commit, build and date."""
    commit = started.get("head_sha")
    context = [
        f"build {started['build_id']}" if started.get("build_id") else None,
        str(started["timestamp"])[:10] if started.get("timestamp") else None,
    ]
    context_str = ", ".join(part for part in context if part)

    if commit:
        return f"commit {commit} ({context_str})" if context_str else f"commit {commit}"
    return context_str


def _format_issue_details(issue: dict[str, Any]) -> list[str]:
    """Indented detail lines explaining why an issue was raised."""
    details: list[str] = []

    if issue["type"] == "regression":
        if "baseline_median" in issue and "current_median" in issue:
            pct = issue.get("increase_pct")
            pct_str = f"+{pct * 100:.0f}%, " if pct is not None else ""
            details.append(
                f"    Median {format_duration(issue['baseline_median'])} -> "
                f"{format_duration(issue['current_median'])} "
                f"({pct_str}+{format_duration(issue.get('increase_seconds'))})"
            )

        origin = _describe_origin(issue.get("started_at") or {})
        if origin:
            details.append(f"    Started at {origin}")

    elif issue["type"] == "flaky":
        if issue.get("method") == "same-commit":
            details.append(
                f"    Failed and passed on the same commit in {issue.get('flaky_commits', 0)} "
                f"of {issue.get('observed_commits', 0)} commits"
            )
            if issue.get("rerun_recoveries"):
                details.append(f"    Passed on re-run after failing in {issue['rerun_recoveries']} run(s)")
            if issue.get("example_commits"):
                details.append(f"    e.g. {', '.join(issue['example_commits'])}")

        elif "failure_rate" in issue:
            failure_rate = issue["failure_rate"] * 100
            failure_count = issue.get("failure_count", 0)
            execution_count = issue.get("execution_count", 0)
            details.append(
                f"    Failure rate: {failure_rate:.1f}% "
                f"({failure_count}/{execution_count} executions)"
            )
            if issue.get("method") == "failure-rate":
                details.append(
                    "    Based on failure rate only (no commit data), which cannot "
                    "tell flakiness from real breakage"
                )

    return details


def format_text(report: Report, top: int | None = None) -> str:
    """Format a Report as human-readable text.
    
    Args:
        report: The report to format
        top: Show only the N steps that consume the most time; None or 0
            shows every step. Percentiles alone make a long table hard to
            read on a matrix build, where each leg is its own step.
        
    Returns:
        Formatted text string ready for console output
    """
    lines: list[str] = []
    width = _LOG_WIDTH if report.mode == "log" else _CONFIG_WIDTH
    
    # Header
    lines.append("=" * width)
    lines.append(report.title)
    lines.append("=" * width)
    lines.append(f"Generated: {report.generated_at.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Mode: {report.mode}")
    lines.append("")
    
    # Summary
    lines.append("SUMMARY")
    lines.append("-" * width)
    for key, value in report.summary.items():
        if key == "mode":
            continue  # Already displayed
        lines.append(f"  {key}: {_format_summary_value(key, value)}")
    
    if report.summary.get("estimated_cost_usd") is not None:
        lines.append(
            "  note: cost uses private-repository rates and GitHub's per-job "
            "rounding; public repos pay nothing for standard runners"
        )
    lines.append("")
    
    # Where the time goes
    if report.mode == "log":
        lines.extend(_format_top_consumers(report.steps, width))
    
    # Issues
    if report.issues:
        lines.append("ISSUES")
        lines.append("-" * width)
        for issue in report.issues:
            issue_type = issue["type"].upper()
            step = issue["step"]
            severity = issue.get("severity", "warning").upper()
            
            lines.append(f"  [{severity}] {issue_type}: {step}")
            
            lines.extend(_format_issue_details(issue))
        lines.append("")

    # Steps
    lines.append("STEPS")
    lines.append("-" * width)
    
    if report.mode == "log":
        # Log mode - show statistics
        lines.append(
            f"{'Step Name':<38} {'Runs':>5} {'Total':>9} {'P50':>8} {'P90':>8} "
            f"{'P95':>8} {'P99':>8} {'Cost':>8} {'Flags':<10}"
        )
        lines.append("-" * width)
        
        shown = report.steps[:top] if top else report.steps
        
        for step in shown:
            name = step["name"][:37]  # Truncate long names
            exec_count = step.get("execution_count", 0)
            
            total_str = format_duration(step.get("total_duration"))
            p50_str = f"{step['p50']:.2f}s" if step.get("p50") is not None else "N/A"
            p90_str = f"{step['p90']:.2f}s" if step.get("p90") is not None else "N/A"
            p95_str = f"{step['p95']:.2f}s" if step.get("p95") is not None else "N/A"
            p99_str = f"{step['p99']:.2f}s" if step.get("p99") is not None else "N/A"
            cost = step.get("estimated_cost_usd")
            cost_str = format_cost(cost) if cost is not None else "N/A"
            
            flags = []
            if step.get("is_regression"):
                flags.append("REGR")
            if step.get("is_flaky"):
                flags.append("FLAKY")
            flags_str = ",".join(flags) if flags else ""
            
            lines.append(
                f"{name:<38} {exec_count:>5} {total_str:>9} {p50_str:>8} {p90_str:>8} "
                f"{p95_str:>8} {p99_str:>8} {cost_str:>8} {flags_str:<10}"
            )
        
        hidden = len(report.steps) - len(shown)
        if hidden > 0:
            lines.append(f"... and {hidden} more steps (use --top 0 to show all)")
    else:
        # Config mode - show steps and estimates
        lines.append(f"{'Step Name':<40} {'Stage':<20} {'Estimate':<15}")
        lines.append("-" * width)
        
        for step in report.steps:
            name = step["name"][:39]
            stage = step.get("stage", "")[:19] if step.get("stage") else ""
            estimate = step.get("estimated_duration")
            estimate_str = f"{estimate:.2f}s" if estimate is not None else "N/A"
            
            lines.append(f"{name:<40} {stage:<20} {estimate_str:<15}")
    
    lines.append("")
    lines.append("=" * width)
    
    return "\n".join(lines)


def parse_json_report(json_str: str) -> Report:
    """Parse a JSON string into a Report object.
    
    Args:
        json_str: JSON string containing a serialized report
        
    Returns:
        Deserialized Report object
        
    Raises:
        json.JSONDecodeError: If the JSON is invalid
        KeyError: If required fields are missing
    """
    data = json.loads(json_str)
    return Report.from_dict(data)

