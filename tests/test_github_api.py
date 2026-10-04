"""Tests for GitHub Actions API ingestion.

Every test runs against recorded payloads through an injected transport, so
the suite never touches the network.
"""

import urllib.error
from typing import Any

import pytest

from ci_time_tracker.cache import RunCache
from ci_time_tracker.github_api import (
    DEFAULT_API_URL,
    GitHubAPIError,
    GitHubClient,
    _http_error,
    build_log_from_run,
    fetch_build_logs,
    resolve_token,
)


# --- Recorded payloads (trimmed to the fields the client reads) -------------

RUN = {
    "id": 9876543210,
    "name": "CI",
    "head_branch": "main",
    "head_sha": "a1b2c3d4e5f6a7b8",
    "run_attempt": 1,
    "status": "completed",
    "conclusion": "failure",
    "event": "push",
    "created_at": "2026-09-01T10:00:00Z",
    "run_started_at": "2026-09-01T10:00:10Z",
    "updated_at": "2026-09-01T10:05:00Z",
    "html_url": "https://github.com/acme/web/actions/runs/9876543210",
}

JOBS = {
    "total_count": 2,
    "jobs": [
        {
            "id": 111,
            "name": "build",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-09-01T10:00:00Z",
            "started_at": "2026-09-01T10:00:12Z",
            "completed_at": "2026-09-01T10:01:45Z",
            "labels": ["ubuntu-latest"],
            "runner_name": "GitHub Actions 3",
            "steps": [
                {
                    "name": "Set up job",
                    "conclusion": "success",
                    "number": 1,
                    "started_at": "2026-09-01T10:00:12Z",
                    "completed_at": "2026-09-01T10:00:15Z",
                },
                {
                    "name": "Run npm ci",
                    "conclusion": "success",
                    "number": 2,
                    "started_at": "2026-09-01T10:00:15Z",
                    "completed_at": "2026-09-01T10:01:45Z",
                },
            ],
        },
        {
            "id": 222,
            "name": "test",
            "status": "completed",
            "conclusion": "failure",
            "created_at": "2026-09-01T10:00:00Z",
            "started_at": "2026-09-01T10:01:50Z",
            "completed_at": "2026-09-01T10:04:30Z",
            "labels": ["ubuntu-latest", "self-hosted"],
            "runner_name": "runner-7",
            "steps": [
                {
                    "name": "Run npm test",
                    "conclusion": "failure",
                    "number": 1,
                    "started_at": "2026-09-01T10:01:50Z",
                    "completed_at": "2026-09-01T10:04:30Z",
                },
                {
                    "name": "Upload coverage",
                    "conclusion": "skipped",
                    "number": 2,
                    "started_at": None,
                    "completed_at": None,
                },
            ],
        },
    ],
}


class FakeTransport:
    """Serves recorded payloads by URL substring and records every request."""

    def __init__(self, routes: list[tuple[str, Any]]):
        self.routes = routes
        self.calls: list[str] = []
        self.headers_seen: list[dict[str, str]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> tuple[Any, dict[str, str]]:
        self.calls.append(url)
        self.headers_seen.append(headers)

        for pattern, payload in self.routes:
            if pattern in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload, {}

        raise AssertionError(f"Unexpected request: {url}")

    def calls_matching(self, pattern: str) -> list[str]:
        return [url for url in self.calls if pattern in url]


def default_routes(runs: list[dict] | None = None, jobs: dict | None = None):
    return [
        ("/actions/runs?", {"total_count": 1, "workflow_runs": runs if runs is not None else [RUN]}),
        ("/jobs", jobs if jobs is not None else JOBS),
    ]


class TestRunMapping:
    """Mapping a run and its jobs onto BuildLog/StepExecution."""

    def test_steps_are_qualified_with_job_name(self):
        """Identically named steps in different jobs must not collide."""
        log = build_log_from_run(RUN, JOBS["jobs"])
        names = [step.name for step in log.steps]

        assert names == [
            "build / Set up job",
            "build / Run npm ci",
            "test / Run npm test",
            "test / Upload coverage",
        ]

    def test_durations_come_from_api_timestamps(self):
        """Durations are computed from the API's own step timestamps."""
        log = build_log_from_run(RUN, JOBS["jobs"])
        durations = {step.name: step.duration_seconds for step in log.steps}

        assert durations["build / Run npm ci"] == 90.0
        assert durations["test / Run npm test"] == 160.0
        # A skipped step never ran, so it has no duration
        assert durations["test / Upload coverage"] is None

    def test_step_status_and_runner(self):
        """Conclusions map to statuses and the runner label is preserved."""
        log = build_log_from_run(RUN, JOBS["jobs"])
        by_name = {step.name: step for step in log.steps}

        assert by_name["build / Run npm ci"].status == "success"
        assert by_name["test / Run npm test"].status == "failure"
        assert by_name["test / Upload coverage"].status == "skipped"
        assert by_name["build / Run npm ci"].runner == "ubuntu-latest"
        assert by_name["test / Run npm test"].runner == "ubuntu-latest, self-hosted"
        assert by_name["test / Run npm test"].job_name == "test"

    def test_build_level_fields(self):
        """head_sha, attempt, conclusion and wall-clock duration are kept."""
        log = build_log_from_run(RUN, JOBS["jobs"])

        assert log.build_id == "9876543210"
        assert log.head_sha == "a1b2c3d4e5f6a7b8"
        assert log.run_attempt == 1
        assert log.conclusion == "failure"
        # 10:00:10 -> 10:05:00 wall clock, not the sum of step durations
        assert log.total_duration == 290.0

    def test_queue_time_is_recorded_per_job(self):
        """Job queue time (created -> started) is kept for later analysis."""
        log = build_log_from_run(RUN, JOBS["jobs"])
        queued = {job["name"]: job["queued_seconds"] for job in log.metadata["jobs"]}

        assert queued == {"build": 12.0, "test": 110.0}

    def test_rerun_marks_steps_as_retries(self):
        """A second attempt flags its steps, which feeds flaky detection."""
        rerun = {**RUN, "run_attempt": 2}
        log = build_log_from_run(rerun, JOBS["jobs"])

        assert log.run_attempt == 2
        assert all(step.is_retry for step in log.steps)

    def test_missing_steps_yield_empty_log(self):
        """A run whose jobs have no steps yet maps to a log without steps."""
        log = build_log_from_run(RUN, [{"name": "build", "steps": []}])

        assert log.steps == []
        assert log.build_id == "9876543210"


class TestClientRequests:
    """Request building, pagination and authentication."""

    def test_token_is_sent_as_bearer(self):
        transport = FakeTransport(default_routes())
        fetch_build_logs("acme/web", limit=1, token="ghp_secret", transport=transport)

        assert transport.headers_seen[0]["Authorization"] == "Bearer ghp_secret"

    def test_request_without_token_is_unauthenticated(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        transport = FakeTransport(default_routes())

        fetch_build_logs("acme/web", limit=1, transport=transport)

        assert "Authorization" not in transport.headers_seen[0]

    def test_token_falls_back_to_environment(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "from-env")
        assert resolve_token() == "from-env"
        assert resolve_token("explicit") == "explicit"

    def test_workflow_filter_uses_workflow_endpoint(self):
        transport = FakeTransport([
            ("/actions/workflows/ci.yml/runs", {"workflow_runs": [RUN]}),
            ("/jobs", JOBS),
        ])

        fetch_build_logs("acme/web", workflow="ci.yml", limit=1, transport=transport)

        assert "/repos/acme/web/actions/workflows/ci.yml/runs" in transport.calls[0]

    def test_branch_and_status_filters_are_sent(self):
        transport = FakeTransport(default_routes())
        fetch_build_logs("acme/web", branch="main", limit=1, transport=transport)

        assert "branch=main" in transport.calls[0]
        assert "status=completed" in transport.calls[0]

    def test_limit_is_respected_when_api_returns_more(self):
        """Only `limit` runs are mapped, even if a page holds more."""
        runs = [{**RUN, "id": 100 + index} for index in range(5)]
        transport = FakeTransport(default_routes(runs=runs))

        logs = fetch_build_logs("acme/web", limit=3, transport=transport)

        assert len(logs) == 3

    def test_pagination_walks_pages_until_limit(self):
        """A limit above one page keeps paging."""
        first_page = [{**RUN, "id": 1000 + index} for index in range(100)]
        transport = FakeTransport([
            ("page=1", {"workflow_runs": first_page}),
            ("page=2", {"workflow_runs": [{**RUN, "id": 2000}]}),
            ("/jobs", JOBS),
        ])

        logs = fetch_build_logs("acme/web", limit=101, transport=transport)

        assert len(logs) == 101
        assert transport.calls_matching("page=2")

    def test_empty_page_stops_pagination(self):
        transport = FakeTransport([
            ("/actions/runs?", {"workflow_runs": []}),
        ])

        assert fetch_build_logs("acme/web", limit=50, transport=transport) == []

    def test_rerun_fetches_every_attempt(self):
        """Each attempt of a re-run is fetched from its own endpoint."""
        transport = FakeTransport([
            ("/actions/runs?", {"workflow_runs": [{**RUN, "run_attempt": 3}]}),
            ("/attempts/", JOBS),
        ])

        logs = fetch_build_logs("acme/web", limit=1, transport=transport)

        for attempt in (1, 2, 3):
            assert transport.calls_matching(f"/attempts/{attempt}/jobs")
        # Oldest first, so attempts come in order
        assert [log.run_attempt for log in logs] == [1, 2, 3]
        assert [log.metadata["superseded_attempt"] for log in logs] == [True, True, False]
        assert all(log.head_sha == RUN["head_sha"] for log in logs)

    def test_earlier_attempts_do_not_count_towards_the_limit(self):
        runs = [{**RUN, "id": 1, "run_attempt": 2}, {**RUN, "id": 2}]
        transport = FakeTransport([
            ("/actions/runs?", {"workflow_runs": runs}),
            ("/jobs", JOBS),
        ])

        logs = fetch_build_logs("acme/web", limit=2, transport=transport)

        # Two runs, one of them re-run once
        assert len(logs) == 3

    def test_attempts_can_be_skipped(self):
        transport = FakeTransport([
            ("/actions/runs?", {"workflow_runs": [{**RUN, "run_attempt": 3}]}),
            ("/attempts/3/jobs", JOBS),
        ])

        logs = fetch_build_logs("acme/web", limit=1, transport=transport, include_attempts=False)

        assert len(logs) == 1
        assert not transport.calls_matching("/attempts/1/")

    def test_earlier_attempt_takes_its_timing_from_its_jobs(self):
        """The runs listing only times the latest attempt."""
        log = build_log_from_run(
            {**RUN, "run_attempt": 1, "run_started_at": None, "updated_at": None},
            JOBS["jobs"],
            superseded=True,
        )

        assert log.timestamp.isoformat() == "2026-09-01T10:00:12+00:00"  # first job start
        assert log.total_duration == 258.0  # until the last job ends, 10:04:30
        assert log.metadata["superseded_attempt"] is True

    def test_logs_are_returned_oldest_first(self):
        """The API returns newest first; trends read better oldest first."""
        runs = [
            {**RUN, "id": 3, "run_started_at": "2026-09-03T10:00:00Z"},
            {**RUN, "id": 2, "run_started_at": "2026-09-02T10:00:00Z"},
            {**RUN, "id": 1, "run_started_at": "2026-09-01T10:00:00Z"},
        ]
        transport = FakeTransport(default_routes(runs=runs))

        logs = fetch_build_logs("acme/web", limit=3, transport=transport)

        assert [log.build_id for log in logs] == ["1", "2", "3"]

    def test_jobs_pagination_follows_pages(self):
        """A job list of exactly one page size triggers another request."""
        many_jobs = {"jobs": [dict(JOBS["jobs"][0], id=index) for index in range(100)]}
        transport = FakeTransport([
            ("/actions/runs?", {"workflow_runs": [RUN]}),
            ("/jobs?per_page=100&page=1", many_jobs),
            ("/jobs?per_page=100&page=2", {"jobs": []}),
        ])

        logs = fetch_build_logs("acme/web", limit=1, transport=transport)

        assert len(logs[0].steps) == 200  # 100 jobs x 2 steps

    def test_invalid_repository_is_rejected(self):
        with pytest.raises(GitHubAPIError) as error:
            GitHubClient(repo="not-a-repo")

        assert "owner/name" in str(error.value)

    def test_api_url_is_overridable_for_enterprise(self):
        transport = FakeTransport([
            ("https://ghe.acme.com/api/v3/repos/acme/web/actions/runs", {"workflow_runs": []}),
        ])

        fetch_build_logs(
            "acme/web",
            limit=1,
            api_url="https://ghe.acme.com/api/v3/",
            transport=transport,
        )

        assert transport.calls[0].startswith("https://ghe.acme.com/api/v3/repos/")


class TestErrorMessages:
    """HTTP failures surface as actionable messages."""

    def _http_error_with(self, code: int, headers: dict[str, str]) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            url=f"{DEFAULT_API_URL}/repos/acme/web/actions/runs",
            code=code,
            msg="error",
            hdrs=headers,  # type: ignore[arg-type]
            fp=None,
        )

    def test_rate_limit_suggests_a_token(self):
        error = _http_error(
            self._http_error_with(403, {"X-RateLimit-Remaining": "0"}),
            "https://api.github.com/repos/acme/web/actions/runs",
        )

        assert "rate limit" in str(error).lower()
        assert "GITHUB_TOKEN" in str(error)

    def test_forbidden_mentions_scope(self):
        error = _http_error(
            self._http_error_with(403, {"X-RateLimit-Remaining": "42"}),
            "https://api.github.com/repos/acme/web/actions/runs",
        )

        assert "actions:read" in str(error)

    def test_not_found_mentions_repository_and_private_access(self):
        error = _http_error(
            self._http_error_with(404, {}),
            "https://api.github.com/repos/acme/web/actions/runs",
        )

        assert "Not found" in str(error)
        assert "private" in str(error)

    def test_server_error_keeps_status_code(self):
        error = _http_error(
            self._http_error_with(500, {}),
            "https://api.github.com/repos/acme/web/actions/runs",
        )

        assert error.status == 500


class TestCaching:
    """Completed runs are served from the local cache on the second pass."""

    def test_second_fetch_skips_the_jobs_request(self, tmp_path):
        cache = RunCache(tmp_path / "runs.db")
        routes = default_routes()

        first = FakeTransport(routes)
        fetch_build_logs("acme/web", limit=1, cache=cache, transport=first)
        assert first.calls_matching("/jobs")

        second = FakeTransport(routes)
        logs = fetch_build_logs("acme/web", limit=1, cache=cache, transport=second)

        assert not second.calls_matching("/jobs")
        assert len(logs[0].steps) == 4
        cache.close()

    def test_in_flight_runs_are_not_cached(self, tmp_path):
        """A run still in progress would change, so it must not be cached."""
        cache = RunCache(tmp_path / "runs.db")
        in_flight = {**RUN, "status": "in_progress"}
        transport = FakeTransport(default_routes(runs=[in_flight]))

        fetch_build_logs("acme/web", limit=1, cache=cache, transport=transport)

        assert cache.count("acme/web") == 0
        cache.close()

    def test_cache_separates_attempts_of_the_same_run(self, tmp_path):
        cache = RunCache(tmp_path / "runs.db")

        cache.store_jobs("acme/web", RUN["id"], 1, JOBS["jobs"])
        cache.store_jobs("acme/web", RUN["id"], 2, [])

        assert cache.get_jobs("acme/web", RUN["id"], 1) == JOBS["jobs"]
        assert cache.get_jobs("acme/web", RUN["id"], 2) == []
        assert cache.count("acme/web") == 2
        cache.close()

    def test_only_uncached_runs_are_fetched(self, tmp_path):
        """With a partially warm cache, only the missing runs cost a request."""
        cache = RunCache(tmp_path / "runs.db")
        runs = [{**RUN, "id": 10}, {**RUN, "id": 20}, {**RUN, "id": 30}]
        cache.store_jobs("acme/web", 20, 1, JOBS["jobs"])

        transport = FakeTransport(default_routes(runs=runs))
        logs = fetch_build_logs("acme/web", limit=3, cache=cache, transport=transport)

        assert len(transport.calls_matching("/jobs")) == 2
        # Every run still ends up with its steps, cached or freshly fetched
        assert [len(log.steps) for log in logs] == [4, 4, 4]
        assert [log.build_id for log in logs] == ["30", "20", "10"]
        cache.close()

    def test_cache_miss_returns_none(self, tmp_path):
        with RunCache(tmp_path / "runs.db") as cache:
            assert cache.get_jobs("acme/web", 1, 1) is None

    def test_cache_is_scoped_per_repository(self, tmp_path):
        with RunCache(tmp_path / "runs.db") as cache:
            cache.store_jobs("acme/web", 1, 1, JOBS["jobs"])

            assert cache.get_jobs("other/repo", 1, 1) is None
            assert cache.count("other/repo") == 0
