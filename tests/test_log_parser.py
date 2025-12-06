"""Tests for log parser module.

Tests cover:
- Text log parsing with various timestamp formats
- JSON log parsing
- Handling of logs with missing timing data
- Requirements: 6.3, 7.4
"""

import pytest
from datetime import datetime

from ci_time_tracker.log_parser import (
    parse_log,
    parse_text_log,
    parse_json_log,
    detect_format,
    LogParseError,
)
from ci_time_tracker.models import BuildLog


class TestFormatDetection:
    """Tests for log format auto-detection."""

    def test_detect_json_array(self):
        """Detect JSON array format."""
        content = '[{"name": "step1"}]'
        assert detect_format(content) == "json"

    def test_detect_json_object(self):
        """Detect JSON object format."""
        content = '{"steps": []}'
        assert detect_format(content) == "json"

    def test_detect_text_log(self):
        """Detect plain text log format."""
        content = "2024-01-15 10:30:00 Starting build..."
        assert detect_format(content) == "text"

    def test_detect_empty_as_text(self):
        """Empty content defaults to text format."""
        assert detect_format("") == "text"
        assert detect_format("   ") == "text"

    def test_detect_invalid_json_as_text(self):
        """Invalid JSON starting with { is detected as text."""
        content = "{invalid json content"
        assert detect_format(content) == "text"


class TestTextLogParsing:
    """Tests for text log parsing with various formats."""

    def test_parse_github_actions_log(self):
        """Parse GitHub Actions log format."""
        log = """
2024-01-15T10:30:45.1234567Z ##[group]Run npm install
2024-01-15T10:30:45.1234567Z npm install
2024-01-15T10:31:00.1234567Z ##[endgroup]
2024-01-15T10:31:00.1234567Z ##[group]Run npm test
2024-01-15T10:31:00.1234567Z npm test
2024-01-15T10:31:30.1234567Z ##[endgroup]
"""
        result = parse_text_log(log)
        assert len(result.steps) == 2
        assert result.steps[0].name == "npm install"
        assert result.steps[1].name == "npm test"
        assert result.steps[0].duration_seconds == 15.0
        assert result.steps[1].duration_seconds == 30.0

    def test_parse_github_actions_failure(self):
        """Parse GitHub Actions log with failure."""
        log = """
2024-01-15T10:30:45.1234567Z ##[group]Run npm test
2024-01-15T10:30:45.1234567Z npm test
2024-01-15T10:31:00.1234567Z ##[error]Tests failed
2024-01-15T10:31:00.1234567Z ##[endgroup]
"""
        result = parse_text_log(log)
        assert len(result.steps) == 1
        assert result.steps[0].status == "failure"

    def test_parse_gitlab_ci_log(self):
        """Parse GitLab CI log format with section markers."""
        log = """
section_start:1705315845:install
Installing dependencies...
section_end:1705315860:install
section_start:1705315860:test
Running tests...
section_end:1705315890:test
"""
        result = parse_text_log(log)
        assert len(result.steps) == 2
        step_names = [s.name for s in result.steps]
        assert "install" in step_names
        assert "test" in step_names
        # Check duration calculation
        install_step = next(s for s in result.steps if s.name == "install")
        assert install_step.duration_seconds == 15.0

    def test_parse_circleci_log(self):
        """Parse CircleCI log format."""
        log = """
step: Install dependencies
npm install
real    0m15.123s

step: Run tests
npm test
real    0m30.456s
"""
        result = parse_text_log(log)
        assert len(result.steps) == 2
        assert result.steps[0].name == "Install dependencies"
        assert result.steps[0].duration_seconds == 15.123
        assert result.steps[1].name == "Run tests"
        assert result.steps[1].duration_seconds == 30.456

    def test_parse_generic_log_with_step_markers(self):
        """Parse generic log with Step: markers."""
        log = """
2024-01-15 10:30:45 Step: Build project
Building...
2024-01-15 10:31:00 Step: Deploy
Deploying...
"""
        result = parse_text_log(log)
        assert len(result.steps) == 2
        assert result.steps[0].name == "Build project"
        assert result.steps[1].name == "Deploy"

    def test_parse_log_extracts_build_id(self):
        """Extract build ID from log content."""
        log = """
Build #12345 started
2024-01-15T10:30:45.1234567Z ##[group]Run step
2024-01-15T10:31:00.1234567Z ##[endgroup]
"""
        result = parse_text_log(log)
        assert result.build_id == "12345"

    def test_parse_empty_log(self):
        """Parse empty log returns empty BuildLog."""
        result = parse_text_log("")
        assert len(result.steps) == 0
        assert result.build_id is None

    def test_parse_log_with_retry_indicator(self):
        """Detect retry attempts in step names."""
        log = """
2024-01-15T10:30:45.1234567Z ##[group]Run tests (retry attempt 2)
2024-01-15T10:31:00.1234567Z ##[endgroup]
"""
        result = parse_text_log(log)
        assert len(result.steps) == 1
        assert result.steps[0].is_retry == True


class TestJsonLogParsing:
    """Tests for JSON log parsing."""

    def test_parse_json_array_of_steps(self):
        """Parse JSON array of step objects."""
        log = """
[
  {"name": "Install", "start_time": "2024-01-15T10:30:00Z", "end_time": "2024-01-15T10:30:15Z", "status": "success"},
  {"name": "Test", "start_time": "2024-01-15T10:30:15Z", "end_time": "2024-01-15T10:30:45Z", "status": "failure"}
]
"""
        result = parse_json_log(log)
        assert len(result.steps) == 2
        assert result.steps[0].name == "Install"
        assert result.steps[0].duration_seconds == 15.0
        assert result.steps[0].status == "success"
        assert result.steps[1].name == "Test"
        assert result.steps[1].status == "failure"

    def test_parse_json_object_with_steps(self):
        """Parse JSON object containing steps array."""
        log = """
{
  "build_id": "12345",
  "steps": [
    {"name": "Checkout", "duration": 5.0, "status": "success"},
    {"name": "Build", "duration": 30.0, "status": "success"}
  ]
}
"""
        result = parse_json_log(log)
        assert result.build_id == "12345"
        assert len(result.steps) == 2
        assert result.steps[0].name == "Checkout"
        assert result.steps[0].duration_seconds == 5.0

    def test_parse_json_with_unix_timestamps(self):
        """Parse JSON with Unix timestamps."""
        log = '[{"name": "Step1", "start_time": 1705315800, "end_time": 1705315815}]'
        result = parse_json_log(log)
        assert len(result.steps) == 1
        assert result.steps[0].duration_seconds == 15.0

    def test_parse_json_with_camelcase_fields(self):
        """Parse JSON with camelCase field names."""
        log = '[{"stepName": "CamelStep", "startTime": "2024-01-15T10:30:00Z", "endTime": "2024-01-15T10:30:10Z"}]'
        result = parse_json_log(log)
        assert result.steps[0].name == "CamelStep"
        assert result.steps[0].duration_seconds == 10.0

    def test_parse_json_with_retry_flag(self):
        """Parse JSON with retry indicators."""
        log = """
[
  {"name": "Flaky", "is_retry": true, "status": "success"},
  {"name": "Retry2", "attempt": 2, "status": "success"}
]
"""
        result = parse_json_log(log)
        assert result.steps[0].is_retry == True
        assert result.steps[1].is_retry == True

    def test_parse_json_empty_array(self):
        """Parse empty JSON array."""
        result = parse_json_log("[]")
        assert len(result.steps) == 0

    def test_parse_json_invalid_syntax_raises_error(self):
        """Invalid JSON raises LogParseError."""
        with pytest.raises(LogParseError) as exc_info:
            parse_json_log("{invalid json}")
        assert "Invalid JSON syntax" in str(exc_info.value)
        assert exc_info.value.position is not None

    def test_parse_json_various_status_values(self):
        """Parse various status value formats."""
        log = """
[
  {"name": "s1", "status": "passed"},
  {"name": "s2", "status": "failed"},
  {"name": "s3", "status": "skipped"},
  {"name": "s4", "conclusion": "success"},
  {"name": "s5", "result": "error"}
]
"""
        result = parse_json_log(log)
        assert result.steps[0].status == "success"
        assert result.steps[1].status == "failure"
        assert result.steps[2].status == "skipped"
        assert result.steps[3].status == "success"
        assert result.steps[4].status == "failure"


class TestMissingTimingData:
    """Tests for handling logs with missing timing data."""

    def test_text_log_no_timestamps(self):
        """Handle text log without timestamps."""
        log = """
step: Build
Building project...
step: Test
Running tests...
"""
        result = parse_text_log(log)
        # Should still extract step names
        assert len(result.steps) == 2
        # Duration should be None when timestamps missing
        assert result.steps[0].duration_seconds is None

    def test_json_log_missing_duration(self):
        """Handle JSON log with missing duration fields."""
        log = '[{"name": "Step1", "status": "success"}]'
        result = parse_json_log(log)
        assert len(result.steps) == 1
        assert result.steps[0].duration_seconds is None

    def test_json_log_missing_name_skipped(self):
        """Steps without name are skipped."""
        log = '[{"duration": 10.0}, {"name": "Valid", "duration": 5.0}]'
        result = parse_json_log(log)
        assert len(result.steps) == 1
        assert result.steps[0].name == "Valid"

    def test_total_duration_calculated_from_steps(self):
        """Total duration calculated when steps have durations."""
        log = """
[
  {"name": "s1", "duration": 10.0},
  {"name": "s2", "duration": 20.0}
]
"""
        result = parse_json_log(log)
        assert result.total_duration == 30.0


class TestParseLogAutoDetect:
    """Tests for parse_log with auto-detection."""

    def test_auto_detect_json(self):
        """Auto-detect and parse JSON format."""
        log = '[{"name": "Step1", "duration": 10.0}]'
        result = parse_log(log)
        assert len(result.steps) == 1
        assert result.steps[0].name == "Step1"

    def test_auto_detect_text(self):
        """Auto-detect and parse text format."""
        log = """
2024-01-15T10:30:45.1234567Z ##[group]Run step
2024-01-15T10:31:00.1234567Z ##[endgroup]
"""
        result = parse_log(log)
        assert len(result.steps) == 1

    def test_explicit_format_override(self):
        """Explicit format parameter overrides auto-detection."""
        # This looks like JSON but we force text parsing
        log = '{"name": "test"}'
        result = parse_log(log, format="text")
        # Text parser won't find steps in this
        assert len(result.steps) == 0
