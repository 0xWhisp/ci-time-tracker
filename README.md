# ci-time-tracker

A minimal CLI tool for analyzing CI/CD pipeline configurations and build logs to identify performance bottlenecks and flaky steps.

## Features

- **Config Analysis**: Parse GitHub Actions, GitLab CI, and CircleCI configurations to extract pipeline structure
- **Log Analysis**: Parse build logs to compute actual step durations and statistics
- **Flaky Detection**: Flag steps that both failed and passed on the same commit,
  including runs that only passed on a re-run
- **Regression Detection**: Flag steps whose duration jumped and stayed up, and
  name the commit where it started
- **Cost and Time**: Rank steps by the time they consume, estimate cost per runner,
  and separate queue time from execution time
- **Multiple Output Formats**: Human-readable text or JSON for dashboard integration

## Installation

Requires Python 3.10+. Not yet published on PyPI; install from source:

```bash
git clone https://github.com/0xWhisp/ci-time-tracker.git
cd ci-time-tracker
pip install -e .
```

## Usage

### Config Analysis Mode

Analyze pipeline configuration files to understand structure and estimated runtimes:

```bash
# Analyze GitHub Actions workflow
ci-time-tracker --config .github/workflows/ci.yml

# Analyze with runtime estimates
ci-time-tracker --config .gitlab-ci.yml --estimates estimates.json

# Output as JSON
ci-time-tracker --config .circleci/config.yml --format json

# Save output to file
ci-time-tracker --config .github/workflows/ci.yml --output report.txt
```

**Estimates File Format** (`estimates.json`):
```json
{
  "Checkout": 5.0,
  "Build": 120.0,
  "Test": 300.0,
  "Deploy": 60.0
}
```

### GitHub Actions API Mode

Analyze recent workflow runs straight from the GitHub Actions API, with no log
files to collect. Timings come from the API, so nothing depends on parsing
runner log text:

```bash
# Last 50 runs of every workflow in the repository
ci-time-tracker --github acme/web

# Last 200 runs of a single workflow on main
ci-time-tracker --github acme/web --workflow ci.yml --branch main --last 200

# JSON for a dashboard
ci-time-tracker --github acme/web --format json
```

Set `GITHUB_TOKEN` (or `GH_TOKEN`) to reach private repositories and to raise the
rate limit from 60 to 5000 requests per hour:

```bash
export GITHUB_TOKEN=$(gh auth token)
```

Each run costs one API request for its jobs, so completed runs are cached
locally and later invocations only fetch what is new. The cache lives in the
platform cache directory (override with `--cache-path` or the
`CI_TIME_TRACKER_CACHE` environment variable), and `--no-cache` skips it.

Steps are reported as `job / step`, the way the GitHub UI names them, so
identically named steps in different jobs stay separate.

### Log Analysis Mode

Analyze build logs to compute actual durations and detect issues:

```bash
# Analyze single log file
ci-time-tracker --logs build.log

# Analyze directory of logs
ci-time-tracker --logs logs/

# Output to file
ci-time-tracker --logs build.log --output report.json --format json
```

### Piping Input

Both config and log modes support stdin input using `-`:

```bash
# Pipe config from stdin
cat .github/workflows/ci.yml | ci-time-tracker --config -

# Pipe log from stdin
cat build.log | ci-time-tracker --logs -

# Chain with other commands
curl https://example.com/config.yml | ci-time-tracker --config - --format json
```

### Module Execution

You can also run ci-time-tracker as a Python module:

```bash
python -m ci_time_tracker --config .github/workflows/ci.yml
```

## Output Example

### Text Output (Log Mode)

```
==============================================================================================================
CI Time Tracker Report (log mode)
==============================================================================================================
Generated: 2026-09-27 03:43:13
Mode: log

SUMMARY
--------------------------------------------------------------------------------------------------------------
  build_count: 10
  step_count: 66
  total_execution_seconds: 1h 05m
  job_count: 35
  total_queue_seconds: 2m 42s
  queue_p50_seconds: 4.0s
  queue_p90_seconds: 10.0s
  estimated_cost_usd: $2.81
  priced_jobs: 35
  pricing_source: built-in defaults
  flaky_detection: same-commit
  regression_checked_steps: 12
  regression_min_builds: 10
  note: cost uses private-repository rates and GitHub's per-job rounding; public repos pay nothing for standard runners

TOP TIME CONSUMERS
--------------------------------------------------------------------------------------------------------------
  1. build / Run tests                              45m 18s   69.2%    23 runs     $1.53
  2. build / Install dependencies                    7m 08s   10.9%    23 runs     $0.30
  3. build / Set up Python                           1m 43s    2.6%    23 runs     $0.08

ISSUES
--------------------------------------------------------------------------------------------------------------
  [WARNING] REGRESSION: build / Install dependencies
    Median 10.0s -> 28.0s (+180%, +18.0s)
    Started at commit 3f9c2ab (build 9876543210, 2026-09-14)
  [ERROR] FLAKY: integration / Run e2e tests
    Failed and passed on the same commit in 2 of 17 commits
    Passed on re-run after failing in 2 run(s)
    e.g. 1a2b3c4, 9f8e7d6

STEPS
--------------------------------------------------------------------------------------------------------------
Step Name                               Runs     Total      P50      P90      P95      P99     Cost Flags
--------------------------------------------------------------------------------------------------------------
build / Run tests                         23   45m 18s  112.00s  178.00s  178.00s  179.00s    $1.53
build / Install dependencies              23    7m 08s   10.00s   28.00s   31.00s  115.00s    $0.30 REGR
build / Set up Python                     23    1m 43s    2.00s    9.00s   44.00s   49.00s    $0.08
... and 63 more steps (use --top 0 to show all)

==============================================================================================================
```

Steps are ordered by the time they consume, because that is what a CI bill is
made of. `--top N` limits the table (default 25, `0` shows everything). (The
issues in this example are illustrative; the rest is real output for
psf/requests.)

### JSON Output

```json
{
  "title": "CI Time Tracker Report (log mode)",
  "mode": "log",
  "generated_at": "2026-09-27T03:43:13",
  "summary": {
    "mode": "log",
    "build_count": 10,
    "step_count": 66,
    "total_execution_seconds": 3918.0,
    "job_count": 35,
    "total_queue_seconds": 162.0,
    "queue_p50_seconds": 4.0,
    "queue_p90_seconds": 10.0,
    "estimated_cost_usd": 2.808,
    "priced_jobs": 35,
    "pricing_source": "built-in defaults"
  },
  "steps": [
    {
      "name": "build / Run tests",
      "execution_count": 23,
      "success_count": 23,
      "failure_count": 0,
      "failure_rate": 0.0,
      "total_duration": 2718.0,
      "mean_duration": 118.17,
      "p50": 112.0,
      "p90": 178.0,
      "p95": 178.0,
      "p99": 179.0,
      "is_regression": false,
      "is_flaky": false,
      "runner": "ubuntu-22.04",
      "estimated_cost_usd": 1.5312,
      "regression": null,
      "flaky_evidence": null
    }
  ],
  "issues": [
    {
      "type": "regression",
      "step": "build / Install dependencies",
      "severity": "warning",
      "baseline_median": 10.0,
      "current_median": 28.0,
      "increase_seconds": 18.0,
      "increase_pct": 1.8,
      "baseline_builds": 14,
      "current_builds": 9,
      "started_at": {
        "build_id": "9876543210",
        "head_sha": "3f9c2ab",
        "timestamp": "2026-09-14T10:02:11+00:00"
      }
    }
  ]
}
```

## Using It in CI

### Run summary in Markdown

`--format markdown` renders the report for a workflow run's summary page or a
PR comment: headline numbers, the issues with their evidence, the top time
consumers, and the full step table collapsed.

```yaml
- name: CI time report
  run: |
    pip install git+https://github.com/0xWhisp/ci-time-tracker.git
    ci-time-tracker --github ${{ github.repository }} --workflow ci.yml       --last 100 --group-matrix --format markdown >> "$GITHUB_STEP_SUMMARY"
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

Output is always UTF-8, including on Windows runners.

### Quality gates

Reporting an issue does not fail the run. Gates do:

```bash
# Fail when a step is flaky or has regressed
ci-time-tracker --github acme/web --fail-on flaky,regression

# Fail when the median build takes longer than 10 minutes
ci-time-tracker --github acme/web --max-duration 600
```

The report is written first, so a failing pipeline still shows why; the reasons
are also printed to stderr. The median build duration counts each build once,
leaving out the earlier attempts of re-runs.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Report written, gates passed |
| 1 | File not found or unreadable, or nothing to analyze |
| 2 | Invalid configuration, log, estimates or pricing file |
| 3 | GitHub API error (rate limit, permissions, not found) |
| 4 | A quality gate failed |

## Cost Estimates

Costs are estimated from the runner each job ran on, using GitHub's published
per-minute rates for **private repositories**, rounded up per job the way
GitHub bills. Public repositories pay nothing for standard runners.

- Self-hosted runners cost `$0.00`.
- Larger runners are priced from their core count (Linux `$0.004`/core/min,
  Windows `$0.008`/core/min).
- A runner with no known rate is reported under `unpriced_runners` instead of
  being assumed free, so an incomplete estimate is never shown as complete.
- Per-step costs are attributed proportionally and do not include the per-job
  rounding, so they add up to slightly less than the billed total.

Rates change. Override them with a JSON file of label to USD per minute:

```bash
ci-time-tracker --github acme/web --pricing pricing.json
```

```json
{
  "ubuntu-latest": 0.008,
  "our-big-runner": 0.032
}
```

## Matrix Builds

GitHub names each leg of a matrix separately, so `Run tests` becomes
`build (3.12, ubuntu-latest) / Run tests` and its siblings: one step per leg.
`--group-matrix` merges them, which is usually what you want when asking where
the time goes:

```bash
# Without grouping: "build (3.12, windows-latest) / Run tests  2m 59s  4.6%"
# With grouping:    "build / Run tests                        45m 18s  69.2%"
ci-time-tracker --github acme/web --group-matrix
```

Grouping mixes runners with different rates, so the cost of a merged step spans
whatever machines its legs ran on.

## Detection Criteria

### Flaky Steps

A step is **flaky** when it both failed and passed **on the same commit**: the
code did not change, the outcome did. A failed run that passed when re-run is
the clearest case, so the earlier attempts of re-run workflows are fetched too.

- Outcomes are compared per workflow, step and commit, using each matrix leg's
  own name: a leg that always fails on Windows is broken there, not flaky.
- A high failure rate alone is **not** evidence. A step that fails because a
  commit broke it, and passes once the fix lands, is doing its job.

Logs without commit information (log files) fall back to the failure-rate
heuristic (`0.05 < failure_rate < 0.95`), which cannot tell flakiness from real
breakage. The report states which method was used (`flaky_detection`).

### Regressions

A step has **regressed** when its duration jumped and stayed up:

1. Its history (one sample per build, oldest first) is split at the point that
   best separates two stable segments.
2. The later segment's median must be at least **25%** higher
   (`--regression-threshold`) and at least **10 seconds** higher, so `1s -> 2s`
   steps are not reported.
3. The most recent 5 builds must still be that slow, so a spike that already
   recovered is not reported.

The report names the commit and build where the regression started. A step
needs at least **10 builds** to be judged; `regression_checked_steps` says how
many had enough history. With `--group-matrix`, the legs of a build are
combined into one sample (their median).

## Supported CI Providers

| Provider | Config Format | Log Format |
|----------|--------------|------------|
| GitHub Actions | YAML | Text/JSON |
| GitLab CI | YAML | Text/JSON |
| CircleCI | YAML | Text/JSON |

## Limitations

- Log parsing relies on timestamp patterns; non-standard formats may not parse correctly
- Flaky detection needs the same commit to have been built more than once
- Regression detection needs at least 10 builds of a step
- Estimated durations in config mode require user-provided estimates file

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=ci_time_tracker
```

## License

MIT License - see [LICENSE](LICENSE) for details.
