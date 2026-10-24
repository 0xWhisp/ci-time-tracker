"""Shared test configuration.

Hypothesis fails any example that takes longer than 200 ms by default. On a
loaded machine (or a CI runner) a cheap example occasionally crosses that
wall-clock limit, which fails the test without any defect in the code. These
property tests check correctness, not speed, so the deadline is disabled.
"""

from hypothesis import settings

settings.register_profile("ci-time-tracker", deadline=None)
settings.load_profile("ci-time-tracker")
