"""CLI module for ci-time-tracker.

This module provides command-line interface functionality including argument
parsing, file I/O, and orchestration of parsing, analysis, and reporting.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ci_time_tracker.analyzer import analyze_config, analyze_logs
from ci_time_tracker.cache import RunCache
from ci_time_tracker.config_parser import parse_config
from ci_time_tracker.detection import DEFAULT_REGRESSION_THRESHOLD
from ci_time_tracker.github_api import (
    DEFAULT_RUN_LIMIT,
    GitHubAPIError,
    fetch_build_logs,
)
from ci_time_tracker.log_parser import parse_log
from ci_time_tracker.pricing import load_pricing
from ci_time_tracker.reporter import format_json, format_text, generate_report


def _positive_percentage(value: str) -> float:
    """argparse type: a percentage above zero, returned as a fraction."""
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a number")
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number / 100.0


def parse_args(args: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.
    
    Args:
        args: Optional list of argument strings (for testing). If None, uses sys.argv.
        
    Returns:
        Parsed arguments namespace
    """
    parser = argparse.ArgumentParser(
        prog="ci-time-tracker",
        description="Analyze CI/CD pipeline configurations and build logs for performance bottlenecks and flaky steps.",
        epilog="Example: ci-time-tracker --config .github/workflows/ci.yml --format text",
    )
    
    # Mode selection (mutually exclusive)
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--config",
        type=str,
        metavar="PATH",
        help="Path to CI config file (GitHub Actions, GitLab CI, CircleCI). Use '-' for stdin.",
    )
    mode_group.add_argument(
        "--logs",
        type=str,
        metavar="PATH",
        help="Path to build log file or directory containing multiple logs. Use '-' for stdin.",
    )
    mode_group.add_argument(
        "--github",
        type=str,
        metavar="OWNER/REPO",
        help="Analyze recent workflow runs fetched from the GitHub Actions API. "
             "Set GITHUB_TOKEN to reach private repositories and raise the rate limit.",
    )

    # GitHub mode options
    parser.add_argument(
        "--workflow",
        type=str,
        metavar="FILE",
        help="Workflow file name to analyze (e.g. ci.yml). Only used with --github.",
    )

    parser.add_argument(
        "--branch",
        type=str,
        metavar="NAME",
        help="Only analyze runs of this branch. Only used with --github.",
    )

    parser.add_argument(
        "--last",
        type=int,
        default=DEFAULT_RUN_LIMIT,
        metavar="N",
        help=f"Number of recent runs to analyze (default: {DEFAULT_RUN_LIMIT}). Only used with --github.",
    )

    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore the local run cache and refetch every run. Only used with --github.",
    )

    parser.add_argument(
        "--cache-path",
        type=str,
        metavar="PATH",
        help="Location of the local run cache database. Only used with --github.",
    )
    
    parser.add_argument(
        "--pricing",
        type=str,
        metavar="PATH",
        help="Path to a JSON file mapping runner labels to USD per minute, "
             "overriding the built-in rates used for cost estimates.",
    )
    
    parser.add_argument(
        "--top",
        type=int,
        default=25,
        metavar="N",
        help="In text output, show only the N steps that consume the most time "
             "(default: 25). Use 0 to show every step.",
    )
    
    parser.add_argument(
        "--group-matrix",
        action="store_true",
        help="Merge the legs of a matrix job, so 'build (3.12, ubuntu) / Run tests' "
             "and its siblings count as one step.",
    )
    
    parser.add_argument(
        "--regression-threshold",
        type=_positive_percentage,
        default=DEFAULT_REGRESSION_THRESHOLD,
        metavar="PCT",
        help="Report a step as regressed when its median duration rose by at least "
             f"PCT percent (default: {DEFAULT_REGRESSION_THRESHOLD * 100:.0f}).",
    )
    
    # Optional arguments
    parser.add_argument(
        "--format",
        type=str,
        choices=["json", "text"],
        default="text",
        help="Output format (default: text)",
    )
    
    parser.add_argument(
        "--output",
        type=str,
        metavar="PATH",
        help="Output file path. If not specified, writes to stdout.",
    )
    
    parser.add_argument(
        "--estimates",
        type=str,
        metavar="PATH",
        help="Path to JSON file mapping step names to estimated durations (in seconds). Only used with --config mode.",
    )
    
    return parser.parse_args(args)


def read_input(path: str) -> str:
    """Read input from file or stdin.
    
    Args:
        path: File path or '-' for stdin
        
    Returns:
        File contents as string
        
    Raises:
        FileNotFoundError: If file doesn't exist
        IOError: If file cannot be read
    """
    if path == "-":
        return sys.stdin.read()
    
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    
    if not file_path.is_file():
        raise IOError(f"Path is not a file: {path}")
    
    try:
        return file_path.read_text(encoding="utf-8")
    except Exception as e:
        raise IOError(f"Error reading file {path}: {e}")


def write_output(content: str, path: str | None) -> None:
    """Write output to file or stdout.
    
    Args:
        content: Content to write
        path: Output file path or None for stdout
        
    Raises:
        IOError: If file cannot be written
    """
    if path is None:
        print(content)
        return
    
    output_path = Path(path)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
    except Exception as e:
        raise IOError(f"Error writing to file {path}: {e}")


def load_estimates(path: str) -> dict[str, float]:
    """Load estimates from JSON file.
    
    Args:
        path: Path to JSON file containing step name to duration mapping
        
    Returns:
        Dictionary mapping step names to estimated durations
        
    Raises:
        FileNotFoundError: If file doesn't exist
        json.JSONDecodeError: If file contains invalid JSON
        ValueError: If JSON structure is invalid
    """
    content = read_input(path)
    
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Invalid JSON in estimates file: {e.msg}",
            e.doc,
            e.pos
        )
    
    if not isinstance(data, dict):
        raise ValueError("Estimates file must contain a JSON object (dictionary)")
    
    # Validate structure
    estimates: dict[str, float] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            raise ValueError(f"Estimate keys must be strings, got: {type(key)}")
        
        if not isinstance(value, (int, float)):
            raise ValueError(f"Estimate values must be numbers, got: {type(value)} for key '{key}'")
        
        if value < 0:
            raise ValueError(f"Estimate values must be non-negative, got: {value} for key '{key}'")
        
        estimates[key] = float(value)
    
    return estimates


def main() -> int:
    """Main entry point for the CLI.
    
    Returns:
        Exit code (0 for success, non-zero for error)
    """
    try:
        args = parse_args()
        
        # Runner pricing for cost estimates (log and GitHub modes)
        pricing = None
        if args.pricing:
            try:
                pricing = load_pricing(args.pricing)
            except OSError as e:
                print(f"Error reading pricing file: {e}", file=sys.stderr)
                return 1
            except ValueError as e:
                print(f"Error parsing pricing file: {e}", file=sys.stderr)
                return 2
        
        # Determine mode
        if args.config:
            # Config analysis mode
            try:
                config_content = read_input(args.config)
            except FileNotFoundError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            except IOError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 1
            
            # Parse config
            try:
                config = parse_config(config_content)
            except ValueError as e:
                print(f"Error parsing config: {e}", file=sys.stderr)
                return 2
            except Exception as e:
                print(f"Unexpected error parsing config: {e}", file=sys.stderr)
                return 2
            
            # Load estimates if provided
            estimates = None
            if args.estimates:
                try:
                    estimates = load_estimates(args.estimates)
                except (FileNotFoundError, IOError) as e:
                    print(f"Error loading estimates: {e}", file=sys.stderr)
                    return 1
                except (json.JSONDecodeError, ValueError) as e:
                    print(f"Error parsing estimates: {e}", file=sys.stderr)
                    return 2
            
            # Analyze
            result = analyze_config(config, estimates)
            
        elif args.logs:
            # Log analysis mode
            log_path = Path(args.logs) if args.logs != "-" else None
            
            # Determine if we have a directory or single file
            logs_to_parse = []
            
            if log_path and log_path.is_dir():
                # Parse all files in directory
                for file_path in sorted(log_path.rglob("*")):
                    if file_path.is_file():
                        try:
                            log_content = file_path.read_text(encoding="utf-8")
                            logs_to_parse.append(log_content)
                        except Exception as e:
                            print(f"Warning: Could not read {file_path}: {e}", file=sys.stderr)
            else:
                # Single file or stdin
                try:
                    log_content = read_input(args.logs)
                    logs_to_parse.append(log_content)
                except (FileNotFoundError, IOError) as e:
                    print(f"Error: {e}", file=sys.stderr)
                    return 1
            
            if not logs_to_parse:
                print("Error: No log files found to analyze", file=sys.stderr)
                return 1
            
            # Parse logs
            parsed_logs = []
            for i, log_content in enumerate(logs_to_parse):
                try:
                    log = parse_log(log_content)
                    parsed_logs.append(log)
                except ValueError as e:
                    print(f"Warning: Error parsing log {i + 1}: {e}", file=sys.stderr)
                except Exception as e:
                    print(f"Warning: Unexpected error parsing log {i + 1}: {e}", file=sys.stderr)
            
            if not parsed_logs:
                print("Error: No logs could be successfully parsed", file=sys.stderr)
                return 2
            
            # Analyze
            result = analyze_logs(
                parsed_logs,
                pricing,
                args.group_matrix,
                regression_threshold=args.regression_threshold,
            )

        elif args.github:
            # GitHub Actions API mode
            cache = None
            if not args.no_cache:
                try:
                    cache = RunCache(args.cache_path)
                except Exception as e:
                    print(f"Warning: run cache unavailable ({e})", file=sys.stderr)

            try:
                logs = fetch_build_logs(
                    repo=args.github,
                    workflow=args.workflow,
                    branch=args.branch,
                    limit=args.last,
                    cache=cache,
                )
            except GitHubAPIError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 3
            finally:
                if cache is not None:
                    cache.close()

            if not logs:
                print("Error: No workflow runs found to analyze", file=sys.stderr)
                return 1

            # Analyze
            result = analyze_logs(
                logs,
                pricing,
                args.group_matrix,
                regression_threshold=args.regression_threshold,
            )

        else:
            # Should never reach here due to argparse
            print("Error: Must specify --config, --logs or --github", file=sys.stderr)
            return 1
        
        # Generate report
        report = generate_report(result)
        
        # Format output
        if args.format == "json":
            output = format_json(report)
        else:
            output = format_text(report, args.top)
        
        # Write output
        try:
            write_output(output, args.output)
        except IOError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        
        return 0
        
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

