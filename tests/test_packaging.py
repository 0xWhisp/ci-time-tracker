"""Packaging consistency checks."""

import re
from pathlib import Path

import ci_time_tracker


def test_version_is_declared_consistently():
    """The release workflow requires pyproject and __version__ to agree.

    Parsed with a regex rather than tomllib, which Python 3.10 lacks.
    """
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)

    assert match is not None
    assert ci_time_tracker.__version__ == match.group(1)
