"""Self-contained HTML report.

One file with inline CSS and SVG, no scripts and no external requests, so it
can be opened offline, attached to an issue or kept as a CI artifact. Every
value from the data (step, job and workflow names come from repositories and
pull requests) is escaped.
"""

import html
import math
from statistics import median
from typing import Any

from ci_time_tracker import __version__
from ci_time_tracker.models import Report
from ci_time_tracker.reporter import (
    _AT_STAKE_NOTE,
    _at_stake,
    _format_issue_details,
    _rank_consumers,
    cost_coverage,
    format_cost,
    format_duration,
)

# Builds in the rolling median drawn over the build durations
_TREND_WINDOW = 5

# Steps shown in the time consumers chart
_CHART_STEPS = 8

_FAILED_CONCLUSIONS = {"failure", "timed_out", "startup_failure"}

_CSS = """
:root {
  --bg: #ffffff; --fg: #1f2328; --muted: #59636e; --card: #f6f8fa; --border: #d1d9e0;
  --accent: #0969da; --ok: #1a7f37; --fail: #cf222e; --warn: #9a6700; --line: #8c959f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --card: #151b23; --border: #3d444d;
    --accent: #4493f8; --ok: #3fb950; --fail: #f85149; --warn: #d29922; --line: #656c76;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
}
main { max-width: 1040px; margin: 0 auto; padding: 32px 20px 48px; }
a { color: var(--accent); }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 0 0 12px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }
h3 { font-size: 15px; margin: 0 0 4px; }
section h3 { margin-top: 18px; }
section { margin-top: 36px; }
p { margin: 4px 0; }
.muted { color: var(--muted); }
code, pre { font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; font-size: 12.5px; }
pre { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; overflow-x: auto; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 24px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; }
.card .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
.card .value { font-size: 22px; font-weight: 600; margin-top: 2px; font-variant-numeric: tabular-nums; }
.card .note { color: var(--muted); font-size: 12px; }
.item { background: var(--card); border: 1px solid var(--border); border-left: 4px solid var(--accent);
        border-radius: 6px; padding: 10px 14px; margin: 10px 0; }
.item.regression { border-left-color: var(--warn); }
.item.flaky { border-left-color: var(--fail); }
.clean { color: var(--ok); font-weight: 600; }
.chart { display: block; width: 100%; height: auto; }
.chart text { fill: var(--muted); font-size: 11px; }
.chart .name { fill: var(--fg); font-size: 12px; }
.chart .grid { stroke: var(--border); stroke-width: 1; }
.chart .trace { fill: none; stroke: var(--line); stroke-width: 1.2; }
.chart .trend { fill: none; stroke: var(--accent); stroke-width: 2.2; }
.chart .ok { fill: var(--ok); }
.chart .fail { fill: var(--fail); }
.chart .other { fill: var(--line); }
.chart .bar { fill: var(--accent); }
.legend { color: var(--muted); font-size: 12px; margin-top: 6px; }
.legend span { margin-right: 16px; white-space: nowrap; }
.swatch { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 5px; vertical-align: -1px; }
.swatch.line { border-radius: 2px; height: 3px; width: 14px; vertical-align: 2px; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: 13px; font-variant-numeric: tabular-nums; }
th, td { border-bottom: 1px solid var(--border); padding: 6px 8px; text-align: right; white-space: nowrap; }
th:first-child, td:first-child { text-align: left; white-space: normal; }
th { color: var(--muted); font-weight: 600; }
details summary { cursor: pointer; color: var(--accent); }
footer { margin-top: 44px; color: var(--muted); font-size: 12px; }
footer p { margin: 2px 0; }
"""


def _e(value: Any) -> str:
    """Escape a value for HTML text or attributes."""
    return html.escape(str(value), quote=True)


def _card(label: str, value: str, note: str | None = None) -> str:
    note_html = f'<div class="note">{_e(note)}</div>' if note else ""
    return f'<div class="card"><div class="label">{_e(label)}</div><div class="value">{_e(value)}</div>{note_html}</div>'


def _summary_cards(report: Report) -> str:
    summary = report.summary
    cards = []

    if "build_count" in summary:
        cards.append(_card("Builds", str(summary["build_count"])))
    if summary.get("build_p50_seconds") is not None:
        p90 = summary.get("build_p90_seconds")
        notes = [f"p90 {format_duration(p90)}" if p90 is not None else None]
        workflows = {entry.get("workflow") for entry in report.timeline if entry.get("workflow")}
        if len(workflows) > 1:
            # A median over a test suite and a 10-second housekeeping job
            # describes neither, so say what it spans
            notes.insert(0, f"across {len(workflows)} workflows")
        cards.append(_card(
            "Median build",
            format_duration(summary["build_p50_seconds"]),
            " · ".join(note for note in notes if note) or None,
        ))
    if summary.get("total_execution_seconds") is not None:
        cards.append(_card("Execution", format_duration(summary["total_execution_seconds"]), "across all steps"))
    if summary.get("total_queue_seconds") is not None:
        cards.append(_card(
            "Queued",
            format_duration(summary["total_queue_seconds"]),
            f"median {format_duration(summary.get('queue_p50_seconds'))} per job",
        ))
    if summary.get("estimated_cost_usd") is not None:
        if summary.get("repository_visibility") == "public":
            note = "public repository: standard runners are free"
        elif cost_coverage(summary):
            note = f"{summary.get('priced_jobs', 0)} of {summary.get('job_count', 0)} jobs priced"
        else:
            note = "at GitHub list prices"
        cards.append(_card("Estimated cost", format_cost(summary["estimated_cost_usd"]), note))

    issues = len(report.issues)
    cards.append(_card("Issues", str(issues), "regressions and flaky steps"))

    return f'<div class="cards">{"".join(cards)}</div>'


# Round steps for the duration axis, in seconds
_AXIS_STEPS = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400]

# Workflows charted, the ones whose builds took the most time first
_CHART_WORKFLOWS = 3


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: the smallest value with `fraction` at or below it."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _axis(durations: list[float]) -> tuple[float, float]:
    """The axis top and tick step, robust to outliers.

    One stuck build should not flatten every other one against zero: when the
    longest build is far above the rest, the axis stops near the 95th
    percentile and the outliers are drawn at its edge.
    """
    longest = max(durations)
    typical = _percentile(durations, 0.95)
    wanted = typical * 1.25 if longest > typical * 1.5 else longest * 1.1
    wanted = max(wanted, 1.0)

    step = next((step for step in _AXIS_STEPS if wanted / step <= 5), _AXIS_STEPS[-1])
    top = step * -(-wanted // step)  # round up to a whole step
    return top, step


def _tick_label(seconds: float) -> str:
    """Compact axis label for a round duration: 30s, 5m, 1h, 1h30m."""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    if not hours:
        return f"{minutes}m"
    return f"{hours}h{minutes:02d}m" if minutes else f"{hours}h"


def _timeline_svg(points: list[dict[str, Any]]) -> str:
    """One workflow's build durations over time, coloured by outcome."""
    width, height = 960, 240
    left, right, top_margin, bottom = 64, 16, 12, 30
    plot_w, plot_h = width - left - right, height - top_margin - bottom
    peak, step = _axis([entry["duration"] for entry in points])

    def x(index: int) -> float:
        return left + plot_w * index / max(1, len(points) - 1)

    def y(value: float) -> float:
        return top_margin + plot_h * (1 - min(value, peak) / peak)

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Build duration over time">']

    tick = 0.0
    while tick <= peak + 1e-9:
        parts.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>')
        parts.append(f'<text x="{left - 8}" y="{y(tick) + 4:.1f}" text-anchor="end">{_e(_tick_label(tick))}</text>')
        tick += step

    trace = " ".join(f"{x(i):.1f},{y(entry['duration']):.1f}" for i, entry in enumerate(points))
    parts.append(f'<polyline class="trace" points="{trace}"/>')

    # Only full windows: a partial one at the start is one or two builds
    # wide and would follow a single outlier
    if len(points) >= _TREND_WINDOW:
        trend = [
            f"{x(i):.1f},{y(median(entry['duration'] for entry in points[i - _TREND_WINDOW + 1: i + 1])):.1f}"
            for i in range(_TREND_WINDOW - 1, len(points))
        ]
        parts.append(f'<polyline class="trend" points="{" ".join(trend)}"/>')

    for i, entry in enumerate(points):
        conclusion = entry.get("conclusion")
        kind = "ok" if conclusion == "success" else "fail" if conclusion in _FAILED_CONCLUSIONS else "other"
        label = " · ".join(str(part) for part in (
            (entry.get("timestamp") or "")[:16].replace("T", " "),
            f"build {entry['build_id']}" if entry.get("build_id") else None,
            format_duration(entry["duration"]),
            conclusion,
            entry.get("head_sha"),
        ) if part)
        cx, cy = x(i), y(entry["duration"])
        if entry["duration"] > peak:
            # Above the axis: a triangle at the top edge, the real value on hover
            mark = (
                f'<polygon class="{kind}" points="{cx - 5:.1f},{cy + 7:.1f} {cx + 5:.1f},{cy + 7:.1f} {cx:.1f},{cy - 1:.1f}">'
                f"<title>{_e(label)}</title></polygon>"
            )
        else:
            mark = f'<circle class="{kind}" cx="{cx:.1f}" cy="{cy:.1f}" r="3.6"><title>{_e(label)}</title></circle>'
        if entry.get("url"):
            mark = f'<a href="{_e(entry["url"])}">{mark}</a>'
        parts.append(mark)

    first, last = (points[0].get("timestamp") or "")[:10], (points[-1].get("timestamp") or "")[:10]
    parts.append(f'<text x="{left}" y="{height - 8}">{_e(first)}</text>')
    parts.append(f'<text x="{width - right}" y="{height - 8}" text-anchor="end">{_e(last)}</text>')
    parts.append("</svg>")

    above = [entry["duration"] for entry in points if entry["duration"] > peak]
    note = ""
    if above:
        count = len(above)
        note = (
            f'<p class="muted">{count} build{"s" if count != 1 else ""} above the chart, '
            f"up to {_e(format_duration(max(above)))} (▲).</p>"
        )
    return "".join(parts) + note


def _timelines_html(timeline: list[dict[str, Any]]) -> str:
    """One build-duration chart per workflow, the costliest workflows first.

    Workflows are charted apart: a 10-second housekeeping workflow next to a
    15-minute test suite would only draw a zigzag.
    """
    workflows: dict[str, list[dict[str, Any]]] = {}
    for entry in timeline:
        if entry.get("duration") is not None:
            workflows.setdefault(entry.get("workflow") or "Builds", []).append(entry)

    charted = sorted(
        (item for item in workflows.items() if len(item[1]) >= 2),
        key=lambda item: sum(entry["duration"] for entry in item[1]),
        reverse=True,
    )[:_CHART_WORKFLOWS]

    if not charted:
        return '<p class="muted">Not enough builds with a duration to chart.</p>'

    legend = (
        '<div class="legend">'
        '<span><i class="swatch" style="background:var(--ok)"></i>success</span>'
        '<span><i class="swatch" style="background:var(--fail)"></i>failure</span>'
        '<span><i class="swatch" style="background:var(--line)"></i>other</span>'
        f'<span><i class="swatch line" style="background:var(--accent)"></i>median of {_TREND_WINDOW} builds</span>'
        "</div>"
    )
    charts = "".join(
        f'<h3>{_e(name)} <span class="muted">· {len(points)} builds</span></h3>{_timeline_svg(points)}'
        for name, points in charted
    )
    others = len(workflows) - len(charted)
    more = f'<p class="muted">{others} other workflow{"s" if others != 1 else ""} not charted.</p>' if others > 0 else ""
    return charts + legend + more


def _consumers_svg(steps: list[dict[str, Any]]) -> str:
    """Horizontal bars for the steps that consume the most time."""
    ranked = _rank_consumers(steps)[:_CHART_STEPS]
    if not ranked:
        return '<p class="muted">No step durations recorded.</p>'

    width, row = 960, 30
    label_w, value_w = 380, 150
    bar_max = width - label_w - value_w
    peak = ranked[0][0]["total_duration"] or 1.0
    height = row * len(ranked) + 6

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-label="Top time consumers">']
    for i, (step, share) in enumerate(ranked):
        y = i * row + 4
        name = step["name"]
        shown = name if len(name) <= 52 else name[:51] + "…"
        bar = max(2.0, bar_max * step["total_duration"] / peak)
        cost = step.get("estimated_cost_usd")
        value = f"{format_duration(step['total_duration'])} · {share}" + (f" · {format_cost(cost)}" if cost else "")
        parts.append(
            f'<text class="name" x="0" y="{y + 16}"><title>{_e(name)}</title>{_e(shown)}</text>'
            f'<rect class="bar" x="{label_w}" y="{y + 4}" width="{bar:.1f}" height="16" rx="3"/>'
            f'<text x="{label_w + bar + 8:.1f}" y="{y + 16}">{_e(value)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _issues_html(report: Report) -> str:
    if not report.issues:
        return '<p class="clean">✓ No flaky steps or regressions found.</p>'

    urls = {entry.get("build_id"): entry.get("url") for entry in report.timeline}
    items = []

    for issue in report.issues:
        kind = issue["type"]
        details = "".join(f"<p>{_e(line.strip())}</p>" for line in _format_issue_details(issue))
        started = (issue.get("started_at") or {}).get("build_id")
        link = (
            f'<p><a href="{_e(urls[started])}">The first slow build</a></p>'
            if started and urls.get(started) else ""
        )
        label = {"regression": "Regression", "flaky": "Flaky"}.get(kind, kind.title())
        items.append(
            f'<div class="item {_e(kind)}"><h3>{_e(label)}: <code>{_e(issue["step"])}</code></h3>'
            f"{details}{link}</div>"
        )

    return "".join(items)


def _recommendations_html(recommendations: list[dict[str, Any]]) -> str:
    items = []
    for item in recommendations:
        suggestion = item["suggestion"].splitlines()
        suggestion_html = f"<p>{_e(suggestion[0])}</p>"
        if len(suggestion) > 1:
            suggestion_html += f"<pre>{_e(chr(10).join(suggestion[1:]))}</pre>"
        where = f'<p class="muted"><code>{_e(item["workflow"])}</code></p>' if item.get("workflow") else ""
        items.append(
            f'<div class="item"><h3>{_e(item["title"])}</h3>{where}'
            f"<p>{_e(item['evidence'])}</p>{suggestion_html}"
            f'<p class="muted">At stake: {_e(_at_stake(item))}</p></div>'
        )
    return "".join(items) + f'<p class="muted">{_e(_AT_STAKE_NOTE[0].upper() + _AT_STAKE_NOTE[1:])}.</p>'


def _steps_table(steps: list[dict[str, Any]], top: int | None) -> str:
    shown = steps[:top] if top else steps
    rows = []

    def seconds(value: Any) -> str:
        return format_duration(value) if value is not None else "–"

    for step in shown:
        flags = " ".join(flag for flag, key in (("REGR", "is_regression"), ("FLAKY", "is_flaky")) if step.get(key))
        cost = step.get("estimated_cost_usd")
        rows.append(
            f"<tr><td>{_e(step['name'])}</td><td>{_e(step.get('execution_count', 0))}</td>"
            f"<td>{_e(seconds(step.get('total_duration')))}</td><td>{_e(seconds(step.get('p50')))}</td>"
            f"<td>{_e(seconds(step.get('p90')))}</td><td>{_e(seconds(step.get('p99')))}</td>"
            f"<td>{_e(format_cost(cost) if cost is not None else '–')}</td><td>{_e(flags)}</td></tr>"
        )

    hidden = len(steps) - len(shown)
    more = f'<p class="muted">…and {hidden} more steps.</p>' if hidden > 0 else ""
    return (
        f"<details><summary>All steps ({len(steps)})</summary><div class=\"table-wrap\"><table>"
        "<tr><th>Step</th><th>Runs</th><th>Total</th><th>P50</th><th>P90</th><th>P99</th><th>Cost</th><th>Flags</th></tr>"
        f"{''.join(rows)}</table></div>{more}</details>"
    )


def _footer(report: Report) -> str:
    summary = report.summary
    notes = [f"Generated {report.generated_at.strftime('%Y-%m-%d %H:%M')} by ci-time-tracker {__version__}."]

    if summary.get("estimated_cost_usd") is not None:
        if summary.get("repository_visibility") == "public":
            notes.append("Cost: public repository, so standard runners are free; larger runners at GitHub's list prices.")
        else:
            notes.append("Cost at GitHub's private-repository list prices, each job rounded up to a whole minute.")
        coverage = cost_coverage(summary)
        if coverage:
            notes.append(f"The {coverage}.")
    if summary.get("flaky_detection") == "same-commit":
        notes.append("Flaky means a step failed and passed on the same commit.")
    elif summary.get("flaky_detection") == "failure-rate":
        notes.append("Flaky is judged by failure rate only (no commit data), which cannot tell flakiness from breakage.")
    if "regression_checked_steps" in summary:
        notes.append(
            f"Regressions were checked on {summary['regression_checked_steps']} steps with at least "
            f"{summary.get('regression_min_builds', '?')} builds."
        )

    return "<footer>" + "".join(f"<p>{_e(note)}</p>" for note in notes) + "</footer>"


def format_html(report: Report, top: int | None = None) -> str:
    """Format a Report as a self-contained HTML page.

    Args:
        report: The report to format
        top: Show only the N steps that consume the most time in the step
            table; None or 0 shows every step

    Returns:
        A complete HTML document
    """
    summary = report.summary
    repository = summary.get("repository")
    heading = f"CI time report: {repository}" if repository else report.title

    subtitle_parts = [f"{summary['build_count']} builds" if "build_count" in summary else None]
    if report.timeline:
        first = (report.timeline[0].get("timestamp") or "")[:10]
        last = (report.timeline[-1].get("timestamp") or "")[:10]
        if first and last:
            subtitle_parts.append(f"{first} to {last}")
    if summary.get("grouping"):
        subtitle_parts.append(summary["grouping"])
    subtitle = " · ".join(part for part in subtitle_parts if part)

    body = [f"<h1>{_e(heading)}</h1>", f'<p class="muted">{_e(subtitle)}</p>' if subtitle else ""]

    if report.mode == "log":
        body.append(_summary_cards(report))
        if report.timeline:
            body.append(f"<section><h2>Build duration</h2>{_timelines_html(report.timeline)}</section>")
        body.append(f"<section><h2>Issues</h2>{_issues_html(report)}</section>")
        if report.recommendations:
            body.append(
                f"<section><h2>Recommendations</h2>{_recommendations_html(report.recommendations)}</section>"
            )
        body.append(f"<section><h2>Where the time goes</h2>{_consumers_svg(report.steps)}</section>")
        body.append(f"<section>{_steps_table(report.steps, top)}</section>")
    else:
        rows = "".join(
            f"<tr><td>{_e(step['name'])}</td><td>{_e(step.get('stage') or '')}</td>"
            f"<td>{_e(format_duration(step['estimated_duration']) if step.get('estimated_duration') is not None else '–')}</td></tr>"
            for step in report.steps
        )
        body.append(
            '<section><div class="table-wrap"><table><tr><th>Step</th><th>Stage</th><th>Estimate</th></tr>'
            f"{rows}</table></div></section>"
        )

    body.append(_footer(report))

    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_e(heading)}</title>\n<style>{_CSS}</style>\n</head>\n"
        f"<body>\n<main>\n{''.join(body)}\n</main>\n</body>\n</html>\n"
    )
