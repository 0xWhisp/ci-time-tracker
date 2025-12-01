"""Property-based tests for config parser module.

Tests the config parsing completeness property across all supported CI providers.

**Feature: ci-time-tracker, Property 1: Config parsing extracts all steps**
**Validates: Requirements 1.1, 1.2, 1.3, 1.4**
"""

import yaml
from hypothesis import given, strategies as st, settings

from ci_time_tracker.config_parser import parse_config
from ci_time_tracker.models import PipelineConfig


# Reserved GitLab CI keys that cannot be used as job names
GITLAB_RESERVED_KEYS = {
    'stages', 'image', 'variables', 'before_script', 'after_script',
    'cache', 'services', 'include', 'default', 'workflow'
}

# Valid step/job names: alphanumeric with hyphens/underscores, must start with letter
valid_name = st.text(
    alphabet=st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789_-"),
    min_size=1,
    max_size=30,
).filter(lambda s: s[0].isalpha())

# GitLab job names must not be reserved keys
gitlab_job_name = valid_name.filter(lambda s: s not in GITLAB_RESERVED_KEYS)

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


@st.composite
def gitlab_ci_config(draw):
    """Generate a valid GitLab CI config."""
    stage_names = draw(st.lists(valid_name, min_size=1, max_size=3, unique=True))
    # Use gitlab_job_name to exclude reserved keys
    job_names = draw(st.lists(gitlab_job_name, min_size=1, max_size=4, unique=True))
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
    config = {"version": 2.1, "jobs": jobs, "workflows": {"main": {"jobs": job_names}}}
    return config, expected_steps, expected_stages


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
