"""Evidence-based detection of flaky steps and duration regressions.

Flaky: a step that both failed and passed on the same commit is flaky by
definition, since the code did not change between the two outcomes. A failed
run that passed when re-run is the clearest case of it. A high failure rate
alone is not evidence: a step that fails because commits break it is doing
its job.

Regression: a step whose duration jumped and stayed up. The step's history is
split at the point that best separates "before" from "after", and the jump
must be large in both relative and absolute terms and still hold for the most
recent builds, so that a spike that already recovered is not reported.
"""

from collections import defaultdict
from statistics import median
from typing import Any, Callable

from ci_time_tracker.models import BuildLog

DEFAULT_REGRESSION_THRESHOLD = 0.25
DEFAULT_MIN_SAMPLES = 5
DEFAULT_MIN_INCREASE_SECONDS = 10.0

_EXAMPLE_COMMITS = 3
_SHORT_SHA = 7


def _short(sha: str) -> str:
    return sha[:_SHORT_SHA]


def detect_flaky_by_commit(
    logs: list[BuildLog],
    group: Callable[[str], str] = lambda name: name,
) -> dict[str, dict[str, Any]]:
    """Find steps that both failed and passed on the same commit.

    Outcomes are compared per (workflow, step, commit), using each step's own
    name, so a matrix leg that only fails on one platform is not mistaken for
    flakiness. `group` maps those names to the names being reported (for
    example merged matrix legs); a reported step is flaky when any of its legs
    is.

    Args:
        logs: Build logs; those without a head_sha carry no evidence
        group: Maps a step's own name to its reported name

    Returns:
        Reported step name -> evidence, only for flaky steps:
            flaky_commits: commits on which the step both failed and passed
            observed_commits: commits on which the step ran to an outcome
            rerun_recoveries: failed runs whose step passed on a re-run
            example_commits: a few short SHAs to look at
    """
    outcomes: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    attempts: dict[tuple[str, str, str], list[tuple[int, str]]] = defaultdict(list)

    for log in logs:
        if not log.head_sha:
            continue

        workflow = str(log.metadata.get("workflow_name") or "")

        for step in log.steps:
            if step.status not in ("success", "failure"):
                continue

            outcomes[(workflow, step.name, log.head_sha)].add(step.status)
            if log.build_id:
                attempts[(workflow, step.name, log.build_id)].append((log.run_attempt, step.status))

    observed: dict[str, set[str]] = defaultdict(set)
    flipped: dict[str, set[str]] = defaultdict(set)

    for (_, name, sha), statuses in outcomes.items():
        reported = group(name)
        observed[reported].add(sha)
        if statuses == {"success", "failure"}:
            flipped[reported].add(sha)

    recoveries: dict[str, int] = defaultdict(int)

    for (_, name, _), entries in attempts.items():
        failed_attempts = [attempt for attempt, status in entries if status == "failure"]
        if not failed_attempts:
            continue
        first_failure = min(failed_attempts)
        if any(status == "success" and attempt > first_failure for attempt, status in entries):
            recoveries[group(name)] += 1

    return {
        reported: {
            "method": "same-commit",
            "flaky_commits": len(shas),
            "observed_commits": len(observed[reported]),
            "rerun_recoveries": recoveries.get(reported, 0),
            "example_commits": [_short(sha) for sha in sorted(shas)[:_EXAMPLE_COMMITS]],
        }
        for reported, shas in flipped.items()
    }


def _segment(values: list[float]) -> tuple[float, float]:
    """Median of a segment and its total absolute deviation from it."""
    center = median(values)
    return center, sum(abs(value - center) for value in values)


def detect_regression(
    samples: list[tuple[float, BuildLog]],
    threshold: float = DEFAULT_REGRESSION_THRESHOLD,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_increase_seconds: float = DEFAULT_MIN_INCREASE_SECONDS,
) -> dict[str, Any] | None:
    """Detect a lasting increase in a step's duration.

    Finds the single split that best divides the history into two stable
    segments (least total absolute deviation from each segment's median),
    then reports a regression only when the later segment is slower by at
    least `threshold` and `min_increase_seconds`, and the most recent
    `min_samples` builds are still that slow.

    Args:
        samples: (duration, build) pairs in chronological order, one per build
        threshold: Minimum relative increase, e.g. 0.25 for +25%
        min_samples: Minimum builds on each side of the split
        min_increase_seconds: Minimum absolute increase, so that 1s -> 2s
            steps are not reported

    Returns:
        Regression details, or None when there is none or too little history
    """
    durations = [duration for duration, _ in samples]
    count = len(durations)

    if count < 2 * min_samples:
        return None

    best: tuple[float, int, float, float] | None = None

    for split in range(min_samples, count - min_samples + 1):
        before_median, before_cost = _segment(durations[:split])
        after_median, after_cost = _segment(durations[split:])
        cost = before_cost + after_cost

        if best is None or cost < best[0]:
            best = (cost, split, before_median, after_median)

    if best is None:
        return None

    _, split, baseline, current = best
    recent = median(durations[-min_samples:])
    required = baseline * (1 + threshold)

    if current < required or recent < required:
        return None
    if current - baseline < min_increase_seconds or recent - baseline < min_increase_seconds:
        return None

    started = samples[split][1]

    return {
        "baseline_median": baseline,
        "current_median": current,
        "increase_seconds": current - baseline,
        "increase_pct": (current - baseline) / baseline if baseline > 0 else None,
        "baseline_builds": split,
        "current_builds": count - split,
        "started_at": {
            "build_id": started.build_id,
            "head_sha": _short(started.head_sha) if started.head_sha else None,
            "timestamp": started.timestamp.isoformat() if started.timestamp else None,
        },
    }
