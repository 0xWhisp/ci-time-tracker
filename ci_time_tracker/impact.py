"""How one run compares with its baseline: the pull request view.

The history analysis answers "where does our CI time go". A pull request asks
something narrower: did this change make CI slower or more expensive, and is
a failure real or a known flaky step? Each step of the run is compared with
its median over the baseline (recent runs of the same workflow on the default
branch), and failures are checked against same-commit flaky evidence.
"""

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from statistics import median
from typing import Any

from ci_time_tracker.analyzer import aggregate_job_metrics, normalize_matrix_name
from ci_time_tracker.detection import (
    DEFAULT_MIN_INCREASE_SECONDS,
    DEFAULT_REGRESSION_THRESHOLD,
    detect_flaky_by_commit,
)
from ci_time_tracker.github_api import COMMENT_MARKER
from ci_time_tracker.models import BuildLog
from ci_time_tracker.pricing import PricingTable
from ci_time_tracker.reporter import format_cost, format_duration

# Fewest baseline builds a step needs before its change is judged
DEFAULT_MIN_BASELINE_BUILDS = 3


@dataclass
class StepChange:
    """A step's duration in this run against its baseline median."""

    name: str
    baseline_median: float
    current: float
    delta_seconds: float
    delta_pct: float | None
    significant: bool


@dataclass
class FailedStep:
    """A step that failed in this run, and whether it is known to be flaky."""

    name: str
    known_flaky: bool
    evidence: dict[str, Any] | None = None


@dataclass
class RunImpact:
    """Everything the pull request comment says about one run."""

    run_id: str | None
    run_url: str | None
    workflow_name: str | None
    head_sha: str | None
    baseline_branch: str
    baseline_builds: int
    duration: float | None
    baseline_duration: float | None
    compute_seconds: float
    baseline_compute_seconds: float | None
    cost_usd: float | None
    baseline_cost_usd: float | None
    threshold: float
    min_seconds: float
    changes: list[StepChange] = field(default_factory=list)
    new_steps: list[str] = field(default_factory=list)
    failures: list[FailedStep] = field(default_factory=list)
    recovered_on_rerun: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _step_values(log: BuildLog, group_matrix: bool) -> dict[str, float]:
    """One duration per reported step: the median across matrix legs."""
    values: dict[str, list[float]] = defaultdict(list)

    for step in log.steps:
        if step.duration_seconds is None:
            continue
        name = normalize_matrix_name(step.name) if group_matrix else step.name
        values[name].append(step.duration_seconds)

    return {name: median(durations) for name, durations in values.items()}


def _median_or_none(values: list[float]) -> float | None:
    return median(values) if values else None


def compare_run(
    run_logs: list[BuildLog],
    baseline: list[BuildLog],
    baseline_branch: str,
    pricing: PricingTable | None = None,
    group_matrix: bool = False,
    threshold: float = DEFAULT_REGRESSION_THRESHOLD,
    min_seconds: float = DEFAULT_MIN_INCREASE_SECONDS,
    min_baseline_builds: int = DEFAULT_MIN_BASELINE_BUILDS,
) -> RunImpact:
    """Compare a run with its baseline.

    Args:
        run_logs: The run's attempts, oldest first; the last one is judged
        baseline: Recent builds of the same workflow on the baseline branch,
            including earlier attempts of re-runs (they carry flaky evidence)
        baseline_branch: Name of the baseline branch, for the report
        pricing: Runner pricing for cost; defaults to the built-in rates
        group_matrix: Merge the legs of a matrix job
        threshold: Relative change that makes a step change significant
        min_seconds: Absolute change that makes a step change significant
        min_baseline_builds: Builds a step needs in the baseline to be judged

    Returns:
        The comparison
    """
    pricing = pricing or PricingTable()
    run = run_logs[-1]
    builds = [log for log in baseline if not log.metadata.get("superseded_attempt")]

    def reported(name: str) -> str:
        return normalize_matrix_name(name) if group_matrix else name

    # Per step, one value per baseline build
    baseline_values: dict[str, list[float]] = defaultdict(list)
    for log in builds:
        for name, value in _step_values(log, group_matrix).items():
            baseline_values[name].append(value)

    changes: list[StepChange] = []
    new_steps: list[str] = []

    for name, current in _step_values(run, group_matrix).items():
        history = baseline_values.get(name)
        if not history:
            new_steps.append(name)
            continue
        if len(history) < min_baseline_builds:
            continue

        base = median(history)
        delta = current - base
        pct = delta / base if base > 0 else None
        significant = abs(delta) >= min_seconds and (pct is None or abs(pct) >= threshold)
        changes.append(StepChange(name, base, current, delta, pct, significant))

    changes.sort(key=lambda change: abs(change.delta_seconds), reverse=True)

    # Failures, checked against flaky evidence from the baseline and from
    # this run's own re-runs
    evidence = detect_flaky_by_commit(baseline + run_logs, reported)
    failed_names = list(dict.fromkeys(
        reported(step.name) for step in run.steps if step.status == "failure"
    ))
    failures = [FailedStep(name, name in evidence, evidence.get(name)) for name in failed_names]

    # Steps that failed on an earlier attempt of this run and passed on the last
    passed_now = {reported(step.name) for step in run.steps if step.status == "success"}
    failed_before = {
        reported(step.name)
        for log in run_logs[:-1]
        for step in log.steps
        if step.status == "failure"
    }
    recovered = sorted(failed_before & passed_now)

    def build_cost(log: BuildLog) -> float | None:
        return aggregate_job_metrics([log], pricing).get("estimated_cost_usd")

    return RunImpact(
        run_id=run.build_id,
        run_url=run.metadata.get("url"),
        workflow_name=run.metadata.get("workflow_name"),
        head_sha=run.head_sha,
        baseline_branch=baseline_branch,
        baseline_builds=len(builds),
        duration=run.total_duration,
        baseline_duration=_median_or_none([log.total_duration for log in builds if log.total_duration is not None]),
        compute_seconds=sum(step.duration_seconds or 0.0 for step in run.steps),
        baseline_compute_seconds=_median_or_none([
            sum(step.duration_seconds or 0.0 for step in log.steps) for log in builds
        ]),
        cost_usd=build_cost(run),
        baseline_cost_usd=_median_or_none([cost for cost in map(build_cost, builds) if cost is not None]),
        threshold=threshold,
        min_seconds=min_seconds,
        changes=changes,
        new_steps=new_steps,
        failures=failures,
        recovered_on_rerun=recovered,
    )


def _signed_duration(delta: float) -> str:
    return f"{'+' if delta >= 0 else '-'}{format_duration(abs(delta))}"


def _change_cell(current: float | None, base: float | None, significant: bool) -> str:
    """Change column: arrow when significant, approximately-equal otherwise."""
    if current is None or base is None:
        return "–"

    delta = current - base
    pct = f" ({'+' if delta >= 0 else ''}{delta / base * 100:.0f}%)" if base > 0 else ""
    marker = ("🔺" if delta > 0 else "🔻") if significant else "≈"
    return f"{marker} {_signed_duration(delta)}{pct}"


def _is_significant(current: float | None, base: float | None, threshold: float, min_seconds: float) -> bool:
    if current is None or base is None:
        return False
    delta = current - base
    return abs(delta) >= min_seconds and (base <= 0 or abs(delta) / base >= threshold)


def _md_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def format_impact_markdown(impact: RunImpact) -> str:
    """The pull request comment for a run's impact."""
    branch = f"`{impact.baseline_branch}`"
    run_label = f"[run {impact.run_id}]({impact.run_url})" if impact.run_url else f"run {impact.run_id}"
    workflow = f"**{impact.workflow_name}**" if impact.workflow_name else "This workflow"
    commit = f" on `{impact.head_sha[:7]}`" if impact.head_sha else ""

    lines = [COMMENT_MARKER, "## ⏱️ CI time impact", ""]

    if not impact.baseline_builds:
        lines += [
            f"{workflow} {run_label}{commit} took **{format_duration(impact.duration)}**.",
            "",
            f"No completed runs of this workflow on {branch} to compare with yet.",
            "",
        ]
    else:
        significant = _is_significant(
            impact.duration, impact.baseline_duration, impact.threshold, impact.min_seconds
        )
        headline = f"{workflow} {run_label}{commit} took **{format_duration(impact.duration)}**"
        if impact.duration is not None and impact.baseline_duration is not None:
            change = _change_cell(impact.duration, impact.baseline_duration, significant)
            headline += (
                f": **{change}** compared with the median of the last "
                f"{impact.baseline_builds} runs on {branch}."
            )
        lines += [headline, ""]

        lines += [
            f"| | {branch} (median) | This run | Change |",
            "|---|---:|---:|---:|",
            f"| Wall clock | {format_duration(impact.baseline_duration)} | {format_duration(impact.duration)} "
            f"| {_change_cell(impact.duration, impact.baseline_duration, significant)} |",
        ]

        compute_significant = _is_significant(
            impact.compute_seconds, impact.baseline_compute_seconds, impact.threshold, impact.min_seconds
        )
        lines.append(
            f"| Compute time | {format_duration(impact.baseline_compute_seconds)} "
            f"| {format_duration(impact.compute_seconds)} "
            f"| {_change_cell(impact.compute_seconds, impact.baseline_compute_seconds, compute_significant)} |"
        )

        if impact.cost_usd is not None and impact.baseline_cost_usd is not None:
            cost_delta = impact.cost_usd - impact.baseline_cost_usd
            lines.append(
                f"| Estimated cost | {format_cost(impact.baseline_cost_usd)} | {format_cost(impact.cost_usd)} "
                f"| {'+' if cost_delta >= 0 else '-'}{format_cost(abs(cost_delta))} |"
            )
        lines.append("")

        changed = [change for change in impact.changes if change.significant]
        lines += ["### Steps that changed", ""]
        if changed:
            lines += [
                f"| Step | {branch} (median) | This run | Change |",
                "|---|---:|---:|---:|",
            ]
            for change in changed:
                lines.append(
                    f"| {_md_cell(change.name)} | {format_duration(change.baseline_median)} "
                    f"| {format_duration(change.current)} "
                    f"| {_change_cell(change.current, change.baseline_median, True)} |"
                )
        else:
            lines.append(
                f"No step changed by more than {impact.threshold * 100:.0f}% "
                f"and {format_duration(impact.min_seconds)}."
            )
        lines.append("")

    if impact.failures:
        lines += ["### Failures", ""]
        for failure in impact.failures:
            if failure.known_flaky and failure.evidence:
                evidence = failure.evidence
                lines.append(
                    f"- ❌ `{failure.name}`: ⚠️ known to be flaky (failed and passed on the same "
                    f"commit in {evidence.get('flaky_commits', 0)} of "
                    f"{evidence.get('observed_commits', 0)} commits). A re-run may pass."
                )
            else:
                lines.append(f"- ❌ `{failure.name}`: not known to be flaky.")
        lines.append("")

    if impact.recovered_on_rerun:
        lines += ["### Passed on re-run", ""]
        for name in impact.recovered_on_rerun:
            lines.append(
                f"- 🔁 `{name}` failed on an earlier attempt of this run and passed on re-run: likely flaky."
            )
        lines.append("")

    if impact.new_steps:
        shown = ", ".join(f"`{name}`" for name in impact.new_steps[:10])
        more = f" and {len(impact.new_steps) - 10} more" if len(impact.new_steps) > 10 else ""
        lines += [f"New steps, with no baseline yet: {shown}{more}.", ""]

    lines.append(
        f"<sub>Baseline: the last {impact.baseline_builds} completed runs of this workflow on "
        f"{branch}. Steps are listed when they changed by at least {impact.threshold * 100:.0f}% "
        f"and {format_duration(impact.min_seconds)}. Cost at private-repository rates. "
        "Generated by ci-time-tracker.</sub>"
    )

    return "\n".join(lines) + "\n"
