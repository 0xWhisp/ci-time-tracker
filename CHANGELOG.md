# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-24

First published release. Version 0.1.0 was never published; this release
replaces it.

### Added

- **GitHub Actions API ingestion**: `--github owner/repo`, with `--workflow`,
  `--branch` and `--last`. Completed runs are cached locally (`--no-cache`,
  `--cache-path`), jobs are fetched concurrently, and the earlier attempts of
  re-run workflows are fetched too.
- **Consumed time, cost and queue time**: steps ranked by the time they consume,
  cost per runner with GitHub's per-job rounding (`--pricing` overrides the
  rates), queue time p50/p90, and median and p90 build duration.
- `--group-matrix` to merge the legs of matrix jobs, and `--top N` to limit the
  step table.
- **Flaky detection by commit**: a step is flaky when it both failed and passed
  on the same commit, including runs that only passed on a re-run.
- **Regression detection**: finds where a step's duration jumped and stayed up,
  and names the commit and build where it started (`--regression-threshold`).
- `--format markdown`, for `$GITHUB_STEP_SUMMARY` and pull request comments.
- **Quality gates**: `--fail-on flaky,regression` and `--max-duration`, exiting
  with code 4.
- **Pull request mode**: `--run-id` compares one run with the median of recent
  runs on the default branch; `--comment` keeps a single pull request comment
  up to date.
- **GitHub Action** (`action.yml`) that comments on pull requests with how they
  change CI time and cost, and whether their failures are in known flaky steps.
- Support for Python 3.10 to 3.14, tested on Linux and Windows.

### Changed

- **Breaking**: the SLOW rule (any duration above 1.5 × p90), which mostly
  reported noise, is replaced by regression detection. In JSON output,
  `is_slow`, `slowest_steps` and `"slow"` issues became `is_regression`,
  `regressed_steps` and `"regression"` issues.
- Flaky detection no longer relies on the failure rate when commit data is
  available. Log files without it fall back to the failure rate, and the
  report says so.
- Steps are ordered by consumed time in every output format.
- Output is always UTF-8.
- Requires Python 3.10 or later. It was declared as 3.9, which never worked.

### Fixed

- GitHub Actions logs: step durations came out near zero, because
  `##[endgroup]` was taken as the end of the step; nested groups were counted
  as steps.
- JSON logs: `duration_ms` was reported as seconds, `0` values were treated as
  missing, and a non-numeric `attempt` crashed the parser.
- GitLab logs: failures were searched across the whole log with an unescaped
  regular expression, and section names containing `-` or `.` were not
  matched.
- A crash on Windows when output was redirected to a file or pipe.

[0.2.0]: https://github.com/0xWhisp/ci-time-tracker/releases/tag/v0.2.0
