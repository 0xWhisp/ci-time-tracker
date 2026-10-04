"""GitHub Actions API ingestion for ci-time-tracker.

Fetches workflow runs and their jobs from the GitHub REST API and maps them to
the BuildLog/StepExecution models. Timing comes from the API's own timestamps,
so analysis does not depend on parsing runner log text.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator, Literal

from ci_time_tracker.models import BuildLog, StepExecution

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_RUN_LIMIT = 50

# Each run needs its own jobs request, and those dominate the wall clock, so
# they are fetched concurrently. Kept modest to stay clear of GitHub's
# secondary rate limits.
DEFAULT_WORKERS = 8

_PER_PAGE = 100
_USER_AGENT = "ci-time-tracker"
_API_VERSION = "2022-11-28"
_TIMEOUT_SECONDS = 30

# GitHub conclusions mapped to StepExecution statuses. A cancelled or skipped
# step never really ran, so it counts as neither success nor failure.
_CONCLUSION_STATUS: dict[str, Literal["success", "failure", "skipped"]] = {
    "success": "success",
    "failure": "failure",
    "timed_out": "failure",
    "startup_failure": "failure",
    "action_required": "failure",
    "cancelled": "skipped",
    "skipped": "skipped",
    "neutral": "skipped",
    "stale": "skipped",
}

# A transport takes (url, headers) and returns (parsed JSON body, response headers).
# Injectable so tests can run against recorded payloads instead of the network.
Transport = Callable[[str, dict[str, str]], tuple[Any, dict[str, str]]]


class GitHubAPIError(Exception):
    """Raised when the GitHub API cannot be queried."""

    def __init__(self, message: str, status: int | None = None):
        self.message = message
        self.status = status
        super().__init__(message)


def resolve_token(explicit: str | None = None) -> str | None:
    """Return the GitHub token to authenticate with.

    Prefers an explicitly passed token, then GITHUB_TOKEN, then GH_TOKEN.
    Requests without a token still work for public repositories, but GitHub
    allows only 60 of them per hour.
    """
    return explicit or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _urllib_transport(url: str, headers: dict[str, str]) -> tuple[Any, dict[str, str]]:
    """Default transport: a plain GET against the GitHub REST API."""
    request = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
            return json.loads(body), dict(response.headers)
    except urllib.error.HTTPError as error:
        raise _http_error(error, url) from error
    except urllib.error.URLError as error:
        raise GitHubAPIError(f"Could not reach {url}: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise GitHubAPIError(f"GitHub returned a non-JSON response for {url}") from error


def _http_error(error: urllib.error.HTTPError, url: str) -> GitHubAPIError:
    """Turn an HTTPError into a GitHubAPIError with actionable wording."""
    remaining = error.headers.get("X-RateLimit-Remaining") if error.headers else None

    if error.code == 403 and remaining == "0":
        return GitHubAPIError(
            "GitHub API rate limit exceeded. Set GITHUB_TOKEN to raise the limit "
            "from 60 to 5000 requests per hour.",
            status=error.code,
        )
    if error.code in (401, 403):
        return GitHubAPIError(
            f"GitHub denied access to {url} (HTTP {error.code}). Check that "
            "GITHUB_TOKEN is set and has the 'actions:read' scope.",
            status=error.code,
        )
    if error.code == 404:
        return GitHubAPIError(
            f"Not found: {url}. Check the repository and workflow names, and "
            "that the token can see a private repository.",
            status=error.code,
        )
    return GitHubAPIError(f"GitHub API request failed (HTTP {error.code}): {url}", status=error.code)


@dataclass
class GitHubClient:
    """Minimal read-only client for the GitHub Actions API.

    Attributes:
        repo: Repository in "owner/name" form
        token: Optional API token; without one only public data is reachable
        api_url: API root, overridable for GitHub Enterprise
        transport: Injectable request function (see Transport)
    """

    repo: str
    token: str | None = None
    api_url: str = DEFAULT_API_URL
    transport: Transport = _urllib_transport

    def __post_init__(self) -> None:
        if self.repo.count("/") != 1 or not all(part.strip() for part in self.repo.split("/")):
            raise GitHubAPIError(
                f"Invalid repository '{self.repo}'. Expected the form 'owner/name'."
            )
        self.api_url = self.api_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": _API_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = {key: value for key, value in (params or {}).items() if value is not None}
        url = f"{self.api_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        payload, _ = self.transport(url, self._headers())
        return payload

    def iter_workflow_runs(
        self,
        workflow: str | None = None,
        branch: str | None = None,
        limit: int = DEFAULT_RUN_LIMIT,
        status: str | None = "completed",
    ) -> Iterator[dict[str, Any]]:
        """Yield up to `limit` workflow runs, newest first.

        Args:
            workflow: Workflow file name (e.g. "ci.yml") or numeric id. When
                omitted, runs of every workflow in the repository are returned.
            branch: Restrict to runs of a single branch
            limit: Maximum number of runs to yield
            status: Run status filter; "completed" skips in-flight runs whose
                timings are not final yet
        """
        if limit <= 0:
            return

        if workflow:
            path = f"/repos/{self.repo}/actions/workflows/{urllib.parse.quote(workflow)}/runs"
        else:
            path = f"/repos/{self.repo}/actions/runs"

        yielded = 0
        page = 1

        while yielded < limit:
            payload = self._get(
                path,
                {
                    "per_page": min(_PER_PAGE, limit - yielded),
                    "page": page,
                    "status": status,
                    "branch": branch,
                },
            )
            runs = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
            if not runs:
                return

            for run in runs:
                yield run
                yielded += 1
                if yielded >= limit:
                    return

            page += 1

    def fetch_run_jobs(self, run_id: int | str, run_attempt: int | None = None) -> list[dict[str, Any]]:
        """Return every job of a workflow run attempt, following pagination.

        Without an attempt, GitHub returns the jobs of the latest attempt, so
        an earlier attempt of a re-run must always be asked for explicitly.
        """
        if run_attempt:
            path = f"/repos/{self.repo}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
        else:
            path = f"/repos/{self.repo}/actions/runs/{run_id}/jobs"

        jobs: list[dict[str, Any]] = []
        page = 1

        while True:
            payload = self._get(path, {"per_page": _PER_PAGE, "page": page})
            batch = payload.get("jobs", []) if isinstance(payload, dict) else []
            jobs.extend(batch)

            if len(batch) < _PER_PAGE:
                return jobs
            page += 1


def parse_api_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp as returned by the GitHub API."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _elapsed_seconds(start: datetime | None, end: datetime | None) -> float | None:
    """Seconds between two timestamps, or None when either is missing."""
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _status_from_conclusion(conclusion: str | None) -> Literal["success", "failure", "skipped"]:
    """Map a GitHub conclusion to a step status."""
    if conclusion is None:
        return "skipped"
    return _CONCLUSION_STATUS.get(conclusion, "skipped")


def _runner_label(job: dict[str, Any]) -> str | None:
    """Best available description of the machine a job ran on."""
    labels = job.get("labels") or []
    if labels:
        return ", ".join(str(label) for label in labels)
    return job.get("runner_name")


def steps_from_job(job: dict[str, Any], run_attempt: int = 1) -> list[StepExecution]:
    """Map a job's steps to StepExecution records.

    Step names are qualified with the job name ("job / step"), the way the
    GitHub UI shows them, so that identically named steps in different jobs
    are not aggregated together.
    """
    job_name = job.get("name") or "unnamed job"
    runner = _runner_label(job)
    executions: list[StepExecution] = []

    for step in job.get("steps") or []:
        step_name = step.get("name") or f"step {step.get('number', len(executions) + 1)}"
        start_time = parse_api_timestamp(step.get("started_at"))
        end_time = parse_api_timestamp(step.get("completed_at"))

        executions.append(StepExecution(
            name=f"{job_name} / {step_name}",
            start_time=start_time,
            end_time=end_time,
            duration_seconds=_elapsed_seconds(start_time, end_time),
            status=_status_from_conclusion(step.get("conclusion")),
            is_retry=run_attempt > 1,
            job_name=job_name,
            runner=runner,
        ))

    return executions


def build_log_from_run(
    run: dict[str, Any],
    jobs: list[dict[str, Any]],
    superseded: bool = False,
) -> BuildLog:
    """Map a workflow run and its jobs to a BuildLog.

    Job-level data that has no place on a step (queue time, runner, result)
    is kept in `metadata["jobs"]` for later analysis.

    Args:
        run: Run payload; for an earlier attempt of a re-run, the run-level
            timestamps describe the latest attempt and must be left out
        jobs: The jobs of that attempt
        superseded: Whether this is an earlier attempt of a re-run
    """
    run_attempt = int(run.get("run_attempt") or 1)

    job_starts = [ts for ts in (parse_api_timestamp(job.get("started_at")) for job in jobs) if ts]
    job_ends = [ts for ts in (parse_api_timestamp(job.get("completed_at")) for job in jobs) if ts]

    started_at = (
        parse_api_timestamp(run.get("run_started_at"))
        or (min(job_starts) if job_starts else None)
        or parse_api_timestamp(run.get("created_at"))
    )
    finished_at = parse_api_timestamp(run.get("updated_at")) or (max(job_ends) if job_ends else None)

    steps: list[StepExecution] = []
    jobs_metadata: list[dict[str, Any]] = []

    for job in jobs:
        steps.extend(steps_from_job(job, run_attempt))

        job_created = parse_api_timestamp(job.get("created_at"))
        job_started = parse_api_timestamp(job.get("started_at"))
        job_completed = parse_api_timestamp(job.get("completed_at"))

        jobs_metadata.append({
            "name": job.get("name"),
            "conclusion": job.get("conclusion"),
            "runner": _runner_label(job),
            "queued_seconds": _elapsed_seconds(job_created, job_started),
            "duration_seconds": _elapsed_seconds(job_started, job_completed),
        })

    return BuildLog(
        build_id=str(run["id"]) if run.get("id") is not None else None,
        steps=steps,
        total_duration=_elapsed_seconds(started_at, finished_at),
        timestamp=started_at,
        head_sha=run.get("head_sha"),
        run_attempt=run_attempt,
        conclusion=run.get("conclusion"),
        metadata={
            "provider": "github",
            "workflow_name": run.get("name"),
            "branch": run.get("head_branch"),
            "event": run.get("event"),
            "url": run.get("html_url"),
            "jobs": jobs_metadata,
            "superseded_attempt": superseded,
        },
    )


def _earlier_attempt(run: dict[str, Any], attempt: int) -> dict[str, Any]:
    """Run payload for an earlier attempt of a re-run.

    The runs listing only describes the latest attempt, so its timestamps and
    conclusion are dropped; build_log_from_run derives them from the jobs.
    An earlier attempt is finished by definition.
    """
    return {
        **run,
        "run_attempt": attempt,
        "run_started_at": None,
        "updated_at": None,
        "conclusion": None,
        "status": "completed",
    }


def fetch_build_logs(
    repo: str,
    workflow: str | None = None,
    branch: str | None = None,
    limit: int = DEFAULT_RUN_LIMIT,
    token: str | None = None,
    cache: Any | None = None,
    api_url: str = DEFAULT_API_URL,
    transport: Transport = _urllib_transport,
    workers: int = DEFAULT_WORKERS,
    include_attempts: bool = True,
) -> list[BuildLog]:
    """Fetch recent workflow runs and return them as BuildLogs.

    Listing runs is one request per 100 runs, but each run's jobs cost a
    request of their own. Those dominate the wall clock, so cached runs are
    served from `cache` (see cache.RunCache) and the rest are fetched
    concurrently.

    The runs listing only shows the latest attempt of a re-run. The earlier
    attempts are fetched too, as BuildLogs of their own: a step that failed
    and then passed on a re-run is the strongest evidence of flakiness.

    Args:
        repo: Repository in "owner/name" form
        workflow: Optional workflow file name (e.g. "ci.yml")
        branch: Optional branch filter
        limit: Maximum number of runs to fetch
        token: API token; falls back to GITHUB_TOKEN / GH_TOKEN
        cache: Optional RunCache for jobs payloads
        api_url: API root, overridable for GitHub Enterprise
        transport: Injectable request function
        workers: Number of concurrent jobs requests
        include_attempts: Also fetch the earlier attempts of re-run runs;
            they do not count towards `limit`

    Returns:
        BuildLogs ordered oldest first, so trends read left to right; the
        attempts of a re-run come in attempt order

    Raises:
        GitHubAPIError: If the API cannot be queried
    """
    client = GitHubClient(
        repo=repo,
        token=resolve_token(token),
        api_url=api_url,
        transport=transport,
    )

    # One entry per (run, attempt), newest first: (payload, attempt, superseded)
    attempts: list[tuple[dict[str, Any], int, bool]] = []

    for run in client.iter_workflow_runs(workflow=workflow, branch=branch, limit=limit):
        if run.get("id") is None:
            continue

        latest = int(run.get("run_attempt") or 1)
        attempts.append((run, latest, False))

        if include_attempts:
            for attempt in range(latest - 1, 0, -1):
                attempts.append((_earlier_attempt(run, attempt), attempt, True))

    # Cache reads and writes stay on this thread: a sqlite connection belongs
    # to the thread that created it.
    jobs_by_attempt: dict[tuple[int, int], list[dict[str, Any]]] = {}
    missing: list[tuple[dict[str, Any], int]] = []

    for run, attempt, _ in attempts:
        cached = cache.get_jobs(repo, run["id"], attempt) if cache else None
        if cached is None:
            missing.append((run, attempt))
        else:
            jobs_by_attempt[(run["id"], attempt)] = cached

    if missing:
        def fetch(entry: tuple[dict[str, Any], int]) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
            run, attempt = entry
            return run, attempt, client.fetch_run_jobs(run["id"], attempt)

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(missing)))) as pool:
            for run, attempt, jobs in pool.map(fetch, missing):
                jobs_by_attempt[(run["id"], attempt)] = jobs
                # Only completed runs are worth caching: an in-flight run's
                # timings still change.
                if cache is not None and run.get("status") == "completed":
                    cache.store_jobs(repo, run["id"], attempt, jobs)

    logs = [
        build_log_from_run(run, jobs_by_attempt.get((run["id"], attempt), []), superseded)
        for run, attempt, superseded in attempts
    ]
    logs.reverse()
    return logs
