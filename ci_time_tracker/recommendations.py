"""Recommendations that cross a workflow's configuration with its run history.

Neither source is enough alone. The configuration says a job installs
dependencies without a cache, but not whether that costs anything; the history
says a step is slow, but not why. A recommendation is made only when the
configuration shows the cause and the history shows it matters.

Each recommendation carries the evidence, a concrete change, and what is at
stake: the time (and cost) the affected steps or jobs consumed over the
analyzed runs. That is an upper bound on the saving, not a promise of it.
"""

import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import timedelta
from itertools import pairwise
from statistics import median
from typing import Any

import yaml

from ci_time_tracker.analyzer import normalize_matrix_name
from ci_time_tracker.models import BuildLog
from ci_time_tracker.pricing import PricingTable, attributed_cost, job_cost
from ci_time_tracker.reporter import format_cost, format_duration

# Below this much time at stake over the analyzed runs, a change is not worth
# suggesting: a list of trivial recommendations buries the ones that matter
MIN_TIME_AT_STAKE_SECONDS = 600.0

# Dependency installs: median duration worth caching, and runs needed to judge
MIN_INSTALL_SECONDS = 20.0
MIN_INSTALL_RUNS = 5

# Obsolete pull request runs: how many, and how much compute, to be worth it
MIN_SUPERSEDED_RUNS = 2
MIN_SUPERSEDED_SECONDS = 60.0

# Matrix legs: builds needed to trust "never failed alone", and the compute
# below which the legs are not worth discussing
MIN_LEG_BUILDS = 10
MIN_LEG_SECONDS = 300.0

_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")

# Install commands, by ecosystem
_INSTALL_COMMANDS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\buv\s+(pip\s+install|sync)\b"), "uv"),
    (re.compile(r"\bpoetry\s+install\b"), "poetry"),
    (re.compile(r"\bpipenv\s+(install|sync)\b"), "pipenv"),
    (re.compile(r"\bpip3?\s+install\b"), "pip"),
    (re.compile(r"\bnpm\s+(ci|install|i)\b"), "npm"),
    (re.compile(r"\bpnpm\s+(install|i)\b"), "pnpm"),
    (re.compile(r"\byarn(\s+install)?\s*$", re.MULTILINE), "yarn"),
    (re.compile(r"\bbundle\s+install\b"), "bundler"),
    (re.compile(r"\bcomposer\s+install\b"), "composer"),
    (re.compile(r"\bgo\s+mod\s+download\b"), "go"),
]

_CACHE_SUGGESTIONS = {
    "pip": "Set `cache: pip` on actions/setup-python.",
    "poetry": "Set `cache: poetry` on actions/setup-python.",
    "pipenv": "Set `cache: pipenv` on actions/setup-python.",
    "uv": "Use astral-sh/setup-uv with `enable-cache: true`.",
    "npm": "Set `cache: npm` on actions/setup-node.",
    "pnpm": "Set `cache: pnpm` on actions/setup-node.",
    "yarn": "Set `cache: yarn` on actions/setup-node.",
    "bundler": "Set `bundler-cache: true` on ruby/setup-ruby.",
    "composer": "Cache Composer's cache directory with actions/cache.",
    "go": "Use actions/setup-go, which caches modules by default.",
}

_CONCURRENCY_SNIPPET = (
    "Add to the workflow:\n"
    "concurrency:\n"
    "  group: ${{ github.workflow }}-${{ github.ref }}\n"
    "  cancel-in-progress: ${{ github.event_name == 'pull_request' }}"
)


@dataclass
class Recommendation:
    """One suggested change, with its evidence and what is at stake."""

    kind: str
    title: str
    workflow: str | None
    job: str | None
    evidence: str
    suggestion: str
    time_at_stake_seconds: float
    cost_at_stake_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_workflow(text: str) -> dict[str, Any] | None:
    """Parse a workflow file, or None if it is not a workflow mapping."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None

    # YAML 1.1 reads the bare key `on` as the boolean True
    if True in data and "on" not in data:
        data["on"] = data.pop(True)
    return data


def _jobs(workflow: dict[str, Any]) -> dict[str, dict[str, Any]]:
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return {}
    return {str(job_id): job for job_id, job in jobs.items() if isinstance(job, dict)}


def _job_display_name(job_id: str, job: dict[str, Any]) -> str:
    """The job's name as runs report it, without matrix values."""
    name = job.get("name")
    if not isinstance(name, str) or not name.strip():
        return job_id
    return normalize_matrix_name(_EXPRESSION.sub("", name)).strip() or job_id


def _step_display_name(step: dict[str, Any]) -> str | None:
    """The step's name as runs report it, when it can be predicted."""
    name = step.get("name")
    if isinstance(name, str) and name.strip() and "${{" not in name:
        return name.strip()
    run = step.get("run")
    if isinstance(run, str) and run.strip():
        return f"Run {run.strip().splitlines()[0]}"
    return None


def _job_caches(steps: list[Any]) -> bool:
    """Whether a job restores any cache.

    Errs towards True: an unrecognized caching setup must not draw a
    recommendation to add a cache that already exists.
    """
    for step in steps:
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses") or "").lower()
        if not uses:
            continue
        action = uses.split("@")[0]
        inputs = step.get("with") if isinstance(step.get("with"), dict) else {}

        if "cache" in action:
            return True
        if action in {"actions/setup-python", "actions/setup-node", "actions/setup-java", "actions/setup-dotnet"}:
            if inputs.get("cache"):
                return True
        if action == "actions/setup-go" and str(inputs.get("cache", True)).lower() != "false":
            return True
        if action == "ruby/setup-ruby" and str(inputs.get("bundler-cache", "")).lower() == "true":
            return True
        if action == "astral-sh/setup-uv":
            return True
    return False


def _install_ecosystem(run: str) -> str | None:
    for pattern, ecosystem in _INSTALL_COMMANDS:
        if pattern.search(run):
            return ecosystem
    return None


def _builds(logs: list[BuildLog]) -> list[BuildLog]:
    return [log for log in logs if not log.metadata.get("superseded_attempt")]


def _step_executions(
    logs: list[BuildLog],
) -> dict[tuple[str, str, str], list[tuple[float, str | None]]]:
    """(workflow path, job, step) -> [(duration, runner)], matrix legs merged."""
    index: dict[tuple[str, str, str], list[tuple[float, str | None]]] = defaultdict(list)

    for log in _builds(logs):
        path = log.metadata.get("workflow_path")
        if not path:
            continue
        for step in log.steps:
            if step.duration_seconds is None or not step.job_name:
                continue
            prefix = f"{step.job_name} / "
            step_name = step.name[len(prefix):] if step.name.startswith(prefix) else step.name
            job = normalize_matrix_name(step.job_name)
            index[(path, job, step_name)].append((step.duration_seconds, step.runner))

    return index


def recommend_dependency_caches(
    logs: list[BuildLog],
    workflows: dict[str, dict[str, Any]],
    pricing: PricingTable,
) -> list[Recommendation]:
    """Jobs that install dependencies without any cache, where it is slow."""
    executions = _step_executions(logs)
    recommendations: list[Recommendation] = []

    for path, workflow in workflows.items():
        for job_id, job in _jobs(workflow).items():
            steps = job.get("steps")
            if not isinstance(steps, list) or _job_caches(steps):
                continue

            job_name = _job_display_name(job_id, job)

            for step in steps:
                if not isinstance(step, dict) or not isinstance(step.get("run"), str):
                    continue
                ecosystem = _install_ecosystem(step["run"])
                step_name = _step_display_name(step)
                if ecosystem is None or step_name is None:
                    continue

                runs = executions.get((path, job_name, step_name), [])
                durations = [duration for duration, _ in runs]
                if len(durations) < MIN_INSTALL_RUNS or median(durations) < MIN_INSTALL_SECONDS:
                    continue

                costs = [attributed_cost(duration, pricing.price_for(runner)) for duration, runner in runs]
                known = [cost for cost in costs if cost is not None]

                recommendations.append(Recommendation(
                    kind="dependency-cache",
                    title=f"Cache {ecosystem} dependencies in `{job_name}`",
                    workflow=path,
                    job=job_name,
                    evidence=(
                        f"`{step_name}` takes a median of {format_duration(median(durations))} per run "
                        f"({format_duration(sum(durations))} over {len(durations)} runs), and the job "
                        "restores no cache."
                    ),
                    suggestion=_CACHE_SUGGESTIONS[ecosystem],
                    time_at_stake_seconds=sum(durations),
                    cost_at_stake_usd=sum(known) if known else None,
                ))

    return recommendations


def _cancels_in_progress(workflow: dict[str, Any]) -> bool:
    """Whether the workflow, or any of its jobs, cancels superseded runs."""
    blocks = [workflow.get("concurrency")] + [job.get("concurrency") for job in _jobs(workflow).values()]
    for block in blocks:
        if isinstance(block, dict) and str(block.get("cancel-in-progress", False)).lower() not in ("false", ""):
            return True
    return False


def _compute(log: BuildLog, pricing: PricingTable) -> tuple[float, float | None]:
    """A build's compute time and job cost, from its jobs."""
    jobs = log.metadata.get("jobs") or []
    seconds = sum(job.get("duration_seconds") or 0.0 for job in jobs)
    costs = [job_cost(job.get("duration_seconds"), pricing.price_for(job.get("runner"))) for job in jobs]
    known = [cost for cost in costs if cost is not None]
    return seconds, (sum(known) if known else None)


def recommend_cancelling_superseded_runs(
    logs: list[BuildLog],
    workflows: dict[str, dict[str, Any]],
    pricing: PricingTable,
) -> list[Recommendation]:
    """Pull request runs that kept running after a newer push replaced them."""
    branches: dict[tuple[str, str | None, str], list[BuildLog]] = defaultdict(list)

    for log in _builds(logs):
        path = log.metadata.get("workflow_path")
        branch = log.metadata.get("branch")
        if (
            log.metadata.get("event") != "pull_request"
            or not path
            or not branch
            or log.timestamp is None
            or not log.total_duration
        ):
            continue
        branches[(path, log.metadata.get("head_repository"), branch)].append(log)

    wasted: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0])  # count, seconds, cost

    for (path, _, _), runs in branches.items():
        runs.sort(key=lambda log: log.timestamp)
        for older, newer in pairwise(runs):
            finished = older.timestamp + timedelta(seconds=older.total_duration)
            overlap = (finished - newer.timestamp).total_seconds()
            if overlap <= 0:
                continue

            # The part of the older run spent after it became obsolete
            fraction = min(1.0, overlap / older.total_duration)
            seconds, cost = _compute(older, pricing)
            totals = wasted[path]
            totals[0] += 1
            totals[1] += seconds * fraction
            totals[2] += (cost or 0.0) * fraction

    recommendations: list[Recommendation] = []

    for path, (count, seconds, cost) in wasted.items():
        workflow = workflows.get(path)
        if workflow is None or _cancels_in_progress(workflow):
            continue
        if count < MIN_SUPERSEDED_RUNS or seconds < MIN_SUPERSEDED_SECONDS:
            continue

        recommendations.append(Recommendation(
            kind="cancel-superseded-runs",
            title=f"Cancel superseded pull request runs of {workflow.get('name') or path}",
            workflow=path,
            job=None,
            evidence=(
                f"{int(count)} pull request runs were still running when a newer push to the same "
                f"branch started another run. They spent {format_duration(seconds)} of compute "
                "after becoming obsolete."
            ),
            suggestion=_CONCURRENCY_SNIPPET,
            time_at_stake_seconds=seconds,
            cost_at_stake_usd=cost or None,
        ))

    return recommendations


def _platform(runner: str | None) -> str | None:
    labels = (runner or "").lower()
    if "macos" in labels:
        return "macOS"
    if "windows" in labels:
        return "Windows"
    if "ubuntu" in labels or "linux" in labels:
        return "Linux"
    return None


def recommend_expensive_matrix_legs(
    logs: list[BuildLog],
    pricing: PricingTable,
) -> list[Recommendation]:
    """Windows or macOS matrix legs that never failed when Linux passed."""
    # (workflow path, job, platform) -> builds, failures alone, seconds, cost
    legs: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0, 0, 0.0, 0.0])
    job_seconds: dict[tuple[str, str], float] = defaultdict(float)

    for log in _builds(logs):
        path = log.metadata.get("workflow_path")
        if not path:
            continue

        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for job in log.metadata.get("jobs") or []:
            if job.get("name"):
                groups[normalize_matrix_name(job["name"])].append(job)

        for group, jobs in groups.items():
            by_platform: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for job in jobs:
                platform = _platform(job.get("runner"))
                if platform:
                    by_platform[platform].append(job)
                job_seconds[(path, group)] += job.get("duration_seconds") or 0.0

            linux = by_platform.get("Linux")
            if not linux:
                continue
            linux_passed = all(job.get("conclusion") == "success" for job in linux)

            for platform in ("Windows", "macOS"):
                platform_jobs = by_platform.get(platform)
                if not platform_jobs:
                    continue
                stats = legs[(path, group, platform)]
                stats[0] += 1
                if linux_passed and any(job.get("conclusion") == "failure" for job in platform_jobs):
                    stats[1] += 1
                for job in platform_jobs:
                    stats[2] += job.get("duration_seconds") or 0.0
                    stats[3] += job_cost(job.get("duration_seconds"), pricing.price_for(job.get("runner"))) or 0.0

    # One recommendation per workflow and platform: tokio's CI drew eighteen
    # near-identical ones, one per job, when the finding is a single one
    qualifying: dict[tuple[str, str], list[tuple[str, float, float, float]]] = defaultdict(list)
    for (path, group, platform), (builds, alone, seconds, cost) in legs.items():
        if builds >= MIN_LEG_BUILDS and not alone and seconds > 0:
            qualifying[(path, platform)].append((group, builds, seconds, cost))

    recommendations: list[Recommendation] = []

    for (path, platform), jobs in qualifying.items():
        jobs.sort(key=lambda job: job[2], reverse=True)
        seconds = sum(job[2] for job in jobs)
        cost = sum(job[3] for job in jobs)
        if seconds < MIN_LEG_SECONDS:
            continue

        builds = int(min(job[1] for job in jobs))
        total = sum(job_seconds[(path, job[0])] for job in jobs)
        cost_str = f" and {format_cost(cost)}" if cost else ""

        if len(jobs) == 1:
            group = jobs[0][0]
            title = f"Run the {platform} legs of `{group}` less often"
            subject = f"the {platform} legs"
            share = f", {seconds / total * 100:.0f}% of the job's compute" if total else ""
        else:
            group = None
            names = ", ".join(f"`{job[0]}`" for job in jobs[:3])
            more = f" and {len(jobs) - 3} more" if len(jobs) > 3 else ""
            title = f"Run the {platform} legs of {len(jobs)} jobs less often"
            subject = f"the {platform} legs of {names}{more}"
            share = f", {seconds / total * 100:.0f}% of those jobs' compute" if total else ""

        recommendations.append(Recommendation(
            kind="expensive-matrix-legs",
            title=title,
            workflow=path,
            job=group,
            evidence=(
                f"In {builds} builds, {subject} never failed when the Linux legs "
                f"passed. They used {format_duration(seconds)}{cost_str}{share}."
            ),
            suggestion=(
                f"Run the {platform} legs only on pushes to the default branch or on a schedule, "
                "and keep Linux on pull requests. A platform-specific failure then shows up "
                "after merging rather than before."
            ),
            time_at_stake_seconds=seconds,
            cost_at_stake_usd=cost or None,
        ))

    return recommendations


def recommend(
    logs: list[BuildLog],
    workflows: dict[str, dict[str, Any]],
    pricing: PricingTable | None = None,
) -> list[Recommendation]:
    """All recommendations, the most time at stake first.

    Args:
        logs: Builds from the API, with job data and workflow paths
        workflows: Parsed workflow files by path; rules that need a
            workflow's configuration skip workflows missing from it
        pricing: Runner pricing; defaults to the built-in rates
    """
    pricing = pricing or PricingTable()

    recommendations = (
        recommend_dependency_caches(logs, workflows, pricing)
        + recommend_cancelling_superseded_runs(logs, workflows, pricing)
        + recommend_expensive_matrix_legs(logs, pricing)
    )
    recommendations = [
        item for item in recommendations if item.time_at_stake_seconds >= MIN_TIME_AT_STAKE_SECONDS
    ]
    recommendations.sort(key=lambda item: item.time_at_stake_seconds, reverse=True)
    return recommendations
