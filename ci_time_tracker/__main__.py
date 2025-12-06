"""Module entry point for ci-time-tracker.

Enables execution via: python -m ci_time_tracker
"""

import sys

from ci_time_tracker.cli import main

if __name__ == "__main__":
    sys.exit(main())

