"""Print a version's CHANGELOG entry, for its GitHub release notes.

Usage: python release_notes.py v0.3.1 [CHANGELOG.md]
"""

import re
import sys
from pathlib import Path


def release_notes(changelog: str, version: str) -> str:
    """The body of the "## [version]" section, without its heading."""
    version = version.removeprefix("v")
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n", re.MULTILINE)
    match = heading.search(changelog)
    if not match:
        raise SystemExit(f"::error::CHANGELOG.md has no entry for {version}")

    following = re.compile(r"^(## |\[[^\]]+\]: )", re.MULTILINE).search(changelog, match.end())
    return changelog[match.end():following.start() if following else None].strip() + "\n"


if __name__ == "__main__":
    path = Path(sys.argv[2] if len(sys.argv) > 2 else "CHANGELOG.md")
    sys.stdout.write(release_notes(path.read_text(encoding="utf-8"), sys.argv[1]))
