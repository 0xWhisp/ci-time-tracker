# Tech Stack

## Language
- Python 3.x

## Dependencies
- PyYAML: YAML parsing for CI configs
- Hypothesis: Property-based testing

## Project Structure
- Package: `ci_time_tracker/`
- Tests: `tests/`
- Config: `pyproject.toml`

## Common Commands

```bash
# Install in development mode
pip install -e .

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run tests with coverage
pytest --cov=ci_time_tracker

# Run the CLI
ci-time-tracker --config <path>
ci-time-tracker --logs <path>
python -m ci_time_tracker
```

## Code Style
- Use dataclasses for data models
- Type hints required on all functions
- Minimal dependencies (stdlib + PyYAML only for runtime)

## Testing
- Framework: pytest
- Property-based testing: Hypothesis (minimum 100 iterations per property)
- Property tests must be annotated: `**Feature: ci-time-tracker, Property {number}: {property_text}**`
