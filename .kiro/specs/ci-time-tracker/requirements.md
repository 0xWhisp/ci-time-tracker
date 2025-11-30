# Requirements Document

## Introduction

ci-time-tracker is a minimal, production-quality CLI tool for analyzing CI/CD pipeline configurations and build logs. The tool helps developers and engineering teams identify performance bottlenecks, detect flaky steps, and understand pipeline runtime characteristics. Built with Python using minimal dependencies, it supports multiple CI providers and output formats.

## Glossary

- **CI_Time_Tracker**: The CLI tool that analyzes CI/CD pipeline configurations and build logs
- **Pipeline_Configuration**: YAML or JSON files that define CI/CD pipeline steps and stages (e.g., GitHub Actions, GitLab CI, CircleCI)
- **Build_Log**: Plain text or JSON output from CI/CD pipeline executions containing timing and status information
- **Step**: A discrete unit of work within a pipeline (e.g., "install dependencies", "run tests")
- **Stage**: A grouping of steps that execute together or in sequence
- **Flaky_Step**: A step that intermittently fails without code changes, indicating instability
- **Duration_Percentile**: Statistical measure (p50, p90, p95, p99) of step execution times across multiple runs
- **Config_Analysis_Mode**: Operating mode that parses pipeline configuration files to extract step definitions
- **Log_Analysis_Mode**: Operating mode that parses build logs to compute actual durations and detect issues
- **Summary_Report**: Human-readable or JSON output containing analysis results

## Requirements

### Requirement 1

**User Story:** As a developer, I want to analyze my CI pipeline configuration files, so that I can understand the structure and estimated runtime of my pipeline steps.

#### Acceptance Criteria

1. WHEN a user provides a pipeline configuration file path THEN the CI_Time_Tracker SHALL parse the file and extract all defined steps and stages
2. WHEN the CI_Time_Tracker parses a GitHub Actions workflow YAML file THEN the CI_Time_Tracker SHALL extract job names, step names, and step order
3. WHEN the CI_Time_Tracker parses a GitLab CI YAML file THEN the CI_Time_Tracker SHALL extract stage names, job names, and script commands
4. WHEN the CI_Time_Tracker parses a CircleCI configuration file THEN the CI_Time_Tracker SHALL extract job names, step names, and executor information
5. WHEN a user provides runtime estimates via command-line or estimate file THEN the CI_Time_Tracker SHALL associate estimates with corresponding steps
6. WHEN the CI_Time_Tracker encounters an unsupported configuration format THEN the CI_Time_Tracker SHALL report a clear error message identifying the format issue

### Requirement 2

**User Story:** As a DevOps engineer, I want to analyze past CI build logs, so that I can identify slow steps and compute actual runtime statistics.

#### Acceptance Criteria

1. WHEN a user provides build log files THEN the CI_Time_Tracker SHALL parse the logs and extract step timing information
2. WHEN the CI_Time_Tracker processes multiple build logs THEN the CI_Time_Tracker SHALL compute duration percentiles (p50, p90, p95, p99) for each step
3. WHEN a step duration exceeds the p90 threshold by more than 50% THEN the CI_Time_Tracker SHALL flag the step as slow in the report
4. WHEN the CI_Time_Tracker parses plain text logs THEN the CI_Time_Tracker SHALL extract timestamps and step markers using configurable patterns
5. WHEN the CI_Time_Tracker parses JSON-formatted logs THEN the CI_Time_Tracker SHALL extract timing data from structured fields

### Requirement 3

**User Story:** As a team lead, I want to detect flaky steps in our CI pipeline, so that I can prioritize fixing unreliable tests or infrastructure.

#### Acceptance Criteria

1. WHEN the CI_Time_Tracker analyzes multiple build logs THEN the CI_Time_Tracker SHALL track success and failure counts per step
2. WHEN a step has a failure rate between 5% and 95% across analyzed logs THEN the CI_Time_Tracker SHALL flag the step as flaky
3. WHEN the CI_Time_Tracker detects retry attempts for a step THEN the CI_Time_Tracker SHALL include retry count in the flakiness assessment
4. WHEN reporting flaky steps THEN the CI_Time_Tracker SHALL display failure rate percentage and total execution count

### Requirement 4

**User Story:** As a developer, I want flexible CLI input options, so that I can integrate the tool into various workflows and scripts.

#### Acceptance Criteria

1. WHEN a user specifies `--config` flag with a file path THEN the CI_Time_Tracker SHALL operate in config-analysis mode
2. WHEN a user specifies `--logs` flag with file path(s) or glob pattern THEN the CI_Time_Tracker SHALL operate in log-analysis mode
3. WHEN a user pipes input via stdin THEN the CI_Time_Tracker SHALL read and process the piped content
4. WHEN a user specifies `--format` flag with value "json" THEN the CI_Time_Tracker SHALL output results in JSON format
5. WHEN a user specifies `--format` flag with value "text" or omits the flag THEN the CI_Time_Tracker SHALL output human-readable text
6. WHEN a user specifies `--output` flag with a file path THEN the CI_Time_Tracker SHALL write results to the specified file
7. WHEN a user omits the `--output` flag THEN the CI_Time_Tracker SHALL write results to stdout

### Requirement 5

**User Story:** As a developer, I want clear and actionable output reports, so that I can quickly identify and address pipeline issues.

#### Acceptance Criteria

1. WHEN the CI_Time_Tracker completes config analysis THEN the CI_Time_Tracker SHALL display a summary listing all steps with their estimated durations
2. WHEN the CI_Time_Tracker completes log analysis THEN the CI_Time_Tracker SHALL display duration statistics, slowest steps, and flaky steps
3. WHEN generating JSON output THEN the CI_Time_Tracker SHALL produce valid JSON with consistent schema including steps array, statistics object, and metadata
4. WHEN generating text output THEN the CI_Time_Tracker SHALL format results with clear headers, aligned columns, and visual indicators for issues
5. WHEN the CI_Time_Tracker serializes report data to JSON THEN the CI_Time_Tracker SHALL produce output that can be deserialized back to equivalent report data

### Requirement 6

**User Story:** As a developer, I want the tool to handle malformed input gracefully, so that I can trust the tool in automated pipelines.

#### Acceptance Criteria

1. WHEN the CI_Time_Tracker encounters invalid YAML syntax THEN the CI_Time_Tracker SHALL report the parsing error with line number and continue processing other files
2. WHEN the CI_Time_Tracker encounters invalid JSON syntax THEN the CI_Time_Tracker SHALL report the parsing error with position and continue processing other files
3. WHEN a build log contains no parseable timing information THEN the CI_Time_Tracker SHALL report a warning and exclude the log from statistics
4. WHEN the CI_Time_Tracker encounters an empty input file THEN the CI_Time_Tracker SHALL report a warning and skip the file
5. IF a required input file does not exist THEN the CI_Time_Tracker SHALL exit with a non-zero status code and descriptive error message

### Requirement 7

**User Story:** As a contributor, I want proper project documentation and structure, so that I can understand and contribute to the codebase.

#### Acceptance Criteria

1. THE CI_Time_Tracker repository SHALL include a README.md with project overview, installation instructions, and usage examples
2. THE CI_Time_Tracker repository SHALL include a LICENSE file with MIT license text
3. THE CI_Time_Tracker repository SHALL include a .gitignore file excluding Python artifacts, IDE files, and OS-specific files
4. THE CI_Time_Tracker repository SHALL include unit tests covering configuration parsing, log parsing, and report generation
