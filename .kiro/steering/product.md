# Product Overview

ci-time-tracker is a minimal Python CLI tool for analyzing CI/CD pipeline configurations and build logs.

## Purpose
- Identify performance bottlenecks in CI pipelines
- Detect flaky steps with intermittent failures
- Compute runtime statistics (p50, p90, p95, p99 percentiles)

## Key Features
- Config analysis mode: Parse GitHub Actions, GitLab CI, CircleCI configurations
- Log analysis mode: Parse build logs to compute actual step durations
- Flaky detection: Flag steps with 5-95% failure rate
- Slow step detection: Flag steps exceeding p90 by >50%
- Multiple output formats: Human-readable text or JSON

## Target Users
- Developers analyzing pipeline performance
- DevOps engineers optimizing CI/CD workflows
- Team leads prioritizing reliability improvements
