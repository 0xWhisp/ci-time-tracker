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


class TestRealWorldLogFormats:
    """Regression tests using logs shaped like real CI runner output."""

    GITHUB_JOB_LOG = """\
2026-09-01T10:00:00.0000000Z Current runner version: '2.319.1'
2026-09-01T10:00:00.1000000Z ##[group]Operating System
2026-09-01T10:00:00.1000000Z Ubuntu
2026-09-01T10:00:00.1000000Z ##[endgroup]
2026-09-01T10:00:02.0000000Z ##[group]Run actions/checkout@v4
2026-09-01T10:00:02.0000000Z with:
2026-09-01T10:00:02.0000000Z   repository: acme/web
2026-09-01T10:00:02.0000000Z ##[endgroup]
2026-09-01T10:00:02.5000000Z Syncing repository: acme/web
2026-09-01T10:00:02.6000000Z ##[group]Getting Git version info
2026-09-01T10:00:03.0000000Z git version 2.46.0
2026-09-01T10:00:03.0000000Z ##[endgroup]
2026-09-01T10:00:06.5000000Z ##[group]Run npm ci
2026-09-01T10:00:06.5000000Z npm ci
2026-09-01T10:00:06.5000000Z shell: /usr/bin/bash -e {0}
2026-09-01T10:00:06.5000000Z ##[endgroup]
2026-09-01T10:01:36.7500000Z added 1204 packages in 90s
2026-09-01T10:01:37.0000000Z ##[group]Run npm test
2026-09-01T10:01:37.0000000Z npm test
2026-09-01T10:01:37.0000000Z ##[endgroup]
2026-09-01T10:04:10.0000000Z FAIL src/app.test.js
2026-09-01T10:04:12.2500000Z ##[error]Process completed with exit code 1.
2026-09-01T10:04:12.5000000Z Post job cleanup.
2026-09-01T10:04:13.0000000Z [command]/usr/bin/git version
2026-09-01T10:04:20.0000000Z Cleaning up orphan processes
"""

    GITLAB_JOB_LOG = (
        "\x1b[0KRunning with gitlab-runner 17.3.0 (a1b2c3d4)\x1b[0;m\n"
        "section_start:1756720800:prepare_executor\r\x1b[0K\x1b[36;1mPreparing the \"docker\" executor\x1b[0;m\n"
        "Using docker image node:22 ...\n"
        "section_end:1756720812:prepare_executor\r\x1b[0K\n"
        "section_start:1756720812:get_sources\r\x1b[0K\x1b[36;1mGetting source from Git repository\x1b[0;m\n"
        "section_end:1756720818:get_sources\r\x1b[0K\n"
        "section_start:1756720818:restore-cache[collapsed=true]\r\x1b[0K\x1b[36;1mRestoring cache\x1b[0;m\n"
        "section_end:1756720823:restore-cache\r\x1b[0K\n"
        "section_start:1756720823:step_script\r\x1b[0K\x1b[36;1mExecuting \"step_script\" stage of the job script\x1b[0;m\n"
        "$ npm test\n"
        "Tests: 1 failed, 41 passed, 42 total\n"
        "section_end:1756721003:step_script\r\x1b[0K\n"
        "section_start:1756721003:cleanup_file_variables\r\x1b[0K\x1b[36;1mCleaning up project directory\x1b[0;m\n"
        "section_end:1756721004:cleanup_file_variables\r\x1b[0K\n"
        "\x1b[31;1mERROR: Job failed: exit code 1\x1b[0;m\n"
    )

    def test_github_step_duration_spans_until_next_step(self):
        """Step duration runs to the next step, not to the header's ##[endgroup]."""
        result = parse_text_log(self.GITHUB_JOB_LOG)
        durations = {s.name: s.duration_seconds for s in result.steps}
        assert durations == {
            "actions/checkout@v4": 4.5,
            "npm ci": 90.5,
            "npm test": 155.5,  # ends at "Post job cleanup.", not at the last log line
        }

    def test_github_nested_groups_are_not_steps(self):
        """Groups without "Run" belong to the surrounding step."""
        result = parse_text_log(self.GITHUB_JOB_LOG)
        names = [s.name for s in result.steps]
        assert "Operating System" not in names
        assert "Getting Git version info" not in names

    def test_github_failure_attributed_to_failing_step_only(self):
        """An ##[error] marks only the step it appears in."""
        result = parse_text_log(self.GITHUB_JOB_LOG)
        statuses = {s.name: s.status for s in result.steps}
        assert statuses == {
            "actions/checkout@v4": "success",
            "npm ci": "success",
            "npm test": "failure",
        }

    def test_gitlab_sections_with_ansi_codes_and_special_names(self):
        """Section names with '-' and options parse, and durations are correct."""
        result = parse_text_log(self.GITLAB_JOB_LOG)
        durations = {s.name: s.duration_seconds for s in result.steps}
        assert durations == {
            "prepare_executor": 12.0,
            "get_sources": 6.0,
            "restore-cache": 5.0,
            "step_script": 180.0,
            "cleanup_file_variables": 1.0,
        }

    def test_gitlab_job_failure_attributed_to_step_script(self):
        """'ERROR: Job failed' after all sections marks step_script as failed."""
        result = parse_text_log(self.GITLAB_JOB_LOG)
        failed = [s.name for s in result.steps if s.status == "failure"]
        assert failed == ["step_script"]

    def test_gitlab_successful_job_has_no_failures(self):
        """Output mentioning 'failed' or 'error' does not mark a step as failed."""
        log = (
            "section_start:1756720800:step_script\r\x1b[0K\n"
            "Tests: 0 failed, 42 passed; no errors\n"
            "section_end:1756720860:step_script\r\x1b[0K\n"
            "\x1b[32;1mJob succeeded\x1b[0;m\n"
        )
        result = parse_text_log(log)
        assert [s.status for s in result.steps] == ["success"]

    def test_json_duration_in_milliseconds_is_converted(self):
        """duration_ms and durationMs are reported in seconds."""
        log = '[{"name": "build", "duration_ms": 120000}, {"name": "test", "durationMs": 1500}]'
        result = parse_json_log(log)
        assert result.steps[0].duration_seconds == 120.0
        assert result.steps[1].duration_seconds == 1.5

    def test_json_zero_values_are_kept(self):
        """A zero duration or id is a real value, not a missing one."""
        log = '{"build_id": 0, "steps": [{"name": "noop", "duration": 0, "duration_ms": 9000}]}'
        result = parse_json_log(log)
        assert result.build_id == "0"
        assert result.steps[0].duration_seconds == 0.0

    def test_json_non_numeric_attempt_does_not_crash(self):
        """String attempt counters are handled instead of raising TypeError."""
        log = '[{"name": "a", "attempt": "2"}, {"name": "b", "attempt": "first"}]'
        result = parse_json_log(log)
        assert result.steps[0].is_retry is True
        assert result.steps[1].is_retry is False
