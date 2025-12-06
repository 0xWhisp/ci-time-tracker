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
                "p50": step.p50,
                "p90": step.p90,
                "p95": step.p95,
                "p99": step.p99,
                "is_slow": step.is_slow,
                "is_flaky": step.is_flaky,
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
    
    # Build issues list
    issues: list[dict[str, Any]] = []
    
    for slow_step in result.slowest_steps:
        # Find the corresponding step to get more details
        step_info = next((s for s in result.steps if s.name == slow_step), None)
        
        issue = {
            "type": "slow",
            "step": slow_step,
            "severity": "warning",
        }
        
        if step_info and isinstance(step_info, StepStatistics):
            if step_info.p90 is not None:
                issue["p90"] = step_info.p90
            if step_info.durations:
                issue["max_duration"] = max(step_info.durations)
        
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


def format_text(report: Report) -> str:
    """Format a Report as human-readable text.
    
    Args:
        report: The report to format
        
    Returns:
        Formatted text string ready for console output
    """
    lines: list[str] = []
    
    # Header
    lines.append("=" * 80)
    lines.append(report.title)
    lines.append("=" * 80)
    lines.append(f"Generated: {report.generated_at.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Mode: {report.mode}")
    lines.append("")
    
    # Summary
    lines.append("SUMMARY")
    lines.append("-" * 80)
    for key, value in report.summary.items():
        if key == "mode":
            continue  # Already displayed
        lines.append(f"  {key}: {value}")
    lines.append("")
    
    # Issues
    if report.issues:
        lines.append("ISSUES")
        lines.append("-" * 80)
        for issue in report.issues:
            issue_type = issue["type"].upper()
            step = issue["step"]
            severity = issue.get("severity", "warning").upper()
            
            lines.append(f"  [{severity}] {issue_type}: {step}")
            
            if issue["type"] == "slow":
                if "p90" in issue and "max_duration" in issue:
                    lines.append(f"    P90: {issue['p90']:.2f}s, Max: {issue['max_duration']:.2f}s")
            elif issue["type"] == "flaky":
                if "failure_rate" in issue:
                    failure_rate = issue["failure_rate"] * 100
                    failure_count = issue.get("failure_count", 0)
                    execution_count = issue.get("execution_count", 0)
                    lines.append(
                        f"    Failure rate: {failure_rate:.1f}% "
                        f"({failure_count}/{execution_count} executions)"
                    )
        lines.append("")
    
    # Steps
    lines.append("STEPS")
    lines.append("-" * 80)
    
    if report.mode == "log":
        # Log mode - show statistics
        lines.append(
            f"{'Step Name':<30} {'Executions':>12} {'P50':>8} {'P90':>8} {'P95':>8} {'P99':>8} {'Flags':<10}"
        )
        lines.append("-" * 80)
        
        for step in report.steps:
            name = step["name"][:29]  # Truncate long names
            exec_count = step.get("execution_count", 0)
            
            p50 = step.get("p50")
            p90 = step.get("p90")
            p95 = step.get("p95")
            p99 = step.get("p99")
            
            p50_str = f"{p50:.2f}s" if p50 is not None else "N/A"
            p90_str = f"{p90:.2f}s" if p90 is not None else "N/A"
            p95_str = f"{p95:.2f}s" if p95 is not None else "N/A"
            p99_str = f"{p99:.2f}s" if p99 is not None else "N/A"
            
            flags = []
            if step.get("is_slow"):
                flags.append("SLOW")
            if step.get("is_flaky"):
                flags.append("FLAKY")
            flags_str = ",".join(flags) if flags else ""
            
            lines.append(
                f"{name:<30} {exec_count:>12} {p50_str:>8} {p90_str:>8} {p95_str:>8} {p99_str:>8} {flags_str:<10}"
            )
    else:
        # Config mode - show steps and estimates
        lines.append(f"{'Step Name':<40} {'Stage':<20} {'Estimate':<15}")
        lines.append("-" * 80)
        
        for step in report.steps:
            name = step["name"][:39]
            stage = step.get("stage", "")[:19] if step.get("stage") else ""
            estimate = step.get("estimated_duration")
            estimate_str = f"{estimate:.2f}s" if estimate is not None else "N/A"
            
            lines.append(f"{name:<40} {stage:<20} {estimate_str:<15}")
    
    lines.append("")
    lines.append("=" * 80)
    
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

