"""Shared test configuration.

Hypothesis fails any example that takes longer than 200 ms by default. On a
loaded machine (or a CI runner) a cheap example occasionally crosses that
wall-clock limit, which fails the test without any defect in the code. These
property tests check correctness, not speed, so the deadline is disabled.

Tests never reach the network: a request a test did not stub fails at once
instead of calling GitHub, and its retries do not wait.
"""

import socket

import pytest
from hypothesis import settings

settings.register_profile("ci-time-tracker", deadline=None)
settings.load_profile("ci-time-tracker")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("network access is disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr("ci_time_tracker.github_api._sleep", lambda seconds: None)
