"""Local cache for CI provider API responses.

Listing workflow runs is cheap, but every run costs an extra request for its
jobs. Those payloads no longer change once a run has completed, so they are
stored in a small SQLite database between invocations. Workflow files are
stored too, keyed by the commit they were read at, which pins their content.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_jobs (
    repo TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    run_attempt INTEGER NOT NULL,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (repo, run_id, run_attempt)
);
CREATE TABLE IF NOT EXISTS workflow_files (
    repo TEXT NOT NULL,
    path TEXT NOT NULL,
    ref TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (repo, path, ref)
);
"""


def default_cache_path() -> Path:
    """Return the cache database location for this platform.

    Overridable with the CI_TIME_TRACKER_CACHE environment variable.
    """
    override = os.environ.get("CI_TIME_TRACKER_CACHE")
    if override:
        return Path(override)

    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")

    return base / "ci-time-tracker" / "runs.db"


class RunCache:
    """Stores the jobs payload of completed runs, keyed by run and attempt."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_cache_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path))
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def get_jobs(
        self,
        repo: str,
        run_id: int | str,
        run_attempt: int = 1,
    ) -> list[dict[str, Any]] | None:
        """Return the cached jobs of a run, or None when not cached."""
        row = self._connection.execute(
            "SELECT payload FROM run_jobs WHERE repo = ? AND run_id = ? AND run_attempt = ?",
            (repo, int(run_id), int(run_attempt)),
        ).fetchone()

        if row is None:
            return None

        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            # A corrupted row should not break the run; refetch instead.
            return None

    def store_jobs(
        self,
        repo: str,
        run_id: int | str,
        run_attempt: int,
        jobs: list[dict[str, Any]],
    ) -> None:
        """Cache the jobs payload of a completed run."""
        self._connection.execute(
            "INSERT OR REPLACE INTO run_jobs "
            "(repo, run_id, run_attempt, fetched_at, payload) VALUES (?, ?, ?, ?, ?)",
            (
                repo,
                int(run_id),
                int(run_attempt),
                datetime.now(timezone.utc).isoformat(),
                json.dumps(jobs),
            ),
        )
        self._connection.commit()

    def get_workflow_file(self, repo: str, path: str, ref: str) -> str | None:
        """Return a workflow file as read at commit `ref`, or None when not cached."""
        row = self._connection.execute(
            "SELECT content FROM workflow_files WHERE repo = ? AND path = ? AND ref = ?",
            (repo, path, ref),
        ).fetchone()
        return None if row is None else row[0]

    def store_workflow_file(self, repo: str, path: str, ref: str, content: str) -> None:
        """Cache a workflow file as read at commit `ref`."""
        self._connection.execute(
            "INSERT OR REPLACE INTO workflow_files "
            "(repo, path, ref, fetched_at, content) VALUES (?, ?, ?, ?, ?)",
            (repo, path, ref, datetime.now(timezone.utc).isoformat(), content),
        )
        self._connection.commit()

    def count(self, repo: str | None = None) -> int:
        """Number of cached runs, optionally limited to one repository."""
        if repo is None:
            query, params = "SELECT COUNT(*) FROM run_jobs", ()
        else:
            query, params = "SELECT COUNT(*) FROM run_jobs WHERE repo = ?", (repo,)

        return int(self._connection.execute(query, params).fetchone()[0])

    def close(self) -> None:
        """Close the underlying database connection."""
        self._connection.close()

    def __enter__(self) -> "RunCache":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
