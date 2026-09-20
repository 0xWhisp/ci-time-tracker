"""Data models for ci-time-tracker.

This module contains all dataclasses used across the application for
representing pipeline configurations, build logs, statistics, and reports.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass
class PipelineStep:
    """A discrete unit of work within a CI/CD pipeline.
    
    Attributes:
        name: The step identifier/name
        stage: Optional stage grouping for the step
        command: Optional command or script executed by the step
        estimated_duration: Optional estimated runtime in seconds
    """
    name: str
    stage: str | None = None
    command: str | None = None
    estimated_duration: float | None = None

    def validate(self) -> list[str]:
        """Validate the step data and return list of validation errors."""
        errors = []
        if not self.name or not self.name.strip():
            errors.append("Step name cannot be empty")
        if self.estimated_duration is not None and self.estimated_duration < 0:
            errors.append("Estimated duration cannot be negative")
        return errors


@dataclass
class PipelineConfig:
    """Parsed CI/CD pipeline configuration.
    
    Attributes:
        provider: CI provider name (github, gitlab, circleci)
        steps: List of pipeline steps
        stages: List of stage names in order
        metadata: Additional provider-specific metadata
    """
    provider: str
    steps: list[PipelineStep] = field(default_factory=list)
    stages: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """Validate the config and return list of validation errors."""
        errors = []
        if not self.provider or not self.provider.strip():
            errors.append("Provider cannot be empty")
        for step in self.steps:
            step_errors = step.validate()
            errors.extend(step_errors)
        return errors


@dataclass
class StepExecution:
    """A single execution of a pipeline step from a build log.
    
    Attributes:
        name: The step identifier/name
        start_time: When the step started executing
        end_time: When the step finished executing
        duration_seconds: Computed or extracted duration in seconds
        status: Execution result (success, failure, skipped)
        is_retry: Whether this execution was a retry attempt
        job_name: Name of the job this step ran in, when known
        runner: Label or name of the machine that ran the step, when known
    """
    name: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    duration_seconds: float | None = None
    status: Literal["success", "failure", "skipped"] = "success"
    is_retry: bool = False
    job_name: str | None = None
    runner: str | None = None

    def validate(self) -> list[str]:
        """Validate the step execution and return list of validation errors."""
        errors = []
        if not self.name or not self.name.strip():
            errors.append("Step name cannot be empty")
        if self.duration_seconds is not None and self.duration_seconds < 0:
            errors.append("Duration cannot be negative")
        if self.start_time and self.end_time and self.start_time > self.end_time:
            errors.append("Start time cannot be after end time")
        return errors


@dataclass
class BuildLog:
    """Parsed build log containing step executions.
    
    Attributes:
        build_id: Optional identifier for the build
        steps: List of step executions from the log
        total_duration: Total build duration in seconds
        timestamp: When the build occurred
        head_sha: Commit the build ran against, when known
        run_attempt: Attempt number of the build (1 unless it was re-run)
        conclusion: Overall build result as reported by the provider
        metadata: Additional provider-specific data (jobs, queue times, ...)
    """
    build_id: str | None = None
    steps: list[StepExecution] = field(default_factory=list)
    total_duration: float | None = None
    timestamp: datetime | None = None
    head_sha: str | None = None
    run_attempt: int = 1
    conclusion: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """Validate the build log and return list of validation errors."""
        errors = []
        if self.total_duration is not None and self.total_duration < 0:
            errors.append("Total duration cannot be negative")
        for step in self.steps:
            step_errors = step.validate()
            errors.extend(step_errors)
        return errors


@dataclass
class StepStatistics:
    """Aggregated statistics for a pipeline step across multiple builds.
    
    Attributes:
        name: The step identifier/name
        execution_count: Total number of executions
        success_count: Number of successful executions
        failure_count: Number of failed executions
        durations: List of recorded durations in seconds
        p50: 50th percentile duration
        p90: 90th percentile duration
        p95: 95th percentile duration
        p99: 99th percentile duration
        is_slow: Whether the step is flagged as slow
        is_flaky: Whether the step is flagged as flaky
        failure_rate: Ratio of failures to total executions
    """
    name: str
    execution_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    durations: list[float] = field(default_factory=list)
    p50: float | None = None
    p90: float | None = None
    p95: float | None = None
    p99: float | None = None
    is_slow: bool = False
    is_flaky: bool = False
    failure_rate: float = 0.0

    def validate(self) -> list[str]:
        """Validate the statistics and return list of validation errors."""
        errors = []
        if not self.name or not self.name.strip():
            errors.append("Step name cannot be empty")
        if self.execution_count < 0:
            errors.append("Execution count cannot be negative")
        if self.success_count < 0:
            errors.append("Success count cannot be negative")
        if self.failure_count < 0:
            errors.append("Failure count cannot be negative")
        if self.success_count + self.failure_count > self.execution_count:
            errors.append("Success + failure count cannot exceed execution count")
        if not (0.0 <= self.failure_rate <= 1.0):
            errors.append("Failure rate must be between 0 and 1")
        for d in self.durations:
            if d < 0:
                errors.append("Duration values cannot be negative")
                break
        return errors


@dataclass
class AnalysisResult:
    """Result of analyzing pipeline config or build logs.
    
    Attributes:
        mode: Analysis mode (config or log)
        steps: List of step statistics (log mode) or pipeline steps (config mode)
        total_estimated_duration: Sum of estimated durations for config mode
        slowest_steps: Names of steps flagged as slow
        flaky_steps: Names of steps flagged as flaky
        metadata: Additional analysis metadata
    """
    mode: Literal["config", "log"]
    steps: list[StepStatistics] | list[PipelineStep] = field(default_factory=list)
    total_estimated_duration: float | None = None
    slowest_steps: list[str] = field(default_factory=list)
    flaky_steps: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> list[str]:
        """Validate the analysis result and return list of validation errors."""
        errors = []
        if self.mode not in ("config", "log"):
            errors.append("Mode must be 'config' or 'log'")
        if self.total_estimated_duration is not None and self.total_estimated_duration < 0:
            errors.append("Total estimated duration cannot be negative")
        return errors


@dataclass
class Report:
    """Final report generated from analysis results.
    
    Attributes:
        title: Report title
        mode: Analysis mode that produced this report
        generated_at: Timestamp when report was generated
        summary: Summary statistics and counts
        steps: List of step data dictionaries
        issues: List of detected issues (slow, flaky steps)
    """
    title: str
    mode: str
    generated_at: datetime
    summary: dict[str, Any] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)

    def validate(self) -> list[str]:
        """Validate the report and return list of validation errors."""
        errors = []
        if not self.title or not self.title.strip():
            errors.append("Report title cannot be empty")
        if not self.mode or not self.mode.strip():
            errors.append("Report mode cannot be empty")
        return errors

    def to_dict(self) -> dict[str, Any]:
        """Convert report to dictionary for JSON serialization."""
        return {
            "title": self.title,
            "mode": self.mode,
            "generated_at": self.generated_at.isoformat(),
            "summary": self.summary,
            "steps": self.steps,
            "issues": self.issues,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Report":
        """Create Report from dictionary (JSON deserialization)."""
        return cls(
            title=data["title"],
            mode=data["mode"],
            generated_at=datetime.fromisoformat(data["generated_at"]),
            summary=data.get("summary", {}),
            steps=data.get("steps", []),
            issues=data.get("issues", []),
        )
