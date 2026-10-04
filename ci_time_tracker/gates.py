"""Quality gates: turn findings into a failing exit code for CI.

A gate lets the report fail the pipeline it runs in, e.g. when a step became
flaky or regressed, or when builds got too slow.
"""

from collections.abc import Iterable

from ci_time_tracker.models import AnalysisResult
from ci_time_tracker.reporter import format_duration

# Findings that --fail-on can gate on
GATE_CHOICES = ("flaky", "regression")

# How many step names a failure reason lists before summarizing the rest
_LISTED_STEPS = 5


def _list_steps(names: list[str]) -> str:
    listed = ", ".join(names[:_LISTED_STEPS])
    hidden = len(names) - _LISTED_STEPS
    return f"{listed} and {hidden} more" if hidden > 0 else listed


def evaluate_gates(
    result: AnalysisResult,
    fail_on: Iterable[str] = (),
    max_duration: float | None = None,
) -> list[str]:
    """Return the reasons the gates fail, or an empty list when they pass.

    Args:
        result: The analysis to judge
        fail_on: Findings that fail the gate: "flaky", "regression"
        max_duration: Fail when the median build duration exceeds this many
            seconds. Not checked when no build durations are known.

    Returns:
        Human-readable failure reasons
    """
    gates = set(fail_on)
    reasons: list[str] = []

    if "flaky" in gates and result.flaky_steps:
        count = len(result.flaky_steps)
        reasons.append(f"{count} flaky step{'s' if count != 1 else ''}: {_list_steps(result.flaky_steps)}")

    if "regression" in gates and result.regressed_steps:
        count = len(result.regressed_steps)
        reasons.append(
            f"{count} regressed step{'s' if count != 1 else ''}: {_list_steps(result.regressed_steps)}"
        )

    if max_duration is not None:
        median_build = result.metadata.get("build_p50_seconds")
        if median_build is not None and median_build > max_duration:
            reasons.append(
                f"median build duration {format_duration(median_build)} exceeds "
                f"the {format_duration(max_duration)} limit"
            )

    return reasons
