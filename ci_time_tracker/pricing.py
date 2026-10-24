"""Runner pricing for cost estimates.

The defaults are GitHub's list prices for private repositories, as published at
PRICES_SOURCE_URL on PRICES_AS_OF. Public repositories pay nothing for
standard GitHub-hosted runners, and self-hosted runners are free. Prices
change, so they can be overridden with a JSON file (see load_pricing).

Each job's cost is its duration rounded up to a whole minute. GitHub has
billed that way; its current documentation does not state the rounding.
"""

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PRICES_AS_OF = "2026-10"
PRICES_SOURCE_URL = "https://docs.github.com/en/billing/reference/actions-runner-pricing"

# USD per minute for the labels that are not versioned (versioned labels such
# as ubuntu-24.04 or macos-15 are matched by _LABEL_FAMILIES below)
DEFAULT_PRICES_USD_PER_MINUTE: dict[str, float] = {
    "ubuntu-latest": 0.006,
    "ubuntu-slim": 0.002,
    "windows-latest": 0.010,
    "macos-latest": 0.062,
    "macos-latest-large": 0.077,
    "macos-latest-xlarge": 0.102,
}

# GitHub-hosted label families, most specific first, so that labels GitHub
# adds later (ubuntu-26.04, macos-26-intel, ...) are priced without an update
_LABEL_FAMILIES: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"^ubuntu-(latest|\d+\.\d+)-arm$"), 0.005),  # Linux 2-core arm64
    (re.compile(r"^ubuntu-(latest|\d+\.\d+)$"), 0.006),  # Linux 2-core x64
    (re.compile(r"^windows-(latest|\d+)-arm$"), 0.010),  # Windows 2-core arm64
    (re.compile(r"^windows-(latest|\d{4})$"), 0.010),  # Windows 2-core x64
    (re.compile(r"^macos-(latest|\d+)-xlarge$"), 0.102),  # macOS 5-core M2 Pro
    (re.compile(r"^macos-(latest|\d+)-large$"), 0.077),  # macOS 12-core Intel
    (re.compile(r"^macos-(latest|\d+)(-intel)?$"), 0.062),  # macOS 3/4-core
]

# Larger runners are named freely (e.g. "ubuntu-24.04-8-cores"), so they are
# priced from the platform, architecture and core count in the label. The
# published rates do not scale linearly with cores, hence the tables; a core
# count that is not published stays unpriced rather than extrapolated.
_LARGER_RUNNER_PRICES: dict[tuple[str, str], dict[int, float]] = {
    ("linux", "x64"): {2: 0.006, 4: 0.012, 8: 0.022, 16: 0.042, 32: 0.082, 64: 0.162, 96: 0.252},
    ("windows", "x64"): {4: 0.022, 8: 0.042, 16: 0.082, 32: 0.162, 64: 0.322, 96: 0.552},
    ("linux", "arm64"): {2: 0.005, 4: 0.008, 8: 0.014, 16: 0.026, 32: 0.050, 64: 0.098},
    ("windows", "arm64"): {2: 0.008, 4: 0.014, 8: 0.026, 16: 0.050, 32: 0.098, 64: 0.194},
}

_CORES_PATTERN = re.compile(r"(\d+)[-_ ]?cores?\b", re.IGNORECASE)

# A self-hosted runner costs the user nothing on GitHub's bill.
SELF_HOSTED_LABELS = frozenset({"self-hosted", "selfhosted"})


@dataclass
class PricingTable:
    """Maps runner labels to a USD-per-minute rate.

    Attributes:
        prices: Runner label (lowercased) to USD per minute; checked before
            the label families and larger-runner tables, so a file can
            override any of them
        source: Where the rates came from, for the report
    """

    prices: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PRICES_USD_PER_MINUTE)
    )
    source: str = f"GitHub list prices as of {PRICES_AS_OF}"
    # Resolved rates by runner string: an analysis asks once per step
    # execution, for a handful of distinct runners
    _resolved: dict[str | None, float | None] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def price_for(self, runner: str | None) -> float | None:
        """Return the USD-per-minute rate for a runner, or None if unknown.

        `runner` may be a comma-separated label list, as GitHub reports it.
        Unknown runners return None rather than 0, so that an incomplete
        estimate is never presented as a complete one.
        """
        if runner not in self._resolved:
            self._resolved[runner] = self._resolve(runner)
        return self._resolved[runner]

    def _resolve(self, runner: str | None) -> float | None:
        """Look up a runner's rate, without caching."""
        if not runner:
            return None

        labels = [label.strip().lower() for label in runner.split(",") if label.strip()]
        if not labels:
            return None

        # A self-hosted job is not billed, whatever else it is labelled with
        if any(label in SELF_HOSTED_LABELS for label in labels):
            return 0.0

        for label in labels:
            if label in self.prices:
                return self.prices[label]

        for label in labels:
            for pattern, price in _LABEL_FAMILIES:
                if pattern.match(label):
                    return price

        for label in labels:
            derived = _price_from_core_count(label)
            if derived is not None:
                return derived

        return None


def _price_from_core_count(label: str) -> float | None:
    """Price a larger runner from the platform and core count in its label."""
    cores_match = _CORES_PATTERN.search(label)
    if not cores_match:
        return None

    if "windows" in label:
        platform = "windows"
    elif "ubuntu" in label or "linux" in label:
        platform = "linux"
    else:
        return None

    architecture = "arm64" if "arm" in label else "x64"
    return _LARGER_RUNNER_PRICES[(platform, architecture)].get(int(cores_match.group(1)))


def load_pricing(path: str | Path) -> PricingTable:
    """Load a runner pricing table from a JSON file.

    The file maps runner labels to USD per minute:

        {"ubuntu-latest": 0.006, "my-big-runner": 0.032}

    Args:
        path: Path to the JSON file

    Returns:
        PricingTable with the file's rates taking precedence over the defaults

    Raises:
        ValueError: If the file is not a JSON object of non-negative numbers
        OSError: If the file cannot be read
    """
    content = Path(path).read_text(encoding="utf-8")

    try:
        data: Any = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON in pricing file: {error.msg}") from error

    if not isinstance(data, dict):
        raise ValueError("Pricing file must contain a JSON object of label -> USD per minute")

    prices = dict(DEFAULT_PRICES_USD_PER_MINUTE)

    for label, price in data.items():
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            raise ValueError(f"Price for '{label}' must be a number, got {type(price).__name__}")
        if price < 0:
            raise ValueError(f"Price for '{label}' cannot be negative")
        prices[str(label).strip().lower()] = float(price)

    return PricingTable(prices=prices, source=str(path))


def job_cost(duration_seconds: float | None, price_per_minute: float | None) -> float | None:
    """Cost of one job, its duration rounded up to a whole minute.

    Returns None when either the duration or the rate is unknown.
    """
    if duration_seconds is None or price_per_minute is None:
        return None
    if duration_seconds <= 0:
        return 0.0

    return math.ceil(duration_seconds / 60.0) * price_per_minute


def attributed_cost(duration_seconds: float | None, price_per_minute: float | None) -> float | None:
    """Cost attributed to a step, without per-job rounding.

    Used to rank steps by spend. The sum over steps is therefore slightly
    below the job total, which rounds each job up to a whole minute.
    """
    if duration_seconds is None or price_per_minute is None:
        return None

    return (duration_seconds / 60.0) * price_per_minute
