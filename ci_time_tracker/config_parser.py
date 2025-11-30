"""Configuration parser for CI/CD pipeline files.

This module provides functionality to parse CI configuration files from
various providers (GitHub Actions, GitLab CI, CircleCI) and extract
pipeline step information.
"""

import yaml
from typing import Any

from ci_time_tracker.models import PipelineConfig, PipelineStep


class ConfigParseError(Exception):
    """Raised when configuration parsing fails."""
    
    def __init__(self, message: str, line: int | None = None, details: str | None = None):
        self.message = message
        self.line = line
        self.details = details
        super().__init__(self._format_message())
    
    def _format_message(self) -> str:
        parts = [f"Error: CONFIG_PARSE_ERROR - {self.message}"]
        if self.line is not None:
            parts.append(f"  Line: {self.line}")
        if self.details:
            parts.append(f"  Details: {self.details}")
        return "\n".join(parts)


class UnsupportedProviderError(Exception):
    """Raised when CI provider cannot be detected or is unsupported."""
    
    def __init__(self, message: str):
        self.message = message
        super().__init__(f"Error: UNSUPPORTED_PROVIDER - {message}")


def detect_provider(content: str) -> str:
    """Auto-detect CI provider from configuration content.
    
    Analyzes the structure and keys in the YAML/JSON content to determine
    which CI provider the configuration belongs to.
    
    Args:
        content: Raw configuration file content (YAML or JSON string)
        
    Returns:
        Provider identifier: 'github', 'gitlab', or 'circleci'
        
    Raises:
        UnsupportedProviderError: If provider cannot be detected
        ConfigParseError: If content cannot be parsed as YAML/JSON
    """
    if not content or not content.strip():
        raise ConfigParseError("Empty configuration content")
    
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        line = None
        if hasattr(e, 'problem_mark') and e.problem_mark:
            line = e.problem_mark.line + 1
        raise ConfigParseError(
            "Invalid YAML syntax",
            line=line,
            details=str(e)
        )
    
    if not isinstance(data, dict):
        raise ConfigParseError(
            "Configuration must be a YAML/JSON object",
            details=f"Got {type(data).__name__} instead"
        )
    
    # CircleCI detection FIRST: has 'version' key with numeric value
    # CircleCI jobs have 'docker', 'machine', or 'executor' keys
    if 'version' in data:
        if 'jobs' in data:
            jobs = data['jobs']
            if isinstance(jobs, dict):
                for job in jobs.values():
                    if isinstance(job, dict):
                        # CircleCI-specific executor keys
                        if any(k in job for k in ['docker', 'machine', 'executor', 'macos']):
                            return 'circleci'
                        # CircleCI steps are a list with 'checkout' string or 'run' dicts
                        if 'steps' in job and isinstance(job['steps'], list):
                            for step in job['steps']:
                                if isinstance(step, str) and step == 'checkout':
                                    return 'circleci'
                                if isinstance(step, dict) and any(k in step for k in ['run', 'checkout', 'save_cache', 'restore_cache', 'persist_to_workspace', 'attach_workspace']):
                                    return 'circleci'
        if 'workflows' in data and isinstance(data.get('workflows'), dict):
            # CircleCI workflows have version and jobs list
            workflows = data['workflows']
            for wf in workflows.values():
                if isinstance(wf, dict) and 'jobs' in wf:
                    return 'circleci'
        # If version is present with orbs, it's CircleCI
        if 'orbs' in data:
            return 'circleci'
    
    # GitHub Actions detection: has 'jobs' key and 'on' trigger or 'runs-on'
    if 'jobs' in data and isinstance(data.get('jobs'), dict):
        # 'on' trigger is GitHub-specific
        if 'on' in data:
            return 'github'
        jobs = data['jobs']
        # 'runs-on' is GitHub-specific
        for job in jobs.values():
            if isinstance(job, dict) and 'runs-on' in job:
                return 'github'
        # Check if jobs have 'steps' arrays (GitHub Actions pattern)
        for job in jobs.values():
            if isinstance(job, dict) and 'steps' in job:
                return 'github'
    
    # GitLab CI detection: has 'stages' key or jobs with 'script' key
    if 'stages' in data and isinstance(data.get('stages'), list):
        return 'gitlab'
    
    # GitLab jobs have 'script' key directly
    for key, value in data.items():
        if key.startswith('.'):  # GitLab hidden jobs/templates
            continue
        if isinstance(value, dict) and 'script' in value:
            return 'gitlab'
    
    # If we have 'image' at top level, likely GitLab
    if 'image' in data and isinstance(data.get('image'), (str, dict)):
        # Check for GitLab-specific job patterns
        for key, value in data.items():
            if key not in ('image', 'variables', 'before_script', 'after_script', 'cache', 'services'):
                if isinstance(value, dict):
                    return 'gitlab'
    
    raise UnsupportedProviderError(
        "Unable to detect CI provider from configuration. "
        "Supported providers: GitHub Actions, GitLab CI, CircleCI"
    )


def parse_config(content: str, provider: str | None = None) -> PipelineConfig:
    """Parse CI configuration content and return structured pipeline data.
    
    Main entry point for configuration parsing. Auto-detects the CI provider
    if not specified, then delegates to the appropriate provider-specific parser.
    
    Args:
        content: Raw configuration file content (YAML or JSON string)
        provider: Optional provider hint ('github', 'gitlab', 'circleci').
                  If None, provider will be auto-detected.
                  
    Returns:
        PipelineConfig containing extracted steps, stages, and metadata
        
    Raises:
        ConfigParseError: If content cannot be parsed
        UnsupportedProviderError: If provider is unsupported or cannot be detected
    """
    if not content or not content.strip():
        raise ConfigParseError("Empty configuration content")
    
    # Parse YAML first
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        line = None
        if hasattr(e, 'problem_mark') and e.problem_mark:
            line = e.problem_mark.line + 1
        raise ConfigParseError(
            "Invalid YAML syntax",
            line=line,
            details=str(e)
        )
    
    if not isinstance(data, dict):
        raise ConfigParseError(
            "Configuration must be a YAML/JSON object",
            details=f"Got {type(data).__name__} instead"
        )
    
    # Detect provider if not specified
    if provider is None:
        provider = detect_provider(content)
    
    # Normalize provider name
    provider = provider.lower().strip()
    
    # Route to appropriate parser
    if provider == 'github':
        return parse_github_actions(data)
    elif provider == 'gitlab':
        return parse_gitlab_ci(data)
    elif provider == 'circleci':
        return parse_circleci(data)
    else:
        raise UnsupportedProviderError(
            f"Unsupported CI provider: '{provider}'. "
            "Supported providers: github, gitlab, circleci"
        )


def parse_github_actions(data: dict[str, Any]) -> PipelineConfig:
    """Parse GitHub Actions workflow YAML.
    
    Extracts jobs, steps, and their order from a GitHub Actions workflow file.
    
    Args:
        data: Parsed YAML data dictionary
        
    Returns:
        PipelineConfig with extracted GitHub Actions steps
    """
    steps: list[PipelineStep] = []
    stages: list[str] = []
    metadata: dict[str, Any] = {}
    
    # Extract workflow name
    if 'name' in data:
        metadata['workflow_name'] = data['name']
    
    # Extract trigger events
    if 'on' in data:
        metadata['triggers'] = data['on']
    
    # Process jobs
    jobs = data.get('jobs', {})
    if not isinstance(jobs, dict):
        return PipelineConfig(provider='github', steps=steps, stages=stages, metadata=metadata)
    
    for job_name, job_config in jobs.items():
        if not isinstance(job_config, dict):
            continue
            
        # Job name becomes a stage
        stages.append(job_name)
        
        # Extract job-level metadata
        if 'runs-on' in job_config:
            metadata.setdefault('runners', {})[job_name] = job_config['runs-on']
        
        # Process steps within the job
        job_steps = job_config.get('steps', [])
        if not isinstance(job_steps, list):
            continue
            
        for idx, step in enumerate(job_steps):
            if not isinstance(step, dict):
                continue
            
            # Determine step name
            step_name = step.get('name')
            if not step_name:
                # Use 'uses' action name or 'run' command as fallback
                if 'uses' in step:
                    step_name = step['uses']
                elif 'run' in step:
                    # Use first line of run command, truncated
                    run_cmd = step['run']
                    if isinstance(run_cmd, str):
                        first_line = run_cmd.split('\n')[0][:50]
                        step_name = f"run: {first_line}"
                else:
                    step_name = f"step_{idx + 1}"
            
            # Extract command
            command = None
            if 'run' in step:
                command = step['run']
            elif 'uses' in step:
                command = step['uses']
            
            steps.append(PipelineStep(
                name=step_name,
                stage=job_name,
                command=command,
                estimated_duration=None
            ))
    
    return PipelineConfig(
        provider='github',
        steps=steps,
        stages=stages,
        metadata=metadata
    )


def parse_gitlab_ci(data: dict[str, Any]) -> PipelineConfig:
    """Parse GitLab CI YAML.
    
    Extracts stages, jobs, and script commands from a GitLab CI configuration.
    
    Args:
        data: Parsed YAML data dictionary
        
    Returns:
        PipelineConfig with extracted GitLab CI steps
    """
    steps: list[PipelineStep] = []
    metadata: dict[str, Any] = {}
    
    # Extract stages (or use default)
    stages = data.get('stages', [])
    if not isinstance(stages, list):
        stages = []
    
    # Extract global image
    if 'image' in data:
        metadata['image'] = data['image']
    
    # Extract global variables
    if 'variables' in data:
        metadata['variables'] = data['variables']
    
    # Reserved GitLab CI keys that are not jobs
    reserved_keys = {
        'stages', 'image', 'variables', 'before_script', 'after_script',
        'cache', 'services', 'include', 'default', 'workflow'
    }
    
    # Process jobs
    for key, value in data.items():
        # Skip reserved keys and hidden jobs (starting with .)
        if key in reserved_keys or key.startswith('.'):
            continue
        
        if not isinstance(value, dict):
            continue
        
        job_name = key
        job_config = value
        
        # Determine stage for this job
        job_stage = job_config.get('stage')
        if job_stage and job_stage not in stages:
            stages.append(job_stage)
        
        # Extract script commands as steps
        script = job_config.get('script', [])
        if isinstance(script, str):
            script = [script]
        
        if isinstance(script, list):
            for idx, cmd in enumerate(script):
                if not isinstance(cmd, str):
                    continue
                
                # Create step name from job name and command index
                if len(script) == 1:
                    step_name = job_name
                else:
                    step_name = f"{job_name}[{idx}]"
                
                steps.append(PipelineStep(
                    name=step_name,
                    stage=job_stage,
                    command=cmd,
                    estimated_duration=None
                ))
        
        # If no script but job exists, add job as a step
        if not script and 'extends' not in job_config:
            steps.append(PipelineStep(
                name=job_name,
                stage=job_stage,
                command=None,
                estimated_duration=None
            ))
    
    return PipelineConfig(
        provider='gitlab',
        steps=steps,
        stages=stages,
        metadata=metadata
    )


def parse_circleci(data: dict[str, Any]) -> PipelineConfig:
    """Parse CircleCI configuration.
    
    Extracts jobs, steps, and executor information from a CircleCI config.
    
    Args:
        data: Parsed YAML data dictionary
        
    Returns:
        PipelineConfig with extracted CircleCI steps
    """
    steps: list[PipelineStep] = []
    stages: list[str] = []
    metadata: dict[str, Any] = {}
    
    # Extract version
    if 'version' in data:
        metadata['version'] = data['version']
    
    # Extract orbs
    if 'orbs' in data:
        metadata['orbs'] = data['orbs']
    
    # Extract executors
    if 'executors' in data:
        metadata['executors'] = data['executors']
    
    # Process jobs
    jobs = data.get('jobs', {})
    if isinstance(jobs, dict):
        for job_name, job_config in jobs.items():
            if not isinstance(job_config, dict):
                continue
            
            # Job name becomes a stage
            stages.append(job_name)
            
            # Extract executor info
            for exec_key in ['docker', 'machine', 'macos', 'executor']:
                if exec_key in job_config:
                    metadata.setdefault('job_executors', {})[job_name] = {
                        'type': exec_key,
                        'config': job_config[exec_key]
                    }
                    break
            
            # Process steps within the job
            job_steps = job_config.get('steps', [])
            if not isinstance(job_steps, list):
                continue
            
            for idx, step in enumerate(job_steps):
                step_name = None
                command = None
                
                if isinstance(step, str):
                    # Simple step like 'checkout'
                    step_name = step
                    command = step
                elif isinstance(step, dict):
                    # Complex step
                    if 'run' in step:
                        run_config = step['run']
                        if isinstance(run_config, str):
                            step_name = run_config[:50]
                            command = run_config
                        elif isinstance(run_config, dict):
                            step_name = run_config.get('name', f"run_{idx + 1}")
                            command = run_config.get('command')
                    else:
                        # Other step types (checkout, save_cache, etc.)
                        step_type = list(step.keys())[0] if step else f"step_{idx + 1}"
                        step_name = step_type
                        command = str(step.get(step_type, ''))
                
                if step_name is None:
                    step_name = f"step_{idx + 1}"
                
                steps.append(PipelineStep(
                    name=step_name,
                    stage=job_name,
                    command=command,
                    estimated_duration=None
                ))
    
    # Process workflows to understand job ordering
    workflows = data.get('workflows', {})
    if isinstance(workflows, dict):
        workflow_info = {}
        for wf_name, wf_config in workflows.items():
            if wf_name == 'version':
                continue
            if isinstance(wf_config, dict) and 'jobs' in wf_config:
                workflow_info[wf_name] = wf_config['jobs']
        if workflow_info:
            metadata['workflows'] = workflow_info
    
    return PipelineConfig(
        provider='circleci',
        steps=steps,
        stages=stages,
        metadata=metadata
    )
