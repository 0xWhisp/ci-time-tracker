"""Tests for CLI module.

Tests cover:
- Argument parsing
- Error handling (Property 8)
- File I/O operations
- Requirements: 4.1-4.7, 6.1-6.5, 7.4
"""

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings, strategies as st

from ci_time_tracker.cli import (
    load_estimates,
    main,
    parse_args,
    read_input,
    write_output,
)


class TestArgumentParsing:
    """Unit tests for CLI argument parsing."""

    def test_parse_args_config_mode(self) -> None:
        """Test parsing arguments for config analysis mode."""
        args = parse_args(["--config", "config.yml"])
        
        assert args.config == "config.yml"
        assert args.logs is None
        assert args.format == "text"  # Default
        assert args.output is None

    def test_parse_args_logs_mode(self) -> None:
        """Test parsing arguments for log analysis mode."""
        args = parse_args(["--logs", "build.log"])
        
        assert args.logs == "build.log"
        assert args.config is None
        assert args.format == "text"  # Default

    def test_parse_args_with_json_format(self) -> None:
        """Test parsing arguments with JSON output format."""
        args = parse_args(["--config", "config.yml", "--format", "json"])
        
        assert args.format == "json"

    def test_parse_args_with_output_file(self) -> None:
        """Test parsing arguments with output file specified."""
        args = parse_args(["--config", "config.yml", "--output", "report.txt"])
        
        assert args.output == "report.txt"

    def test_parse_args_with_estimates(self) -> None:
        """Test parsing arguments with estimates file."""
        args = parse_args(["--config", "config.yml", "--estimates", "estimates.json"])
        
        assert args.estimates == "estimates.json"

    def test_parse_args_stdin_config(self) -> None:
        """Test parsing arguments with stdin input for config."""
        args = parse_args(["--config", "-"])
        
        assert args.config == "-"

    def test_parse_args_stdin_logs(self) -> None:
        """Test parsing arguments with stdin input for logs."""
        args = parse_args(["--logs", "-"])
        
        assert args.logs == "-"

    def test_parse_args_missing_mode_fails(self) -> None:
        """Test that missing --config or --logs raises error."""
        with pytest.raises(SystemExit):
            parse_args([])

    def test_parse_args_both_modes_fails(self) -> None:
        """Test that specifying both --config and --logs raises error."""
        with pytest.raises(SystemExit):
            parse_args(["--config", "config.yml", "--logs", "build.log"])


class TestFileIO:
    """Unit tests for file I/O operations."""

    def test_read_input_from_file(self) -> None:
        """Test reading input from a regular file."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".yml") as f:
            f.write("test content\n")
            temp_path = f.name
        
        try:
            content = read_input(temp_path)
            assert content == "test content\n"
        finally:
            Path(temp_path).unlink()

    def test_read_input_nonexistent_file_raises_error(self) -> None:
        """Test that reading nonexistent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            read_input("/nonexistent/path/to/file.yml")

    def test_write_output_to_file(self) -> None:
        """Test writing output to a file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "output.txt"
            
            write_output("test content", str(output_path))
            
            assert output_path.exists()
            assert output_path.read_text() == "test content"

    def test_write_output_creates_parent_directories(self) -> None:
        """Test that write_output creates parent directories if needed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "subdir" / "output.txt"
            
            write_output("test content", str(output_path))
            
            assert output_path.exists()
            assert output_path.read_text() == "test content"

    def test_write_output_to_stdout(self, capsys: Any) -> None:
        """Test writing output to stdout."""
        write_output("test content", None)
        
        captured = capsys.readouterr()
        assert "test content" in captured.out


class TestEstimateLoading:
    """Unit tests for loading estimate files."""

    def test_load_estimates_from_valid_json(self) -> None:
        """Test loading estimates from valid JSON file."""
        estimates_data = {
            "build": 100.0,
            "test": 200.5,
            "deploy": 50.0,
        }
        
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            json.dump(estimates_data, f)
            temp_path = f.name
        
        try:
            estimates = load_estimates(temp_path)
            assert estimates == estimates_data
        finally:
            Path(temp_path).unlink()

    def test_load_estimates_empty_dict(self) -> None:
        """Test loading empty estimates dictionary."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            json.dump({}, f)
            temp_path = f.name
        
        try:
            estimates = load_estimates(temp_path)
            assert estimates == {}
        finally:
            Path(temp_path).unlink()

    def test_load_estimates_invalid_json_raises_error(self) -> None:
        """Test that invalid JSON raises JSONDecodeError."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            f.write("{ invalid json }")
            temp_path = f.name
        
        try:
            with pytest.raises(json.JSONDecodeError):
                load_estimates(temp_path)
        finally:
            Path(temp_path).unlink()

    def test_load_estimates_non_dict_raises_error(self) -> None:
        """Test that non-dictionary JSON raises ValueError."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            json.dump([1, 2, 3], f)  # Array instead of object
            temp_path = f.name
        
        try:
            with pytest.raises(ValueError, match="must contain a JSON object"):
                load_estimates(temp_path)
        finally:
            Path(temp_path).unlink()

    def test_load_estimates_negative_value_raises_error(self) -> None:
        """Test that negative estimate values raise ValueError."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            json.dump({"build": -10.0}, f)
            temp_path = f.name
        
        try:
            with pytest.raises(ValueError, match="must be non-negative"):
                load_estimates(temp_path)
        finally:
            Path(temp_path).unlink()

    def test_load_estimates_non_numeric_value_raises_error(self) -> None:
        """Test that non-numeric estimate values raise ValueError."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            json.dump({"build": "not a number"}, f)
            temp_path = f.name
        
        try:
            with pytest.raises(ValueError, match="must be numbers"):
                load_estimates(temp_path)
        finally:
            Path(temp_path).unlink()


class TestCLIIntegration:
    """Integration tests for the CLI main function."""

    def test_main_with_valid_github_config(self) -> None:
        """Test main function with valid GitHub Actions config."""
        config_content = """
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v2
      - name: Build
        run: make build
"""
        
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".yml") as f:
            f.write(config_content)
            config_path = f.name
        
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
            output_path = f.name
        
        try:
            # Mock sys.argv
            import sys
            old_argv = sys.argv
            sys.argv = ["ci-time-tracker", "--config", config_path, "--output", output_path]
            
            exit_code = main()
            
            assert exit_code == 0
            assert Path(output_path).exists()
            output_content = Path(output_path).read_text()
            assert "Checkout" in output_content or "Build" in output_content
            
            sys.argv = old_argv
        finally:
            Path(config_path).unlink()
            Path(output_path).unlink(missing_ok=True)

    def test_main_with_nonexistent_file_returns_error(self) -> None:
        """Test that main returns error code for nonexistent file."""
        import sys
        old_argv = sys.argv
        sys.argv = ["ci-time-tracker", "--config", "/nonexistent/file.yml"]
        
        exit_code = main()
        
        assert exit_code == 1
        sys.argv = old_argv

    def test_main_with_empty_file_handles_gracefully(self) -> None:
        """Test that main handles empty files gracefully."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".yml") as f:
            f.write("")  # Empty file
            config_path = f.name
        
        try:
            import sys
            old_argv = sys.argv
            sys.argv = ["ci-time-tracker", "--config", config_path]
            
            exit_code = main()
            
            # Should return error code for empty/invalid config
            assert exit_code != 0
            
            sys.argv = old_argv
        finally:
            Path(config_path).unlink()


# Hypothesis strategies for property testing

# Helper to check if string can be encoded in cp1252 (Windows default)
def can_encode_windows(s: str) -> bool:
    """Check if string can be encoded in Windows default encoding."""
    try:
        s.encode('cp1252')
        return True
    except (UnicodeEncodeError, UnicodeDecodeError):
        return False

# Generate malformed YAML content (printable ASCII only to avoid encoding issues)
malformed_yaml = st.one_of(
    st.just("{[invalid yaml"),
    st.just("key: value\n  bad_indent: value"),
    st.just("- item\n  - nested\n - bad"),
    st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),  # Printable ASCII
        min_size=1, 
        max_size=50
    ).filter(lambda s: s.strip()).map(lambda s: f"{{{{{{ {s} }}}}}}"),
)

# Generate malformed JSON content (printable ASCII only)
malformed_json = st.one_of(
    st.just("{invalid json}"),
    st.just('{"key": "value",}'),  # Trailing comma
    st.just('[1, 2, 3,]'),  # Trailing comma
    st.just('{"key": undefined}'),
    st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),  # Printable ASCII
        min_size=1, 
        max_size=50
    ).filter(lambda s: s.strip() and not s.startswith('"')),
)


class TestCLIErrorHandling:
    """Property tests for CLI error handling.
    
    **Feature: ci-time-tracker, Property 8: Invalid input error handling**
    """

    @given(content=malformed_yaml)
    @settings(max_examples=100)
    def test_malformed_yaml_produces_descriptive_error_without_crash(
        self, content: str
    ) -> None:
        """
        **Feature: ci-time-tracker, Property 8: Invalid input error handling**
        
        *For any* malformed YAML input, the CLI SHALL return a descriptive error
        message and non-zero exit code without crashing.
        
        **Validates: Requirements 6.1, 6.2**
        """
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".yml") as f:
            f.write(content)
            config_path = f.name
        
        try:
            import sys
            old_argv = sys.argv
            sys.argv = ["ci-time-tracker", "--config", config_path]
            
            # Should not crash, should return non-zero exit code
            exit_code = main()
            
            assert exit_code != 0, \
                f"Expected non-zero exit code for malformed YAML, got {exit_code}"
            
            sys.argv = old_argv
        finally:
            Path(config_path).unlink()

    @given(content=malformed_json)
    @settings(max_examples=100)
    def test_malformed_json_estimates_produces_descriptive_error(
        self, content: str
    ) -> None:
        """
        **Feature: ci-time-tracker, Property 8: Invalid input error handling**
        
        *For any* malformed JSON in estimates file, the CLI SHALL return a
        descriptive error message and non-zero exit code without crashing.
        
        **Validates: Requirements 6.1, 6.2**
        """
        # Create valid config
        config_content = """
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Build
        run: make build
"""
        
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".yml") as f:
            f.write(config_content)
            config_path = f.name
        
        # Create malformed estimates
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False, suffix=".json") as f:
            f.write(content)
            estimates_path = f.name
        
        try:
            import sys
            old_argv = sys.argv
            sys.argv = ["ci-time-tracker", "--config", config_path, "--estimates", estimates_path]
            
            # Should not crash, should return non-zero exit code
            exit_code = main()
            
            assert exit_code != 0, \
                f"Expected non-zero exit code for malformed JSON, got {exit_code}"
            
            sys.argv = old_argv
        finally:
            Path(config_path).unlink()
            Path(estimates_path).unlink()

    def test_error_handling_for_unsupported_ci_format(self) -> None:
        """Test that unsupported CI format produces descriptive error."""
        # Create a config that doesn't match any known CI provider
        config_content = """
# This is not a valid CI config format
random: configuration
with: unknown
structure: true
"""
        
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".yml") as f:
            f.write(config_content)
            config_path = f.name
        
        try:
            import sys
            old_argv = sys.argv
            sys.argv = ["ci-time-tracker", "--config", config_path]
            
            exit_code = main()
            
            # Should return error code
            assert exit_code != 0
            
            sys.argv = old_argv
        finally:
            Path(config_path).unlink()

