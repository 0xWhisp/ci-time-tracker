"""Tests for config parser module."""

import pytest
import yaml
from hypothesis import given, strategies as st, settings

from ci_time_tracker.config_parser import (
    parse_config,
    detect_provider,
    ConfigParseError,
    UnsupportedProviderError,
)
from ci_time_tracker.models import PipelineConfig


class TestGitHubActionsParser:
    """Unit tests for GitHub Actions config parsing."""

    def test_parse_simple_workflow(self):
        """Test parsing a simple GitHub Actions workflow."""
        config = """
name: CI
on: [push, pull_request]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Install deps
        run: npm install
      - name: Run tests
        run: npm test
"""
        result = parse_config(config, provider="github")
        assert result.provider == "github"
        assert result.stages == ["build"]
        assert len(result.steps) == 3
        assert result.steps[0].name == "Checkout"
        assert result.steps[0].stage == "build"

    def test_parse_multi_job_workflow(self):
        """Test parsing workflow with multiple jobs."""
        config = """
name: CI
on: push
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - name: Lint code
        run: npm run lint
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Run tests
        run: npm test
"""
        result = parse_config(config, provider="github")
        assert result.provider == "github"
        assert set(result.stages) == {"lint", "test"}
        assert len(result.steps) == 2

    def test_parse_step_without_name(self):
        """Test parsing steps without explicit names."""
        config = """
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo Hello
"""
        result = parse_config(config, provider="github")
        assert len(result.steps) == 2
        assert result.steps[0].name == "actions/checkout@v4"
        assert "echo" in result.steps[1].name


class TestGitLabCIParser:
    """Unit tests for GitLab CI config parsing."""

    def test_parse_simple_config(self):
        """Test parsing a simple GitLab CI config."""
        config = """
stages:
  - build
  - test

build-job:
  stage: build
  script:
    - npm install

test-job:
  stage: test
  script:
    - npm test
"""
        result = parse_config(config, provider="gitlab")
        assert result.provider == "gitlab"
        assert "build" in result.stages
        assert "test" in result.stages
        assert len(result.steps) == 2

    def test_parse_multi_script_job(self):
        """Test parsing job with multiple script commands."""
        config = """
stages:
  - build

build-job:
  stage: build
  script:
    - npm install
    - npm run build
    - npm run lint
"""
        result = parse_config(config, provider="gitlab")
        assert len(result.steps) == 3
        assert result.steps[0].name == "build-job[0]"
        assert result.steps[1].name == "build-job[1]"
        assert result.steps[2].name == "build-job[2]"

    def test_parse_single_script_string(self):
        """Test parsing job with single script as string."""
        config = """
stages:
  - test

test-job:
  stage: test
  script: npm test
"""
        result = parse_config(config, provider="gitlab")
        assert len(result.steps) == 1
        assert result.steps[0].name == "test-job"


class TestCircleCIParser:
    """Unit tests for CircleCI config parsing."""

    def test_parse_simple_config(self):
        """Test parsing a simple CircleCI config."""
        config = """
version: 2.1
jobs:
  build:
    docker:
      - image: cimg/node:18.0
    steps:
      - checkout
      - run:
          name: Install dependencies
          command: npm install
      - run:
          name: Run tests
          command: npm test
workflows:
  main:
    jobs:
      - build
"""
        result = parse_config(config, provider="circleci")
        assert result.provider == "circleci"
        assert result.stages == ["build"]
        assert len(result.steps) == 3
        assert result.steps[0].name == "checkout"
        assert result.steps[1].name == "Install dependencies"
        assert result.steps[2].name == "Run tests"

    def test_parse_simple_run_step(self):
        """Test parsing simple run step (string format)."""
        config = """
version: 2.1
jobs:
  test:
    docker:
      - image: cimg/base:stable
    steps:
      - run: echo Hello
workflows:
  main:
    jobs:
      - test
"""
        result = parse_config(config, provider="circleci")
        assert len(result.steps) == 1
        assert "echo" in result.steps[0].name


class TestProviderDetection:
    """Unit tests for CI provider auto-detection."""

    def test_detect_github_actions(self):
        """Test detection of GitHub Actions config."""
        config = """
name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
"""
        assert detect_provider(config) == "github"

    def test_detect_gitlab_ci(self):
        """Test detection of GitLab CI config."""
        config = """
stages:
  - build
  - test

build-job:
  stage: build
  script:
    - npm install
"""
        assert detect_provider(config) == "gitlab"

    def test_detect_circleci(self):
        """Test detection of CircleCI config."""
        config = """
version: 2.1
jobs:
  build:
    docker:
      - image: cimg/node:18.0
    steps:
      - checkout
"""
        assert detect_provider(config) == "circleci"

    def test_unsupported_format_raises_error(self):
        """Test that unsupported format raises UnsupportedProviderError."""
        config = """
some_random_key: value
another_key:
  nested: data
"""
        with pytest.raises(UnsupportedProviderError) as exc_info:
            detect_provider(config)
        assert "Unable to detect CI provider" in str(exc_info.value)


class TestMalformedInputHandling:
    """Unit tests for handling malformed YAML/JSON input."""

    def test_invalid_yaml_syntax(self):
        """Test handling of invalid YAML syntax."""
        invalid_yaml = """
name: CI
jobs:
  build:
    - this is invalid
      indentation: error
    steps:
"""
        with pytest.raises(ConfigParseError) as exc_info:
            parse_config(invalid_yaml)
        assert "YAML" in str(exc_info.value) or "Invalid" in str(exc_info.value)

    def test_invalid_yaml_with_tabs(self):
        """Test handling of YAML with tab characters."""
        invalid_yaml = "name: CI\n\tjobs:\n\t\tbuild:"
        with pytest.raises(ConfigParseError) as exc_info:
            parse_config(invalid_yaml)
        assert "YAML" in str(exc_info.value) or "syntax" in str(exc_info.value).lower()

    def test_empty_content(self):
        """Test handling of empty configuration content."""
        with pytest.raises(ConfigParseError) as exc_info:
            parse_config("")
        assert "Empty" in str(exc_info.value) or "empty" in str(exc_info.value)

    def test_whitespace_only_content(self):
        """Test handling of whitespace-only content."""
        with pytest.raises(ConfigParseError) as exc_info:
            parse_config("   \n\t\n   ")
        assert "Empty" in str(exc_info.value) or "empty" in str(exc_info.value)

    def test_non_dict_yaml(self):
        """Test handling of YAML that parses to non-dict."""
        with pytest.raises(ConfigParseError) as exc_info:
            parse_config("- item1\n- item2\n- item3")
        assert "object" in str(exc_info.value).lower() or "dict" in str(exc_info.value).lower()

    def test_scalar_yaml(self):
        """Test handling of YAML that parses to scalar."""
        with pytest.raises(ConfigParseError) as exc_info:
            parse_config("just a string")
        assert "object" in str(exc_info.value).lower() or "dict" in str(exc_info.value).lower()

    def test_unsupported_provider_explicit(self):
        """Test handling of explicitly unsupported provider."""
        config = """
name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo test
"""
        with pytest.raises(UnsupportedProviderError) as exc_info:
            parse_config(config, provider="jenkins")
        assert "jenkins" in str(exc_info.value).lower() or "Unsupported" in str(exc_info.value)


class TestEdgeCases:
    """Unit tests for edge cases in config parsing."""

    def test_github_empty_jobs(self):
        """Test GitHub Actions config with empty jobs section."""
        config = """
name: CI
on: push
jobs: {}
"""
        result = parse_config(config, provider="github")
        assert result.steps == []
        assert result.stages == []

    def test_gitlab_no_stages_defined(self):
        """Test GitLab CI config without explicit stages."""
        config = """
build-job:
  script:
    - npm install
"""
        result = parse_config(config, provider="gitlab")
        assert len(result.steps) == 1

    def test_circleci_empty_steps(self):
        """Test CircleCI config with job having no steps."""
        config = """
version: 2.1
jobs:
  empty-job:
    docker:
      - image: cimg/base:stable
    steps: []
workflows:
  main:
    jobs:
      - empty-job
"""
        result = parse_config(config, provider="circleci")
        assert result.stages == ["empty-job"]
        assert result.steps == []


# =============================================================================
# Property-Based Tests - Hypothesis Strategies
# =============================================================================

# Valid step/job names: alphanumeric with hyphens/underscores, must start with letter
valid_name = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
    min_size=1,
    max_size=30,
).filter(lambda s: s[0].isalpha())

# Valid shell commands
valid_command = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789 _-./$"),
    min_size=1,
    max_size=50,
)


@st.composite
def github_step(draw):
    """Generate a valid GitHub Actions step."""
    name = draw(valid_name)
    step = {"name": name}
    if draw(st.booleans()):
        step["run"] = draw(valid_command)
    else:
        step["uses"] = f"actions/{draw(valid_name)}@v1"
    return step


@st.composite
def github_job(draw, job_name):
    """Generate a valid GitHub Actions job."""
    steps = draw(st.lists(github_step(), min_size=1, max_size=5))
    return {"runs-on": "ubuntu-latest", "steps": steps}


@st.composite
def github_actions_config(draw):
    """Generate a valid GitHub Actions workflow config."""
    job_names = draw(st.lists(valid_name, min_size=1, max_size=3, unique=True))
    
    jobs = {}
    expected_steps = []
    expected_stages = []
    
    for job_name in job_names:
        job = draw(github_job(job_name))
        jobs[job_name] = job
        expected_stages.append(job_name)
        
        for step in job["steps"]:
            expected_steps.append({"name": step["name"], "stage": job_name})
    
    config = {"name": "Test Workflow", "on": ["push"], "jobs": jobs}
    return config, expected_steps, expected_stages


@st.composite
def gitlab_job(draw, job_name, stage_name):
    """Generate a valid GitLab CI job."""
    scripts = draw(st.lists(valid_command, min_size=1, max_size=3))
    return {"stage": stage_name, "script": scripts}


# Reserved GitLab CI keys that cannot be used as job names
GITLAB_RESERVED_KEYS = {
    'stages', 'image', 'variables', 'before_script', 'after_script',
    'cache', 'services', 'include', 'default', 'workflow'
}

# Valid GitLab job names: exclude reserved keys
valid_gitlab_job_name = valid_name.filter(lambda s: s not in GITLAB_RESERVED_KEYS)


@st.composite
def gitlab_ci_config(draw):
    """Generate a valid GitLab CI config."""
    stage_names = draw(st.lists(valid_name, min_size=1, max_size=3, unique=True))
    job_names = draw(st.lists(valid_gitlab_job_name, min_size=1, max_size=4, unique=True))
    
    config = {"stages": stage_names}
    expected_steps = []
    
    for job_name in job_names:
        stage = draw(st.sampled_from(stage_names))
        job = draw(gitlab_job(job_name, stage))
        config[job_name] = job
        
        scripts = job["script"]
        if len(scripts) == 1:
            expected_steps.append({"name": job_name, "stage": stage})
        else:
            for idx in range(len(scripts)):
                expected_steps.append({"name": f"{job_name}[{idx}]", "stage": stage})
    
    return config, expected_steps, stage_names


@st.composite
def circleci_step(draw):
    """Generate a valid CircleCI step."""
    step_type = draw(st.sampled_from(["run", "checkout"]))
    if step_type == "checkout":
        return "checkout", "checkout"
    else:
        name = draw(valid_name)
        command = draw(valid_command)
        return {"run": {"name": name, "command": command}}, name


@st.composite
def circleci_job(draw, job_name):
    """Generate a valid CircleCI job."""
    step_data = draw(st.lists(circleci_step(), min_size=1, max_size=5))
    steps = [s[0] for s in step_data]
    step_names = [s[1] for s in step_data]
    return {"docker": [{"image": "cimg/base:stable"}], "steps": steps}, step_names


@st.composite
def circleci_config(draw):
    """Generate a valid CircleCI config."""
    job_names = draw(st.lists(valid_name, min_size=1, max_size=3, unique=True))
    
    jobs = {}
    expected_steps = []
    expected_stages = []
    
    for job_name in job_names:
        job, step_names = draw(circleci_job(job_name))
        jobs[job_name] = job
        expected_stages.append(job_name)
        
        for step_name in step_names:
            expected_steps.append({"name": step_name, "stage": job_name})
    
    config = {
        "version": 2.1,
        "jobs": jobs,
        "workflows": {"main": {"jobs": job_names}}
    }
    return config, expected_steps, expected_stages


# =============================================================================
# Property-Based Tests
# =============================================================================

class TestConfigParsingCompleteness:
    """
    **Feature: ci-time-tracker, Property 1: Config parsing extracts all steps**
    
    For any valid CI configuration structure (GitHub Actions, GitLab CI, or CircleCI),
    parsing the configuration SHALL produce a PipelineConfig containing every step
    and stage defined in the input, with no steps omitted or duplicated.
    
    **Validates: Requirements 1.1, 1.2, 1.3, 1.4**
    """

    @settings(max_examples=100)
    @given(github_actions_config())
    def test_github_actions_extracts_all_steps(self, config_data):
        """Property: GitHub Actions parsing extracts all steps without omission."""
        config, expected_steps, expected_stages = config_data
        yaml_content = yaml.dump(config)
        
        result = parse_config(yaml_content, provider="github")
        
        assert result.provider == "github"
        assert set(result.stages) == set(expected_stages)
        assert len(result.steps) == len(expected_steps)
        
        result_step_names = {(s.name, s.stage) for s in result.steps}
        expected_step_names = {(s["name"], s["stage"]) for s in expected_steps}
        assert result_step_names == expected_step_names

    @settings(max_examples=100)
    @given(gitlab_ci_config())
    def test_gitlab_ci_extracts_all_steps(self, config_data):
        """Property: GitLab CI parsing extracts all steps without omission."""
        config, expected_steps, expected_stages = config_data
        yaml_content = yaml.dump(config)
        
        result = parse_config(yaml_content, provider="gitlab")
        
        assert result.provider == "gitlab"
        for stage in expected_stages:
            assert stage in result.stages
        assert len(result.steps) == len(expected_steps)
        
        result_step_names = {(s.name, s.stage) for s in result.steps}
        expected_step_names = {(s["name"], s["stage"]) for s in expected_steps}
        assert result_step_names == expected_step_names

    @settings(max_examples=100)
    @given(circleci_config())
    def test_circleci_extracts_all_steps(self, config_data):
        """Property: CircleCI parsing extracts all steps without omission."""
        config, expected_steps, expected_stages = config_data
        yaml_content = yaml.dump(config)
        
        result = parse_config(yaml_content, provider="circleci")
        
        assert result.provider == "circleci"
        assert set(result.stages) == set(expected_stages)
        assert len(result.steps) == len(expected_steps)
        
        result_step_names = {(s.name, s.stage) for s in result.steps}
        expected_step_names = {(s["name"], s["stage"]) for s in expected_steps}
        assert result_step_names == expected_step_names
