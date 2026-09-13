"""Log parser module for ci-time-tracker.

This module provides functionality to parse CI/CD build logs in various formats
(text and JSON) and extract step timing information.
"""

import json
import re
from datetime import datetime, timedelta
from typing import Any, Literal

from ci_time_tracker.models import BuildLog, StepExecution


class LogParseError(Exception):
    """Exception raised when log parsing fails."""
    
    def __init__(self, message: str, position: int | None = None):
        self.message = message
        self.position = position
        super().__init__(self._format_message())
    
    def _format_message(self) -> str:
        if self.position is not None:
            return f"{self.message} (position: {self.position})"
        return self.message


def detect_format(content: str) -> Literal["text", "json"]:
    """Auto-detect the format of log content.
    
    Attempts to determine if the content is JSON or plain text by checking
    if it starts with JSON structural characters after stripping whitespace.
    
    Args:
        content: The raw log content to analyze
        
    Returns:
        "json" if content appears to be JSON, "text" otherwise
    """
    stripped = content.strip()
    
    if not stripped:
        return "text"
    
    # JSON logs typically start with { or [
    if stripped.startswith('{') or stripped.startswith('['):
        # Verify it's actually valid JSON
        try:
            json.loads(stripped)
            return "json"
        except json.JSONDecodeError:
            return "text"
    
    return "text"


def parse_log(content: str, format: Literal["text", "json"] | None = None) -> BuildLog:
    """Parse build log content and return structured data.
    
    Main entry point for log parsing. Automatically detects format if not
    specified, then delegates to the appropriate parser.
    
    Args:
        content: The raw log content to parse
        format: Optional format hint ("text" or "json"). If None, auto-detects.
        
    Returns:
        BuildLog containing extracted step executions and timing data
        
    Raises:
        LogParseError: If the log cannot be parsed
    """
    if format is None:
        format = detect_format(content)
    
    if format == "json":
        return parse_json_log(content)
    else:
        return parse_text_log(content)


def parse_text_log(content: str) -> BuildLog:
    """Parse plain text log with timestamp patterns.
    
    Extracts step timing information from plain text CI logs by matching
    common timestamp and step marker patterns. Supports GitHub Actions,
    GitLab CI, and CircleCI log formats.
    
    Args:
        content: Plain text log content
        
    Returns:
        BuildLog with extracted step executions
    """
    steps: list[StepExecution] = []
    build_id: str | None = None
    build_timestamp: datetime | None = None
    total_duration: float | None = None
    
    # Try different CI provider patterns
    github_steps = _parse_github_actions_log(content)
    if github_steps:
        steps = github_steps
    else:
        gitlab_steps = _parse_gitlab_log(content)
        if gitlab_steps:
            steps = gitlab_steps
        else:
            circleci_steps = _parse_circleci_log(content)
            if circleci_steps:
                steps = circleci_steps
            else:
                # Fall back to generic timestamp parsing
                steps = _parse_generic_log(content)
    
    # Extract build ID if present
    build_id_match = re.search(r'(?:build|run|job)[_\s#:]*(\d+)', content, re.IGNORECASE)
    if build_id_match:
        build_id = build_id_match.group(1)
    
    # Extract build timestamp from first timestamp found
    if steps and steps[0].start_time:
        build_timestamp = steps[0].start_time
    
    # Calculate total duration if we have step data
    if steps:
        durations = [s.duration_seconds for s in steps if s.duration_seconds is not None]
        if durations:
            total_duration = sum(durations)
    
    return BuildLog(
        build_id=build_id,
        steps=steps,
        total_duration=total_duration,
        timestamp=build_timestamp
    )


# Timestamp patterns for various formats
_TIMESTAMP_PATTERNS = [
    # ISO 8601: 2024-01-15T10:30:45Z or 2024-01-15T10:30:45.123Z
    (r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)', '%Y-%m-%dT%H:%M:%S'),
    # Common log format: 2024-01-15 10:30:45
    (r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', '%Y-%m-%d %H:%M:%S'),
    # GitHub Actions format: 2024-01-15T10:30:45.1234567Z
    (r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.\d+Z?', '%Y-%m-%dT%H:%M:%S'),
]


def _parse_timestamp(text: str) -> datetime | None:
    """Extract and parse timestamp from text."""
    for pattern, fmt in _TIMESTAMP_PATTERNS:
        match = re.search(pattern, text)
        if match:
            try:
                ts_str = match.group(1).replace('Z', '').strip()
                return datetime.strptime(ts_str, fmt)
            except ValueError:
                continue
    return None


_GHA_TIMESTAMP = r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)'

# "##[group]<title>", optionally prefixed by the line timestamp
_GHA_GROUP_PATTERN = re.compile(
    rf'^(?:{_GHA_TIMESTAMP}\s+)?##\[group\](.*?)[ \t\r]*$',
    re.MULTILINE
)

# Lines the runner emits once the job's own steps have finished
_GHA_JOB_TEARDOWN_PATTERN = re.compile(
    rf'^(?:{_GHA_TIMESTAMP}\s+)?(?:Post job cleanup\.|Cleaning up orphan processes)',
    re.MULTILINE
)

_GHA_LINE_TIMESTAMP_PATTERN = re.compile(rf'^{_GHA_TIMESTAMP}\s', re.MULTILINE)


def _parse_gha_timestamp(value: str | None) -> datetime | None:
    """Parse a GitHub Actions timestamp, keeping sub-second precision."""
    if not value:
        return None
    match = re.match(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?', value)
    if not match:
        return None
    timestamp = datetime.strptime(match.group(1), '%Y-%m-%dT%H:%M:%S')
    if match.group(2):
        # Runner logs use 7 fractional digits; datetime supports 6
        timestamp += timedelta(microseconds=int(match.group(2)[:6].ljust(6, '0')))
    return timestamp


def _parse_github_actions_log(content: str) -> list[StepExecution]:
    """Parse GitHub Actions log format.

    Job logs prefix every line with a timestamp and open each step with a
    ``##[group]Run <name>`` header:

    2024-01-15T10:30:45.1234567Z ##[group]Run npm test
    2024-01-15T10:30:45.1234567Z npm test
    2024-01-15T10:30:45.1234567Z ##[endgroup]
    2024-01-15T10:32:10.0000000Z ...step output...

    ``##[endgroup]`` only closes the header block (the step's inputs), so a
    step runs until the next step header or the job teardown, and the last
    step until its final timestamped line. Groups without ``Run`` are nested
    sections of the current step; if a log has no ``Run`` headers at all,
    every group is treated as a step.
    """
    steps: list[StepExecution] = []

    groups = list(_GHA_GROUP_PATTERN.finditer(content))
    headers = [m for m in groups if m.group(2).startswith('Run ')] or groups
    boundaries = sorted(
        headers + list(_GHA_JOB_TEARDOWN_PATTERN.finditer(content)),
        key=lambda m: m.start()
    )

    for header in headers:
        step_name = header.group(2)
        if step_name.startswith('Run '):
            step_name = step_name[len('Run '):]
        step_name = step_name.strip()

        # The step ends where the next step (or the job teardown) begins
        boundary = next((b for b in boundaries if b.start() > header.start()), None)
        step_content = content[header.end():boundary.start() if boundary else len(content)]

        start_time = _parse_gha_timestamp(header.group(1))
        end_time = _parse_gha_timestamp(boundary.group(1)) if boundary else None
        if end_time is None:
            line_timestamps = _GHA_LINE_TIMESTAMP_PATTERN.findall(step_content)
            if line_timestamps:
                end_time = _parse_gha_timestamp(line_timestamps[-1])

        duration = None
        if start_time and end_time:
            duration = (end_time - start_time).total_seconds()

        status: Literal["success", "failure", "skipped"] = "success"
        exit_code_match = re.search(r'Process completed with exit code (\d+)', step_content)
        if '##[error]' in step_content:
            status = "failure"
        elif exit_code_match and exit_code_match.group(1) != '0':
            status = "failure"
        
        # Check for retry indicator
        is_retry = bool(re.search(r'retry|attempt\s*[2-9]', step_name, re.IGNORECASE))
        
        steps.append(StepExecution(
            name=step_name,
            start_time=start_time,
            end_time=end_time,
            duration_seconds=duration,
            status=status,
            is_retry=is_retry
        ))
    
    return steps


def _parse_gitlab_log(content: str) -> list[StepExecution]:
    """Parse GitLab CI log format.
    
    GitLab logs have patterns like:
    section_start:1234567890:step_name
    section_end:1234567890:step_name
    Or ANSI-colored output with timestamps.
    """
    steps: list[StepExecution] = []

    # Pattern for GitLab section markers. Names allow letters, digits, '_',
    # '.' and '-'; options like "[collapsed=true]" and ANSI codes follow them.
    section_start_pattern = re.compile(
        r'section_start:(\d+):([\w.-]+)',
        re.MULTILINE
    )
    section_end_pattern = re.compile(
        r'section_end:(\d+):([\w.-]+)',
        re.MULTILINE
    )

    # Failure evidence inside a section, and the runner's job-level verdict
    step_failure_pattern = re.compile(r'exit code [1-9]|ERROR: Job failed')
    job_failed_pattern = re.compile(r'ERROR: Job failed')

    # Also try to match "Running with gitlab-runner" style logs
    job_pattern = re.compile(
        r'(?:Executing|Running)\s+"([^"]+)"',
        re.MULTILINE
    )

    starts = {m.group(2): m for m in section_start_pattern.finditer(content)}
    ends = {m.group(2): m for m in section_end_pattern.finditer(content)}

    if starts:
        for name, start_match in starts.items():
            start_ts = int(start_match.group(1))
            start_time = datetime.fromtimestamp(start_ts)
            end_time = None
            duration = None
            section_end = len(content)

            end_match = ends.get(name)
            if end_match and end_match.start() > start_match.start():
                end_ts = int(end_match.group(1))
                end_time = datetime.fromtimestamp(end_ts)
                duration = float(end_ts - start_ts)
                section_end = end_match.start()

            # Check for failure within the section's own output
            status: Literal["success", "failure", "skipped"] = "success"
            if step_failure_pattern.search(content[start_match.end():section_end]):
                status = "failure"

            is_retry = bool(re.search(r'retry|attempt\s*[2-9]', name, re.IGNORECASE))

            steps.append(StepExecution(
                name=name,
                start_time=start_time,
                end_time=end_time,
                duration_seconds=duration,
                status=status,
                is_retry=is_retry
            ))

        # The runner reports a failed job after all sections have closed;
        # attribute it to the job script when no section showed the failure.
        if job_failed_pattern.search(content) and not any(s.status == "failure" for s in steps):
            culprit = next((s for s in steps if s.name == "step_script"), steps[-1])
            culprit.status = "failure"
    else:
        # Try job pattern
        for match in job_pattern.finditer(content):
            steps.append(StepExecution(
                name=match.group(1),
                status="success",
                is_retry=False
            ))
    
    return steps


def _parse_circleci_log(content: str) -> list[StepExecution]:
    """Parse CircleCI log format.
    
    CircleCI logs have patterns like:
    #!/bin/bash -eo pipefail
    step: Step Name
    Or timing output like:
    real    0m5.123s
    """
    steps: list[StepExecution] = []
    
    # Pattern for CircleCI step markers
    step_pattern = re.compile(
        r'(?:^|\n)\s*(?:step:|Running step:)\s*(.+?)(?:\s*$)',
        re.MULTILINE | re.IGNORECASE
    )
    
    # Alternative pattern for CircleCI command output
    command_pattern = re.compile(
        r'#!/bin/bash.*?\n(.+?)(?=\n#!/bin/bash|\Z)',
        re.DOTALL
    )
    
    # Pattern for timing output
    timing_pattern = re.compile(
        r'real\s+(\d+)m([\d.]+)s',
        re.MULTILINE
    )
    
    step_matches = list(step_pattern.finditer(content))
    
    if step_matches:
        timing_matches = list(timing_pattern.finditer(content))
        
        for i, match in enumerate(step_matches):
            step_name = match.group(1).strip()
            duration = None
            
            # Try to find corresponding timing
            if i < len(timing_matches):
                minutes = int(timing_matches[i].group(1))
                seconds = float(timing_matches[i].group(2))
                duration = minutes * 60 + seconds
            
            # Check for failure
            status: Literal["success", "failure", "skipped"] = "success"
            step_end = step_matches[i + 1].start() if i + 1 < len(step_matches) else len(content)
            step_content = content[match.end():step_end]
            
            if re.search(r'(?:failed|error|exit code [1-9]|Exited with code [1-9])', step_content, re.IGNORECASE):
                status = "failure"
            
            is_retry = bool(re.search(r'retry|attempt\s*[2-9]', step_name, re.IGNORECASE))
            
            steps.append(StepExecution(
                name=step_name,
                duration_seconds=duration,
                status=status,
                is_retry=is_retry
            ))
    
    return steps


def _parse_generic_log(content: str) -> list[StepExecution]:
    """Parse generic log format with timestamps.
    
    Attempts to extract step information from logs that don't match
    specific CI provider formats.
    """
    steps: list[StepExecution] = []
    
    # Generic patterns for step markers
    patterns = [
        # "Step: name" or "Step 1: name" - match after timestamp if present
        re.compile(r'(?:^|\n)(?:[\d\-T:.\sZ]+)?\s*[Ss]tep(?:\s*\d*)?[:\s]+(.+?)(?:\s*$)', re.MULTILINE),
        # "Running: name" or "Executing: name"
        re.compile(r'(?:^|\n)(?:[\d\-T:.\sZ]+)?\s*(?:running|executing)[:\s]+(.+?)(?:\s*$)', re.IGNORECASE | re.MULTILINE),
        # "[timestamp] Starting: name"
        re.compile(r'(?:^|\n)(?:[\d\-T:.\sZ]+)?\s*(?:starting|begin)[:\s]+(.+?)(?:\s*$)', re.IGNORECASE | re.MULTILINE),
    ]
    
    for pattern in patterns:
        matches = list(pattern.finditer(content))
        if matches:
            for match in matches:
                step_name = match.group(1).strip()
                if step_name and len(step_name) < 200:  # Sanity check
                    # Try to extract timestamp near this match
                    context_start = max(0, match.start() - 50)
                    context = content[context_start:match.end()]
                    start_time = _parse_timestamp(context)
                    
                    steps.append(StepExecution(
                        name=step_name,
                        start_time=start_time,
                        status="success",
                        is_retry=bool(re.search(r'retry|attempt\s*[2-9]', step_name, re.IGNORECASE))
                    ))
            break  # Use first pattern that matches
    
    return steps


def parse_json_log(content: str) -> BuildLog:
    """Parse JSON-formatted log.
    
    Extracts timing data from structured JSON log fields. Supports various
    JSON log formats including:
    - Array of step objects with name, start_time, end_time, status
    - Object with steps/jobs array
    - GitHub Actions JSON format
    - GitLab CI JSON format
    
    Args:
        content: JSON log content
        
    Returns:
        BuildLog with extracted step executions
        
    Raises:
        LogParseError: If JSON is invalid or missing required fields
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise LogParseError(f"Invalid JSON syntax: {e.msg}", e.pos)
    
    steps: list[StepExecution] = []
    build_id: str | None = None
    build_timestamp: datetime | None = None
    total_duration: float | None = None
    
    # Handle different JSON structures
    if isinstance(data, list):
        # Array of step objects
        steps = _parse_json_steps_array(data)
    elif isinstance(data, dict):
        # Extract build metadata
        build_id = _extract_build_id(data)
        build_timestamp = _extract_timestamp(data)
        total_duration = _extract_total_duration(data)
        
        # Find steps in various locations
        steps = _extract_steps_from_dict(data)
    
    # Calculate total duration if not provided
    if total_duration is None and steps:
        durations = [s.duration_seconds for s in steps if s.duration_seconds is not None]
        if durations:
            total_duration = sum(durations)
    
    # Extract build timestamp from first step if not found
    if build_timestamp is None and steps and steps[0].start_time:
        build_timestamp = steps[0].start_time
    
    return BuildLog(
        build_id=build_id,
        steps=steps,
        total_duration=total_duration,
        timestamp=build_timestamp
    )


def _parse_json_steps_array(data: list) -> list[StepExecution]:
    """Parse an array of step objects."""
    steps: list[StepExecution] = []
    
    for item in data:
        if isinstance(item, dict):
            step = _parse_json_step(item)
            if step:
                steps.append(step)
    
    return steps


def _first_present(data: dict, *keys: str) -> Any:
    """Return the value of the first key holding a non-null, non-empty value.
    
    Unlike chaining ``data.get(a) or data.get(b)``, this keeps falsy but
    meaningful values such as ``0``.
    """
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _as_number(value: Any) -> float | None:
    """Convert a JSON value to a float, or None if it is not numeric."""
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _parse_json_step(data: dict) -> StepExecution | None:
    """Parse a single step object from JSON."""
    # Try various field names for step name
    name = _first_present(
        data, "name", "step_name", "stepName", "step", "job", "job_name", "jobName"
    )
    
    if name is None:
        return None
    
    # Parse timestamps
    start_time = _parse_json_timestamp(_first_present(
        data, "start_time", "startTime", "started_at", "startedAt", "start"
    ))
    
    end_time = _parse_json_timestamp(_first_present(
        data, "end_time", "endTime", "finished_at", "finishedAt", "end",
        "completed_at", "completedAt"
    ))
    
    # Parse duration
    duration = _parse_json_duration(data)
    
    # Calculate duration from timestamps if not provided
    if duration is None and start_time and end_time:
        duration = (end_time - start_time).total_seconds()
    
    # Parse status
    status = _parse_json_status(data)
    
    # Check for retry
    attempt = _as_number(data.get("attempt"))
    retry_count = _as_number(data.get("retry_count"))
    is_retry = (
        any(bool(data.get(key)) for key in ("is_retry", "isRetry", "retry")) or
        (attempt is not None and attempt > 1) or
        (retry_count is not None and retry_count > 0)
    )
    
    return StepExecution(
        name=str(name),
        start_time=start_time,
        end_time=end_time,
        duration_seconds=duration,
        status=status,
        is_retry=is_retry
    )


def _parse_json_timestamp(value: str | int | float | None) -> datetime | None:
    """Parse a timestamp from various JSON formats."""
    if value is None:
        return None
    
    if isinstance(value, (int, float)):
        # Unix timestamp
        try:
            return datetime.fromtimestamp(value)
        except (ValueError, OSError):
            return None
    
    if isinstance(value, str):
        # Try ISO format first
        try:
            # Handle Z suffix
            clean_value = value.replace('Z', '+00:00')
            return datetime.fromisoformat(clean_value)
        except ValueError:
            pass
        
        # Try common formats
        formats = [
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%dT%H:%M:%S.%f',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d %H:%M:%S.%f',
        ]
        for fmt in formats:
            try:
                return datetime.strptime(value.split('+')[0].split('Z')[0], fmt)
            except ValueError:
                continue
    
    return None


# Duration field names, in lookup order, with the divisor that converts them to seconds
_DURATION_FIELDS = (
    ("duration", 1.0),
    ("duration_seconds", 1.0),
    ("durationSeconds", 1.0),
    ("duration_ms", 1000.0),
    ("durationMs", 1000.0),
    ("elapsed", 1.0),
    ("elapsed_time", 1.0),
    ("elapsedTime", 1.0),
)


def _parse_json_duration(data: dict) -> float | None:
    """Extract duration in seconds from JSON step data."""
    for key, divisor in _DURATION_FIELDS:
        if _first_present(data, key) is None:
            continue
        duration = _as_number(data[key])
        return duration / divisor if duration is not None else None
    
    return None


def _parse_json_status(data: dict) -> Literal["success", "failure", "skipped"]:
    """Extract status from JSON step data."""
    status_value = _first_present(data, "status", "conclusion", "result", "state")
    
    if status_value is None:
        return "success"
    
    status_str = str(status_value).lower()
    
    if status_str in ("success", "passed", "completed", "ok", "0"):
        return "success"
    elif status_str in ("failure", "failed", "error", "errored", "1"):
        return "failure"
    elif status_str in ("skipped", "skip", "cancelled", "canceled", "pending", "neutral"):
        return "skipped"
    
    return "success"


def _extract_build_id(data: dict) -> str | None:
    """Extract build ID from JSON data."""
    build_id = _first_present(
        data, "build_id", "buildId", "id", "run_id", "runId", "job_id", "jobId"
    )
    return str(build_id) if build_id is not None else None


def _extract_timestamp(data: dict) -> datetime | None:
    """Extract build timestamp from JSON data."""
    timestamp = _first_present(
        data, "timestamp", "created_at", "createdAt", "started_at", "startedAt"
    )
    return _parse_json_timestamp(timestamp)


def _extract_total_duration(data: dict) -> float | None:
    """Extract total duration from JSON data."""
    duration = _first_present(data, "total_duration", "totalDuration", "duration", "elapsed")
    return _as_number(duration) if duration is not None else None


def _extract_steps_from_dict(data: dict) -> list[StepExecution]:
    """Extract steps from various JSON object structures."""
    steps: list[StepExecution] = []
    
    # Try common field names for steps array
    steps_data = (
        data.get("steps") or
        data.get("jobs") or
        data.get("stages") or
        data.get("tasks") or
        data.get("workflow_steps") or
        data.get("workflowSteps")
    )
    
    if isinstance(steps_data, list):
        steps = _parse_json_steps_array(steps_data)
    elif isinstance(steps_data, dict):
        # Steps might be a dict with step names as keys
        for name, step_data in steps_data.items():
            if isinstance(step_data, dict):
                step_data_with_name = {**step_data, "name": name}
                step = _parse_json_step(step_data_with_name)
                if step:
                    steps.append(step)
    
    # If no steps found, check for nested job structures (GitHub Actions style)
    if not steps and "jobs" in data and isinstance(data["jobs"], dict):
        for job_name, job_data in data["jobs"].items():
            if isinstance(job_data, dict):
                job_steps = job_data.get("steps", [])
                if isinstance(job_steps, list):
                    for step_data in job_steps:
                        if isinstance(step_data, dict):
                            step = _parse_json_step(step_data)
                            if step:
                                steps.append(step)
    
    return steps
