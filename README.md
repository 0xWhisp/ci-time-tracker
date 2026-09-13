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
================================================================================
CI Time Tracker Report (log mode)
================================================================================
Generated: 2024-01-15 10:30:45
Mode: log

SUMMARY
--------------------------------------------------------------------------------
  build_count: 50
  step_count: 3

ISSUES
--------------------------------------------------------------------------------
  [WARNING] SLOW: run-tests
    P90: 145.20s, Max: 240.00s
  [ERROR] FLAKY: integration
    Failure rate: 12.0% (6/50 executions)

STEPS
--------------------------------------------------------------------------------
Step Name                      Executions          P50      P90      P95      P99 Flags     
--------------------------------------------------------------------------------
install-deps                           50    45.20s   62.10s   65.00s   70.00s           
run-tests                              50   120.30s  145.20s  180.00s  240.00s SLOW      
integration                            50    89.10s  102.40s  110.00s  115.00s FLAKY     

================================================================================
```

### JSON Output

```json
{
  "title": "CI Time Tracker Report (log mode)",
  "mode": "log",
  "generated_at": "2024-01-15T10:30:45",
  "summary": {
    "mode": "log",
    "build_count": 50,
    "step_count": 3
  },
  "steps": [
    {
      "name": "install-deps",
      "execution_count": 50,
      "success_count": 50,
      "failure_count": 0,
      "failure_rate": 0.0,
      "p50": 45.2,
      "p90": 62.1,
      "p95": 65.0,
      "p99": 70.0,
      "is_slow": false,
      "is_flaky": false
    },
    {
      "name": "run-tests",
      "execution_count": 50,
      "success_count": 50,
      "failure_count": 0,
      "failure_rate": 0.0,
      "p50": 120.3,
      "p90": 145.2,
      "p95": 180.0,
      "p99": 240.0,
      "is_slow": true,
      "is_flaky": false
    },
    {
      "name": "integration",
      "execution_count": 50,
      "success_count": 44,
      "failure_count": 6,
      "failure_rate": 0.12,
      "p50": 89.1,
      "p90": 102.4,
      "p95": 110.0,
      "p99": 115.0,
      "is_slow": false,
      "is_flaky": true
    }
  ],
  "issues": [
    {
      "type": "slow",
      "step": "run-tests",
      "severity": "warning",
      "p90": 145.2,
      "max_duration": 240.0
    },
    {
      "type": "flaky",
      "step": "integration",
      "severity": "error",
      "failure_rate": 0.12,
      "failure_count": 6,
      "execution_count": 50
    }
  ]
}
```

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
