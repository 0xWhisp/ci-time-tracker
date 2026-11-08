"""Tests for the script that turns a CHANGELOG entry into release notes."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release_notes", ROOT / ".github" / "scripts" / "release_notes.py")
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)

CHANGELOG = """# Changelog

## [Unreleased]

- Not yet.

## [0.3.1] - 2026-11-01

### Added

- `--version`.

## [0.3.0] - 2026-10-30

- HTML report.

[0.3.1]: https://example.com/v0.3.1
[0.3.0]: https://example.com/v0.3.0
"""


class TestReleaseNotes:
    def test_a_section_between_two_others(self):
        assert release_notes.release_notes(CHANGELOG, "v0.3.1") == "### Added\n\n- `--version`.\n"

    def test_the_last_section_stops_at_the_link_definitions(self):
        assert release_notes.release_notes(CHANGELOG, "0.3.0") == "- HTML report.\n"

    def test_a_missing_version_fails(self):
        with pytest.raises(SystemExit, match="no entry for 9.9.9"):
            release_notes.release_notes(CHANGELOG, "v9.9.9")

    def test_every_released_version_has_notes(self):
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        version = (ROOT / "pyproject.toml").read_text(encoding="utf-8").split('version = "')[1].split('"')[0]

        assert release_notes.release_notes(changelog, version).strip()
