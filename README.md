# ci-time-tracker

A minimal CLI tool for analyzing CI/CD pipeline configurations and build logs to identify performance bottlenecks and flaky steps.

## Features

- **Config Analysis**: Parse GitHub Actions, GitLab CI, and CircleCI configurations to extract pipeline structure
- **Log Analysis**: Parse build logs to compute actual step durations and statistics
- **Flaky Detection**: Identify steps with intermittent failures (5-95% failure rate)
- **Slow Step Detection**: Flag steps exceeding p90 duration by more than 50%
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
  note: cost uses private-repository rates and GitHub's per-job rounding; public repos pay nothing for standard runners

TOP TIME CONSUMERS
--------------------------------------------------------------------------------------------------------------
  1. build / Run tests                              45m 18s   69.2%    23 runs     $1.53
  2. build / Install dependencies                    7m 08s   10.9%    23 runs     $0.30
  3. build / Set up Python                           1m 43s    2.6%    23 runs     $0.08

ISSUES
--------------------------------------------------------------------------------------------------------------
  [WARNING] SLOW: build / Install dependencies
    P90: 28.00s, Max: 115.00s

STEPS
--------------------------------------------------------------------------------------------------------------
Step Name                               Runs     Total      P50      P90      P95      P99     Cost Flags
--------------------------------------------------------------------------------------------------------------
build / Run tests                         23   45m 18s  112.00s  178.00s  178.00s  179.00s    $1.53
build / Install dependencies              23    7m 08s   10.00s   28.00s   31.00s  115.00s    $0.30 SLOW
build / Set up Python                     23    1m 43s    2.00s    9.00s   44.00s   49.00s    $0.08
... and 63 more steps (use --top 0 to show all)

==============================================================================================================
```

Steps are ordered by the time they consume, because that is what a CI bill is
made of. `--top N` limits the table (default 25, `0` shows everything).

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
      "is_slow": false,
      "is_flaky": false,
      "runner": "ubuntu-22.04",
      "estimated_cost_usd": 1.5312
    }
  ],
  "issues": [
    {
      "type": "slow",
      "step": "build / Install dependencies",
      "severity": "warning",
      "p90": 28.0,
      "max_duration": 115.0
    }
  ]
}
```

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

### Slow Steps

A step is flagged as **slow** when:
- Any recorded duration exceeds the p90 (90th percentile) by more than 50%
- Formula: `duration > p90 * 1.5`

This identifies steps with occasional performance outliers that significantly impact build times.

### Flaky Steps

A step is flagged as **flaky** when:
- Failure rate is strictly between 5% and 95%
- Formula: `0.05 < failure_rate < 0.95`

This identifies steps with intermittent failures (not consistently passing or failing), which often indicate race conditions, timing issues, or environmental dependencies.

## Supported CI Providers

| Provider | Config Format | Log Format |
|----------|--------------|------------|
| GitHub Actions | YAML | Text/JSON |
| GitLab CI | YAML | Text/JSON |
| CircleCI | YAML | Text/JSON |

## Limitations

- Log parsing relies on timestamp patterns; non-standard formats may not parse correctly
- Flaky detection requires multiple build logs for meaningful analysis
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
