"""Runner pricing for cost estimates.

GitHub bills hosted runners per minute, rounding each job up to the next whole
minute. The defaults below are GitHub's published rates for private
repositories; public repositories do not pay for standard runners. Rates
change, so they are overridable with a JSON file (see load_pricing).
"""

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# USD per minute, private-repository rates for GitHub-hosted runners.
DEFAULT_PRICES_USD_PER_MINUTE: dict[str, float] = {
    # Linux, 2 cores
    "ubuntu-latest": 0.008,
    "ubuntu-24.04": 0.008,
    "ubuntu-22.04": 0.008,
    "ubuntu-20.04": 0.008,
    # Windows, 2 cores
    "windows-latest": 0.016,
    "windows-2025": 0.016,
    "windows-2022": 0.016,
    "windows-2019": 0.016,
    # macOS, 3-4 cores
    "macos-latest": 0.08,
    "macos-15": 0.08,
    "macos-14": 0.08,
    "macos-13": 0.08,
    # macOS, 12 cores
    "macos-latest-xlarge": 0.16,
    "macos-15-xlarge": 0.16,
    "macos-14-xlarge": 0.16,
}

# Larger runners are named freely (e.g. "ubuntu-latest-8-cores"), so their
# price is derived from the core count: on Linux and Windows the published
# rates scale linearly with cores.
_PRICE_PER_CORE: dict[str, float] = {
    "ubuntu": 0.004,
    "linux": 0.004,
    "windows": 0.008,
}

_CORES_PATTERN = re.compile(r'(\d+)[-_ ]?cores?\b', re.IGNORECASE)

# A self-hosted runner costs the user nothing on GitHub's bill.
SELF_HOSTED_LABELS = frozenset({"self-hosted", "selfhosted"})


@dataclass
class PricingTable:
    """Maps runner labels to a USD-per-minute rate.

    Attributes:
        prices: Runner label (lowercased) to USD per minute
        source: Where the table came from, for the report
    """

    prices: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_PRICES_USD_PER_MINUTE)
    )
    source: str = "built-in defaults"

    def price_for(self, runner: str | None) -> float | None:
        """Return the USD-per-minute rate for a runner, or None if unknown.

        `runner` may be a comma-separated label list, as GitHub reports it.
        Unknown runners return None rather than 0, so that an incomplete
        estimate is never presented as a complete one.
        """
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
            derived = self._price_from_core_count(label)
            if derived is not None:
                return derived

        return None

    def _price_from_core_count(self, label: str) -> float | None:
        """Derive a larger runner's price from its core count."""
        cores_match = _CORES_PATTERN.search(label)
        if not cores_match:
            return None

        for platform, per_core in _PRICE_PER_CORE.items():
            if platform in label:
                return int(cores_match.group(1)) * per_core

        return None


def load_pricing(path: str | Path) -> PricingTable:
    """Load a runner pricing table from a JSON file.

    The file maps runner labels to USD per minute:

        {"ubuntu-latest": 0.008, "my-big-runner": 0.032}

    Args:
        path: Path to the JSON file

    Returns:
        PricingTable with the file's rates merged over the defaults

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
    """Cost of one job, rounding up to the next whole minute as GitHub does.

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
    below the billed total, which includes GitHub's per-job rounding.
    """
    if duration_seconds is None or price_per_minute is None:
        return None

    return (duration_seconds / 60.0) * price_per_minute
