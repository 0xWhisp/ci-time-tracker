"""GitHub Actions API ingestion for ci-time-tracker.

Fetches workflow runs and their jobs from the GitHub REST API and maps them to
the BuildLog/StepExecution models. Timing comes from the API's own timestamps,
so analysis does not depend on parsing runner log text.
"""

import base64
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from ci_time_tracker.models import BuildLog, StepExecution

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_RUN_LIMIT = 50

# Each run needs its own jobs request, and those dominate the wall clock, so
# they are fetched concurrently. Kept modest to stay clear of GitHub's
# secondary rate limits.
DEFAULT_WORKERS = 8

_PER_PAGE = 100

# GitHub takes longer to build a bigger page of runs, so a limit is split into
# smaller pages, one per worker, down to this size
_MIN_RUNS_PAGE = 25

# Runs that never ran are skipped and made up from later pages, but the
# listing stops after this many times the pages the limit needed
_MAX_LISTING_FACTOR = 3

# Run conclusions for runs that never executed: waiting for a maintainer's
# approval (pull requests from forks), skipped by the workflow's conditions,
# or superseded while queued. They have no timings to analyze.
_NOT_RUN_CONCLUSIONS = frozenset({"action_required", "skipped", "stale"})

# A run with many jobs, each with many steps, can make GitHub time out
# building a 100-job page (502/504); such runs are paged in smaller pieces
_SMALL_JOBS_PAGE = 30
_TIMEOUT_STATUSES = frozenset({502, 504})
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

# A sender takes (method, url, headers, JSON body) and returns the parsed JSON
# response. Used for the few writes (PR comments); injectable like Transport.
Sender = Callable[[str, str, dict[str, str], dict[str, Any] | None], Any]

# Marks the comment ci-time-tracker owns on a pull request, so a later run
# updates it instead of adding another
COMMENT_MARKER = "<!-- ci-time-tracker:pr-impact -->"


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


# Reads are retried on errors GitHub reports as transient: one 502 among
# hundreds of jobs requests should not lose the whole analysis. Writes are not
# retried, since retrying a POST whose response was lost could comment twice.
_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1.0
_MAX_RETRY_AFTER_SECONDS = 60.0
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})

# Injectable so tests do not wait
_sleep = time.sleep


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float | None:
    """Seconds to wait before retrying, or None when the error is not transient."""
    retry_after = error.headers.get("Retry-After") if error.headers else None

    if error.code in (403, 429) and retry_after:
        # Secondary rate limit: GitHub says when to come back
        try:
            delay = float(retry_after)
        except ValueError:
            return None
        return delay if delay <= _MAX_RETRY_AFTER_SECONDS else None

    if error.code in _TRANSIENT_STATUSES:
        return _RETRY_BACKOFF_SECONDS * 2 ** attempt

    return None


def _urllib_transport(url: str, headers: dict[str, str]) -> tuple[Any, dict[str, str]]:
    """Default transport: a GET against the GitHub REST API.

    Retried on server errors, network errors and secondary rate limits.
    """
    request = urllib.request.Request(url, headers=headers)

    for attempt in range(_ATTEMPTS):
        last_attempt = attempt == _ATTEMPTS - 1
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8")
                return json.loads(body), dict(response.headers)
        except urllib.error.HTTPError as error:
            delay = None if last_attempt else _retry_delay(error, attempt)
            if delay is None:
                raise _http_error(error, url) from error
            _sleep(delay)
        except (urllib.error.URLError, TimeoutError) as error:
            if last_attempt:
                reason = getattr(error, "reason", error)
                raise GitHubAPIError(f"Could not reach {url}: {reason}") from error
            _sleep(_RETRY_BACKOFF_SECONDS * 2 ** attempt)
        except json.JSONDecodeError as error:
            raise GitHubAPIError(f"GitHub returned a non-JSON response for {url}") from error

    raise AssertionError("unreachable: the last attempt returns or raises")


def _urllib_send(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
) -> Any:
    """Default sender: a JSON write request against the GitHub REST API."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={**headers, "Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
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
            "GITHUB_TOKEN is set and has the 'actions:read' scope (and "
            "'pull-requests: write' to post comments).",
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
    """Minimal client for the GitHub Actions API.

    Reads runs and jobs; the only write is the pull request comment.

    Attributes:
        repo: Repository in "owner/name" form
        token: Optional API token; without one only public data is reachable
        api_url: API root, overridable for GitHub Enterprise
        transport: Injectable read function (see Transport)
        sender: Injectable write function (see Sender)
    """

    repo: str
    token: str | None = None
    api_url: str = DEFAULT_API_URL
    transport: Transport = _urllib_transport
    sender: Sender = _urllib_send

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

    def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.sender(method, f"{self.api_url}{path}", self._headers(), body)

    def fetch_run(self, run_id: int | str) -> dict[str, Any]:
        """Return one workflow run's payload (its latest attempt)."""
        return self._get(f"/repos/{self.repo}/actions/runs/{run_id}")

    def fetch_workflow_file(self, path: str, ref: str | None = None) -> str | None:
        """Return a workflow file's YAML, or None when it does not exist.

        Workflows GitHub generates itself (Dependabot, default CodeQL setup)
        have no file and also return None.
        """
        try:
            payload = self._get(f"/repos/{self.repo}/contents/{urllib.parse.quote(path)}", {"ref": ref})
        except GitHubAPIError as error:
            if error.status == 404:
                return None
            raise

        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            return None
        return base64.b64decode(payload.get("content") or "").decode("utf-8", errors="replace")

    def fetch_default_branch(self) -> str:
        """Return the repository's default branch."""
        return self._get(f"/repos/{self.repo}")["default_branch"]

    def find_pull_request(self, sha: str) -> int | None:
        """Return the number of the open pull request whose head is `sha`.

        The workflow_run event leaves its pull request list empty for pull
        requests from forks, so the commit is looked up instead.
        """
        pulls = self._get(f"/repos/{self.repo}/commits/{sha}/pulls")
        if not isinstance(pulls, list):
            return None

        candidates = [pull for pull in pulls if pull.get("state") == "open"] or pulls
        for pull in candidates:
            if (pull.get("head") or {}).get("sha") == sha:
                return pull["number"]
        return candidates[0]["number"] if candidates else None

    def upsert_comment(self, number: int, body: str, marker: str = COMMENT_MARKER) -> tuple[str, str | None]:
        """Create or update the pull request comment carrying `marker`.

        Returns:
            ("created" or "updated", the comment's URL)
        """
        page = 1
        while True:
            comments = self._get(
                f"/repos/{self.repo}/issues/{number}/comments",
                {"per_page": _PER_PAGE, "page": page},
            )
            comments = comments if isinstance(comments, list) else []

            for comment in comments:
                if marker in (comment.get("body") or ""):
                    updated = self._send(
                        "PATCH", f"/repos/{self.repo}/issues/comments/{comment['id']}", {"body": body}
                    )
                    return "updated", (updated or {}).get("html_url")

            if len(comments) < _PER_PAGE:
                break
            page += 1

        created = self._send("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})
        return "created", (created or {}).get("html_url")

    def iter_workflow_runs(
        self,
        workflow: str | None = None,
        branch: str | None = None,
        limit: int = DEFAULT_RUN_LIMIT,
        status: str | None = "completed",
        include: Callable[[dict[str, Any]], bool] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield up to `limit` workflow runs, newest first.

        Args:
            workflow: Workflow file name (e.g. "ci.yml") or numeric id. When
                omitted, runs of every workflow in the repository are returned.
            branch: Restrict to runs of a single branch
            limit: Maximum number of runs to yield
            status: Run status filter; "completed" skips in-flight runs whose
                timings are not final yet
            include: Optional filter; runs it rejects are not yielded and do
                not count towards `limit`, so later pages are read instead
        """
        if limit <= 0:
            return

        if workflow:
            path = f"/repos/{self.repo}/actions/workflows/{urllib.parse.quote(workflow)}/runs"
        else:
            path = f"/repos/{self.repo}/actions/runs"

        # The pages a limit needs are known up front, so they are requested
        # together: listing is the slowest part of a cached analysis, at a few
        # seconds a page. The pull requests attached to each run are left out
        # of the payload; nothing reads them (a run's pull request is looked up
        # by its commit). Eight pages of 25 come back in about half the time
        # of two pages of 100.
        # With a filter, a margin is listed up front: a second round of pages
        # for the runs it dropped would add a few seconds.
        target = limit + limit // 4 if include is not None else limit
        per_page = min(_PER_PAGE, max(_MIN_RUNS_PAGE, -(-target // DEFAULT_WORKERS)), target)
        pages = -(-target // per_page)
        # Filtered out runs are made up from later pages, within reason
        last_page = pages * _MAX_LISTING_FACTOR

        def fetch(page: int) -> list[dict[str, Any]]:
            payload = self._get(
                path,
                {
                    "per_page": per_page,
                    "page": page,
                    "status": status,
                    "branch": branch,
                    "exclude_pull_requests": "true",
                },
            )
            return payload.get("workflow_runs", []) if isinstance(payload, dict) else []

        seen: set[Any] = set()
        yielded = listed = 0
        first = 1

        while first <= last_page:
            with ThreadPoolExecutor(max_workers=min(DEFAULT_WORKERS, pages)) as pool:
                batches = list(pool.map(fetch, range(first, first + pages)))

            for runs in batches:
                for run in runs:
                    # A run created while the pages were fetched shifts the
                    # others by one, which can show a run on two pages
                    if run.get("id") in seen:
                        continue
                    seen.add(run.get("id"))
                    listed += 1
                    if include is not None and not include(run):
                        continue
                    yield run
                    yielded += 1
                    if yielded >= limit:
                        return

                # A short page is the last one; later pages can only be empty
                if len(runs) < per_page:
                    return

            # Enough further pages for the runs still missing, at the share
            # of listed runs the filter has kept so far
            first += pages
            kept = max(yielded / listed, 0.1)
            needed = math.ceil((limit - yielded) / (per_page * kept))
            pages = max(1, min(DEFAULT_WORKERS, last_page - first + 1, needed))

    def fetch_run_jobs(self, run_id: int | str, run_attempt: int | None = None) -> list[dict[str, Any]]:
        """Return every job of a workflow run attempt, following pagination.

        Without an attempt, GitHub returns the jobs of the latest attempt, so
        an earlier attempt of a re-run must always be asked for explicitly.
        When GitHub times out building a page of a very large run, the jobs
        are fetched again in smaller pages.
        """
        if run_attempt:
            path = f"/repos/{self.repo}/actions/runs/{run_id}/attempts/{run_attempt}/jobs"
        else:
            path = f"/repos/{self.repo}/actions/runs/{run_id}/jobs"

        try:
            return self._fetch_jobs(path, _PER_PAGE)
        except GitHubAPIError as error:
            if error.status not in _TIMEOUT_STATUSES:
                raise
            return self._fetch_jobs(path, _SMALL_JOBS_PAGE)

    def _fetch_jobs(self, path: str, per_page: int) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        page = 1

        while True:
            payload = self._get(path, {"per_page": per_page, "page": page})
            batch = payload.get("jobs", []) if isinstance(payload, dict) else []
            jobs.extend(batch)

            if len(batch) < per_page:
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
            # Where the workflow lives (".github/workflows/ci.yml"), and the
            # repository the commit came from, which differs for forks
            "workflow_path": run.get("path"),
            "head_repository": (run.get("head_repository") or {}).get("full_name"),
            # Public repositories pay nothing for standard runners
            "repository_private": (run.get("repository") or {}).get("private"),
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


def _ran(run: dict[str, Any]) -> bool:
    """Whether a run executed at all, so that it has timings to analyze."""
    return run.get("conclusion") not in _NOT_RUN_CONCLUSIONS


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
    skipped: list[str] | None = None,
) -> list[BuildLog]:
    """Fetch recent workflow runs and return them as BuildLogs.

    Listing runs is one request per 100 runs, but each run's jobs cost a
    request of their own. Those dominate the wall clock, so cached runs are
    served from `cache` (see cache.RunCache) and the rest are fetched
    concurrently.

    Runs that never executed (awaiting approval, skipped, stale) are left
    out and do not count towards `limit`.

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
        skipped: When given, receives the ids of runs left out because
            GitHub kept failing to return their jobs (a server error)
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

    for run in client.iter_workflow_runs(workflow=workflow, branch=branch, limit=limit, include=_ran):
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
        def fetch(
            entry: tuple[dict[str, Any], int],
        ) -> tuple[dict[str, Any], int, list[dict[str, Any]] | None]:
            run, attempt = entry
            try:
                return run, attempt, client.fetch_run_jobs(run["id"], attempt)
            except GitHubAPIError as error:
                # One run GitHub cannot serve should not lose the whole
                # analysis; access and rate-limit errors still stop it
                if error.status is not None and error.status >= 500:
                    return run, attempt, None
                raise

        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(missing)))) as pool:
            for run, attempt, jobs in pool.map(fetch, missing):
                if jobs is None:
                    if skipped is not None:
                        skipped.append(f"{run['id']}" + (f" (attempt {attempt})" if attempt > 1 else ""))
                    continue
                jobs_by_attempt[(run["id"], attempt)] = jobs
                # Only completed runs are worth caching: an in-flight run's
                # timings still change.
                if cache is not None and run.get("status") == "completed":
                    cache.store_jobs(repo, run["id"], attempt, jobs)

    logs = [
        build_log_from_run(run, jobs_by_attempt[(run["id"], attempt)], superseded)
        for run, attempt, superseded in attempts
        if (run["id"], attempt) in jobs_by_attempt
    ]
    logs.reverse()
    return logs


def fetch_impact_inputs(
    repo: str,
    run_id: int | str,
    baseline_branch: str | None = None,
    baseline_runs: int = 20,
    token: str | None = None,
    cache: Any | None = None,
    api_url: str = DEFAULT_API_URL,
    transport: Transport = _urllib_transport,
) -> tuple[dict[str, Any], list[BuildLog], list[BuildLog], str]:
    """Fetch one run and the baseline to compare it with.

    The baseline is the latest completed runs of the same workflow on
    `baseline_branch` (the repository's default branch when omitted), the run
    itself excluded.

    Returns:
        (run payload, the run's attempts oldest first, baseline logs, the
        baseline branch)

    Raises:
        GitHubAPIError: If the API cannot be queried
    """
    client = GitHubClient(repo=repo, token=resolve_token(token), api_url=api_url, transport=transport)

    run = client.fetch_run(run_id)
    branch = baseline_branch or client.fetch_default_branch()

    latest = int(run.get("run_attempt") or 1)
    run_logs = [
        build_log_from_run(
            run if attempt == latest else _earlier_attempt(run, attempt),
            client.fetch_run_jobs(run["id"], attempt),
            superseded=attempt != latest,
        )
        for attempt in range(1, latest + 1)
    ]

    baseline = [
        log
        for log in fetch_build_logs(
            repo,
            workflow=str(run["workflow_id"]) if run.get("workflow_id") else None,
            branch=branch,
            limit=baseline_runs,
            token=token,
            cache=cache,
            api_url=api_url,
            transport=transport,
        )
        if log.build_id != str(run["id"])
    ]

    return run, run_logs, baseline, branch
