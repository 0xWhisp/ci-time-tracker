"""CLI module for ci-time-tracker.

This module provides command-line interface functionality including argument
parsing, file I/O, and orchestration of parsing, analysis, and reporting.
"""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from ci_time_tracker.analyzer import analyze_config, analyze_logs
from ci_time_tracker.cache import RunCache
from ci_time_tracker.config_parser import parse_config
from ci_time_tracker.detection import DEFAULT_REGRESSION_THRESHOLD
from ci_time_tracker.gates import GATE_CHOICES, evaluate_gates, evaluate_impact_gates
from ci_time_tracker.github_api import (
    DEFAULT_RUN_LIMIT,
    DEFAULT_WORKERS,
    GitHubAPIError,
    GitHubClient,
    fetch_build_logs,
    fetch_impact_inputs,
    resolve_token,
)
from ci_time_tracker.html_report import format_html
from ci_time_tracker.impact import compare_run, format_impact_markdown
from ci_time_tracker.log_parser import parse_log
from ci_time_tracker.pricing import PricingTable, load_pricing
from ci_time_tracker.recommendations import parse_workflow, recommend
from ci_time_tracker.reporter import (
    format_json,
    format_markdown,
    format_text,
    generate_report,
)


def _positive_percentage(value: str) -> float:
    """argparse type: a percentage above zero, returned as a fraction."""
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a number") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number / 100.0


def _gate_list(value: str) -> set[str]:
    """argparse type: a comma-separated list of findings to fail on."""
    gates = {item.strip().lower() for item in value.split(",") if item.strip()}
    unknown = gates - set(GATE_CHOICES)
    if not gates or unknown:
        raise argparse.ArgumentTypeError(
            f"expected a comma-separated list of: {', '.join(GATE_CHOICES)}"
        )
    return gates


def _positive_seconds(value: str) -> float:
    """argparse type: a duration in seconds above zero."""
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{value}' is not a number") from None
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


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

    # Pull request mode: one run against its baseline
    parser.add_argument(
        "--run-id",
        type=int,
        metavar="ID",
        help="Compare one workflow run with recent runs of the same workflow on the "
             "baseline branch, instead of analyzing history. Requires --github.",
    )

    parser.add_argument(
        "--baseline-branch",
        type=str,
        metavar="NAME",
        help="Branch to compare --run-id with (default: the repository's default branch).",
    )

    parser.add_argument(
        "--baseline-runs",
        type=int,
        default=20,
        metavar="N",
        help="Number of baseline runs to compare --run-id with (default: 20).",
    )

    parser.add_argument(
        "--comment",
        action="store_true",
        help="Post the --run-id comparison as a comment on the run's pull request, "
             "updating the previous one. Needs a token with pull-requests: write.",
    )

    parser.add_argument(
        "--pr",
        type=int,
        metavar="NUMBER",
        help="Pull request to comment on (default: found from the run's commit).",
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
        choices=["json", "text", "markdown", "html"],
        default="text",
        help="Output format (default: text). Markdown suits $GITHUB_STEP_SUMMARY "
             "and PR comments; html is a self-contained page with charts.",
    )
    
    parser.add_argument(
        "--fail-on",
        type=_gate_list,
        default=set(),
        metavar="FINDINGS",
        help="Exit with code 4 when any of these findings is reported: "
             f"a comma-separated list of {', '.join(GATE_CHOICES)}.",
    )
    
    parser.add_argument(
        "--max-duration",
        type=_positive_seconds,
        metavar="SECONDS",
        help="Exit with code 4 when the median build duration exceeds SECONDS.",
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
    
    parsed = parser.parse_args(args)

    if parsed.run_id is not None and not parsed.github:
        parser.error("--run-id requires --github")
    if (parsed.comment or parsed.pr is not None) and parsed.run_id is None:
        parser.error("--comment and --pr require --run-id")

    return parsed


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
        raise OSError(f"Path is not a file: {path}")
    
    try:
        return file_path.read_text(encoding="utf-8")
    except Exception as e:
        raise OSError(f"Error reading file {path}: {e}") from e


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
        raise OSError(f"Error writing to file {path}: {e}") from e


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
        ) from e
    
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


def _default_branch_if_mixed(repo: str, logs: list[Any]) -> str | None:
    """The repository's default branch, when the runs span several branches.

    Regressions are looked for on that branch only; with a single branch
    there is nothing to choose and no request is made.
    """
    branches = {log.metadata.get("branch") for log in logs if log.metadata.get("branch")}
    if len(branches) < 2:
        return None
    try:
        return GitHubClient(repo=repo, token=resolve_token()).fetch_default_branch()
    except (GitHubAPIError, KeyError, TypeError):
        return None


def _visibility_pricing(logs: list[Any], pricing: Any) -> tuple[PricingTable, str | None]:
    """Pricing for the repository's visibility, read from its runs.

    Public repositories pay nothing for standard GitHub-hosted runners. When
    the runs do not say, list prices are kept and no visibility is claimed.
    """
    pricing = pricing or PricingTable()
    private = next(
        (log.metadata["repository_private"] for log in logs if log.metadata.get("repository_private") is not None),
        None,
    )

    if private is False:
        return replace(pricing, public_repository=True), "public"
    if private is True:
        return pricing, "private"
    return pricing, None


# Most distinct workflow files fetched to cross with the history
_MAX_WORKFLOW_FILES = 20


def _workflow_refs(repo: str, logs: list[Any]) -> dict[str, str | None]:
    """Map each workflow file behind the runs to the commit to read it at.

    That is the newest run's commit from the repository itself (a fork's
    commit may not be readable through it). None reads the default branch.
    """
    def rank(log: Any) -> tuple[bool, float]:
        own = log.metadata.get("head_repository") in (None, repo)
        return bool(own and log.head_sha), log.timestamp.timestamp() if log.timestamp else float("-inf")

    newest: dict[str, Any] = {}
    for log in logs:
        path = str(log.metadata.get("workflow_path") or "")
        if path.startswith(".github/workflows/") and (path not in newest or rank(log) > rank(newest[path])):
            newest[path] = log

    return {
        path: newest[path].head_sha if rank(newest[path])[0] else None
        for path in sorted(newest)[:_MAX_WORKFLOW_FILES]
    }


def _fetch_workflows(repo: str, logs: list[Any], cache: Any = None) -> dict[str, dict[str, Any]]:
    """Fetch and parse the workflow files behind the analyzed runs.

    Recommendations that need a workflow's configuration skip the ones that
    cannot be read, so a failure here only narrows what is recommended.
    Files read at a commit are cached, since that pins their content.
    """
    refs = _workflow_refs(repo, logs)
    texts: dict[str, str | None] = {}
    if cache is not None:
        for path, ref in refs.items():
            if ref:
                cached = cache.get_workflow_file(repo, path, ref)
                if cached is not None:
                    texts[path] = cached

    missing = [path for path in refs if path not in texts]
    if missing:
        client = GitHubClient(repo=repo, token=resolve_token())

        def fetch(path: str) -> tuple[str, str | None]:
            try:
                return path, client.fetch_workflow_file(path, ref=refs[path])
            except GitHubAPIError as e:
                print(f"Warning: could not read {path} ({e}); skipping its recommendations", file=sys.stderr)
                return path, None

        # Fetched concurrently: one after the other they added over a second
        # to an analysis whose runs were all cached
        with ThreadPoolExecutor(max_workers=min(DEFAULT_WORKERS, len(missing))) as pool:
            for path, text in pool.map(fetch, missing):
                texts[path] = text
                if text is not None and cache is not None and refs[path]:
                    cache.store_workflow_file(repo, path, refs[path], text)

    workflows: dict[str, dict[str, Any]] = {}
    for path in refs:
        text = texts.get(path)
        parsed = parse_workflow(text) if text else None
        if parsed is not None:
            workflows[path] = parsed

    return workflows


def _no_runs_message(args: argparse.Namespace) -> str:
    """Explain an empty result, pointing at the default branch when it differs.

    Repositories differ on main versus master, and an empty answer for the
    wrong branch is otherwise indistinguishable from a repository without CI.
    """
    message = "No workflow runs found to analyze"
    if not args.branch:
        return message

    message = f"No completed workflow runs found on branch '{args.branch}'"
    try:
        default = GitHubClient(repo=args.github, token=resolve_token()).fetch_default_branch()
    except GitHubAPIError:
        return message

    if default and default != args.branch:
        message += f". The repository's default branch is '{default}'"
    return message


def _run_impact(args: argparse.Namespace, pricing: Any) -> int:
    """Compare one run with its baseline, and optionally comment on its PR.

    Returns:
        Exit code
    """
    cache = None
    if not args.no_cache:
        try:
            cache = RunCache(args.cache_path)
        except Exception as e:
            print(f"Warning: run cache unavailable ({e})", file=sys.stderr)

    try:
        run, run_logs, baseline, branch = fetch_impact_inputs(
            repo=args.github,
            run_id=args.run_id,
            baseline_branch=args.baseline_branch,
            baseline_runs=args.baseline_runs,
            cache=cache,
        )
    except GitHubAPIError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 3
    finally:
        if cache is not None:
            cache.close()

    pricing, _ = _visibility_pricing(run_logs, pricing)
    impact = compare_run(
        run_logs,
        baseline,
        branch,
        pricing=pricing,
        group_matrix=args.group_matrix,
        threshold=args.regression_threshold,
    )
    comment = format_impact_markdown(impact)

    # The comparison is Markdown in both text and markdown formats
    output = json.dumps(impact.to_dict(), indent=2, default=str) if args.format == "json" else comment

    try:
        write_output(output, args.output)
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if args.comment:
        client = GitHubClient(repo=args.github, token=resolve_token())
        try:
            number = args.pr or client.find_pull_request(run.get("head_sha") or "")
            if number is None:
                print("Warning: no open pull request found for this run; not commenting", file=sys.stderr)
            else:
                action, url = client.upsert_comment(number, comment)
                print(f"Comment {action} on pull request #{number}: {url}", file=sys.stderr)
        except GitHubAPIError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 3

    reasons = evaluate_impact_gates(impact, args.fail_on, args.max_duration)
    if reasons:
        print("Gate failed:", file=sys.stderr)
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 4

    return 0


def _use_utf8_output() -> None:
    """Write UTF-8 to stdout and stderr whatever the platform default is.
    
    On Windows, output sent to a pipe or a file (such as
    $GITHUB_STEP_SUMMARY) uses the ANSI code page, which cannot encode emoji
    or many step names and would crash the run halfway through the report.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            # Not a reconfigurable text stream (e.g. replaced in tests)
            pass


def main() -> int:
    """Main entry point for the CLI.
    
    Returns:
        Exit code (0 for success, non-zero for error)
    """
    _use_utf8_output()
    
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
            except OSError as e:
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
                print(
                    "Warning: --estimates is deprecated and will be removed: use --github "
                    "for durations measured from real runs",
                    file=sys.stderr,
                )
                try:
                    estimates = load_estimates(args.estimates)
                except (OSError, FileNotFoundError) as e:
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
                except (OSError, FileNotFoundError) as e:
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
            if args.run_id is not None:
                return _run_impact(args, pricing)

            # GitHub Actions API mode
            cache = None
            if not args.no_cache:
                try:
                    cache = RunCache(args.cache_path)
                except Exception as e:
                    print(f"Warning: run cache unavailable ({e})", file=sys.stderr)

            skipped: list[str] = []
            try:
                logs = fetch_build_logs(
                    repo=args.github,
                    workflow=args.workflow,
                    branch=args.branch,
                    limit=args.last,
                    cache=cache,
                    skipped=skipped,
                )
                workflows = _fetch_workflows(args.github, logs, cache) if logs else {}
            except GitHubAPIError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 3
            finally:
                if cache is not None:
                    cache.close()

            if not logs:
                print(f"Error: {_no_runs_message(args)}", file=sys.stderr)
                return 1

            # Analyze
            pricing, visibility = _visibility_pricing(logs, pricing)
            result = analyze_logs(
                logs,
                pricing,
                args.group_matrix,
                regression_threshold=args.regression_threshold,
                regression_branch=_default_branch_if_mixed(args.github, logs),
            )
            result.metadata["repository"] = args.github
            if visibility:
                result.metadata["repository_visibility"] = visibility
            if skipped:
                result.metadata["skipped_runs"] = len(skipped)
                print(
                    f"Warning: left out {len(skipped)} run(s) GitHub failed to return: "
                    f"{', '.join(skipped[:5])}",
                    file=sys.stderr,
                )
            result.recommendations = [
                item.to_dict() for item in recommend(logs, workflows, pricing)
            ]

        else:
            # Should never reach here due to argparse
            print("Error: Must specify --config, --logs or --github", file=sys.stderr)
            return 1
        
        # Generate report
        report = generate_report(result)
        
        # Format output
        if args.format == "json":
            output = format_json(report)
        elif args.format == "markdown":
            output = format_markdown(report, args.top)
        elif args.format == "html":
            output = format_html(report, args.top)
        else:
            output = format_text(report, args.top)
        
        # Write output
        try:
            write_output(output, args.output)
        except OSError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        
        # Quality gates run after the report is written, so a failing
        # pipeline still shows why it failed
        if args.max_duration is not None and "build_p50_seconds" not in result.metadata:
            print("Warning: --max-duration not checked: no build durations known", file=sys.stderr)
        
        reasons = evaluate_gates(result, args.fail_on, args.max_duration)
        if reasons:
            print("Gate failed:", file=sys.stderr)
            for reason in reasons:
                print(f"  - {reason}", file=sys.stderr)
            return 4
        
        return 0
        
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

