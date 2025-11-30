# ci-time-tracker

A minimal CLI tool for analyzing CI/CD pipeline configurations and build logs to identify performance bottlenecks and flaky steps.

## Features

- **Config Analysis**: Parse GitHub Actions, GitLab CI, and CircleCI configurations to extract pipeline structure
- **Log Analysis**: Parse build logs to compute actual step durations and statistics
- **Flaky Detection**: Identify steps with intermittent failures (5-95% failure rate)
- **Slow Step Detection**: Flag steps exceeding p90 duration by more than 50%
- **Multiple Output Formats**: Human-readable text or JSON for dashboard integration

## Installation

```bash
pip install ci-time-tracker
```

Or run directly from source:

```bash
git clone <repository-url>
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
```

### Log Analysis Mode

Analyze build logs to compute actual durations and detect issues:

```bash
# Analyze single log file
ci-time-tracker --logs build.log

# Analyze multiple logs (glob pattern)
ci-time-tracker --logs "logs/*.log"

# Output to file
ci-time-tracker --logs build.log --output report.json --format json
```

### Piping Input

```bash
cat .github/workflows/ci.yml | ci-time-tracker --config -
cat build.log | ci-time-tracker --logs -
```

## Output Example

### Text Output

```
CI Pipeline Analysis Report
===========================
Mode: log
Analyzed: 50 builds

Step Statistics:
  install-deps     p50: 45.2s  p90: 62.1s  [OK]
  run-tests        p50: 120.3s p90: 145.2s [SLOW]
  integration      p50: 89.1s  p90: 102.4s [FLAKY: 12%]

Issues:
  - run-tests: Exceeds p90 by 65%
  - integration: Failure rate 12% (6/50 runs)
```

### JSON Output

```json
{
  "title": "CI Pipeline Analysis Report",
  "mode": "log",
  "steps": [...],
  "issues": [...]
}
```

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
