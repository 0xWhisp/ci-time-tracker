# Project Structure

```
ci-time-tracker/
├── ci_time_tracker/           # Main package
│   ├── __init__.py
│   ├── __main__.py            # Module entry point (python -m)
│   ├── cli.py                 # CLI argument parsing and main()
│   ├── config_parser.py       # CI config parsing (GitHub/GitLab/CircleCI)
│   ├── log_parser.py          # Build log parsing (text/JSON)
│   ├── analyzer.py            # Statistics, flaky/slow detection
│   ├── reporter.py            # Text and JSON report generation
│   └── models.py              # Dataclasses (PipelineStep, StepStatistics, etc.)
├── tests/
│   ├── __init__.py
│   ├── conftest.py            # Shared fixtures and Hypothesis generators
│   ├── test_cli.py
│   ├── test_config_parser.py
│   ├── test_log_parser.py
│   ├── test_analyzer.py
│   └── test_reporter.py
├── pyproject.toml             # Package config and dependencies
├── README.md
├── LICENSE
└── .gitignore
```

## Module Responsibilities

| Module | Purpose |
|--------|---------|
| `cli.py` | Argument parsing, stdin handling, orchestration |
| `config_parser.py` | Parse YAML configs, detect CI provider |
| `log_parser.py` | Parse text/JSON logs, extract timing |
| `analyzer.py` | Compute percentiles, detect slow/flaky steps |
| `reporter.py` | Generate text/JSON reports |
| `models.py` | Data structures shared across modules |

## Data Flow
1. CLI parses args → routes to config or log parser
2. Parser extracts structured data
3. Analyzer computes statistics and detects issues
4. Reporter formats output as text or JSON
