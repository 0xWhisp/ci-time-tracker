# Implementation Plan

- [x] 1. Set up project structure and dependencies





  - Create directory structure: `ci_time_tracker/`, `tests/`
  - Create `pyproject.toml` with dependencies (PyYAML, Hypothesis for testing)
  - Create `__init__.py` files
  - Create `.gitignore` for Python artifacts
  - Create `LICENSE` file with MIT license
  - Create `README.md` skeleton with project overview
  - _Requirements: 7.1, 7.2, 7.3_

- [ ] 2. Implement core data models
  - [ ] 2.1 Create data model classes in `models.py`
    - Implement `PipelineStep`, `PipelineConfig`, `StepExecution`, `BuildLog`, `StepStatistics`, `AnalysisResult`, `Report` dataclasses
    - Add type hints and validation methods
    - _Requirements: 1.1, 2.1, 5.3_
  - [ ] 2.2 Write property test for Report JSON round-trip
    - **Property 6: Report JSON round-trip**
    - Generate random Report objects, serialize to JSON, deserialize, verify equivalence
    - **Validates: Requirements 5.5**

- [ ] 3. Implement configuration parser
  - [ ] 3.1 Create `config_parser.py` with provider detection
    - Implement `detect_provider()` to auto-detect CI provider from config content
    - Implement `parse_config()` main entry point
    - _Requirements: 1.1, 1.6_
  - [ ] 3.2 Implement GitHub Actions parser
    - Implement `parse_github_actions()` to extract jobs, steps, and order
    - Handle workflow YAML structure with jobs and steps arrays
    - _Requirements: 1.2_
  - [ ] 3.3 Implement GitLab CI parser
    - Implement `parse_gitlab_ci()` to extract stages, jobs, and scripts
    - Handle stage ordering and job definitions
    - _Requirements: 1.3_
  - [ ] 3.4 Implement CircleCI parser
    - Implement `parse_circleci()` to extract jobs, steps, and executors
    - Handle orbs and workflow definitions
    - _Requirements: 1.4_
  - [ ] 3.5 Write property test for config parsing completeness
    - **Property 1: Config parsing extracts all steps**
    - Generate random valid config structures, verify all steps and stages extracted
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.4**
  - [ ] 3.6 Write unit tests for config parser
    - Test each provider parser with valid configs
    - Test malformed YAML/JSON handling
    - Test unsupported format detection
    - _Requirements: 1.6, 6.1, 6.2, 7.4_

- [ ] 4. Implement log parser
  - [ ] 4.1 Create `log_parser.py` with format detection
    - Implement `parse_log()` main entry point
    - Implement format auto-detection (text vs JSON)
    - _Requirements: 2.1_
  - [ ] 4.2 Implement text log parser
    - Implement `parse_text_log()` with timestamp pattern matching
    - Extract step names, start/end times, status
    - Handle common CI log formats (GitHub Actions, GitLab, CircleCI)
    - _Requirements: 2.4_
  - [ ] 4.3 Implement JSON log parser
    - Implement `parse_json_log()` for structured logs
    - Extract timing data from standard fields
    - _Requirements: 2.5_
  - [ ] 4.4 Write unit tests for log parser
    - Test text log parsing with various timestamp formats
    - Test JSON log parsing
    - Test handling of logs with missing timing data
    - _Requirements: 6.3, 7.4_

- [ ] 5. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Implement analyzer module
  - [ ] 6.1 Create `analyzer.py` with percentile computation
    - Implement `compute_percentiles()` for p50, p90, p95, p99
    - Implement `analyze_config()` for config analysis mode
    - Implement `analyze_logs()` for log analysis mode
    - _Requirements: 2.2, 5.1, 5.2_
  - [ ] 6.2 Write property test for percentile accuracy
    - **Property 3: Percentile computation accuracy**
    - Generate random duration lists, verify percentiles match statistical definition
    - **Validates: Requirements 2.2**
  - [ ] 6.3 Implement slow step detection
    - Implement `detect_slow_steps()` to flag steps exceeding p90 by >50%
    - _Requirements: 2.3_
  - [ ] 6.4 Write property test for slow step detection
    - **Property 4: Slow step detection threshold**
    - Generate step stats with various durations, verify correct flagging
    - **Validates: Requirements 2.3**
  - [ ] 6.5 Implement flaky step detection
    - Implement `detect_flaky_steps()` to flag steps with 5-95% failure rate
    - Track retry attempts in flakiness assessment
    - _Requirements: 3.1, 3.2, 3.3_
  - [ ] 6.6 Write property test for flaky step detection
    - **Property 5: Flaky step detection bounds**
    - Generate step stats with various failure rates, verify correct flagging at boundaries
    - **Validates: Requirements 3.1, 3.2**
  - [ ] 6.7 Implement estimate association
    - Implement logic to associate user-provided estimates with steps
    - _Requirements: 1.5_
  - [ ] 6.8 Write property test for estimate association
    - **Property 2: Estimate association correctness**
    - Generate random steps and estimates, verify correct association
    - **Validates: Requirements 1.5**

- [ ] 7. Implement reporter module
  - [ ] 7.1 Create `reporter.py` with report generation
    - Implement `generate_report()` to create Report from AnalysisResult
    - Implement `format_json()` for JSON output
    - Implement `format_text()` for human-readable output
    - Implement `parse_json_report()` for deserialization
    - _Requirements: 5.1, 5.2, 5.3, 5.5_
  - [ ] 7.2 Write property test for report completeness
    - **Property 7: Report completeness**
    - Generate analysis results, verify report contains all steps with required fields
    - **Validates: Requirements 5.1, 5.2, 5.3**
  - [ ] 7.3 Write unit tests for reporter
    - Test JSON output validity
    - Test text formatting
    - Test flaky step reporting includes failure rate and count
    - _Requirements: 3.4, 7.4_

- [ ] 8. Checkpoint
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 9. Implement CLI module
  - [ ] 9.1 Create `cli.py` with argument parsing
    - Implement `parse_args()` using argparse
    - Support `--config`, `--logs`, `--format`, `--output`, `--estimates` flags
    - Support stdin input detection
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_
  - [ ] 9.2 Implement main entry point
    - Implement `main()` function orchestrating parsing, analysis, reporting
    - Handle file I/O and error conditions
    - Return appropriate exit codes
    - _Requirements: 6.5_
  - [ ] 9.3 Write property test for error handling
    - **Property 8: Invalid input error handling**
    - Generate malformed YAML/JSON, verify descriptive errors without crashes
    - **Validates: Requirements 6.1, 6.2**
  - [ ] 9.4 Write unit tests for CLI
    - Test argument parsing for each flag combination
    - Test stdin handling
    - Test file not found error handling
    - Test empty file handling
    - _Requirements: 6.4, 6.5, 7.4_

- [ ] 10. Create package entry point
  - [ ] 10.1 Create `__main__.py` for module execution
    - Enable `python -m ci_time_tracker` execution
    - Wire up CLI main function
    - _Requirements: 4.1, 4.2_
  - [ ] 10.2 Update `pyproject.toml` with console script entry point
    - Add `ci-time-tracker` command
    - _Requirements: 7.1_

- [ ] 11. Finalize documentation
  - [ ] 11.1 Complete README.md
    - Add installation instructions (pip install)
    - Add usage examples for config and log analysis modes
    - Add output format examples
    - Document limitations
    - _Requirements: 7.1_

- [ ] 12. Final Checkpoint
  - Ensure all tests pass, ask the user if questions arise.
