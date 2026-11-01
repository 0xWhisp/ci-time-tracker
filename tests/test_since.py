"""Analyzing a period (--since) rather than a number of runs: on a busy
repository 300 runs can be a single day, too little to see a trend."""

import argparse
from datetime import datetime, timedelta, timezone

import pytest

from ci_time_tracker.cli import _since, main, parse_args
from ci_time_tracker.github_api import MAX_LISTED_RUNS, GitHubClient
from ci_time_tracker.models import BuildLog, StepExecution


class TestPeriod:
    @pytest.mark.parametrize(("value", "delta"), [
        ("12h", timedelta(hours=12)),
        ("30d", timedelta(days=30)),
        ("2w", timedelta(weeks=2)),
        ("7D", timedelta(days=7)),
    ])
    def test_periods_count_back_from_now(self, value, delta):
        expected = datetime.now(timezone.utc) - delta

        assert abs(_since(value) - expected) < timedelta(seconds=5)

    def test_a_date_is_midnight_utc(self):
        assert _since("2026-10-01") == datetime(2026, 10, 1, tzinfo=timezone.utc)

    def test_a_time_with_an_offset_is_kept(self):
        moment = _since("2026-10-01T12:00:00+02:00")

        assert moment == datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)

    @pytest.mark.parametrize("value", ["0d", "30", "a month", "-3d", "2026-13-01"])
    def test_invalid_periods(self, value):
        with pytest.raises(argparse.ArgumentTypeError):
            _since(value)

    def test_last_has_no_default_of_its_own(self):
        args = parse_args(["--github", "acme/web", "--since", "30d"])

        assert args.last is None
        assert args.since is not None


class TestListing:
    def test_the_period_becomes_a_created_filter(self):
        calls = []

        def transport(url, headers):
            calls.append(url)
            return {"workflow_runs": []}, {}

        client = GitHubClient(repo="acme/web", transport=transport)
        since = datetime(2026, 10, 1, 12, 30, tzinfo=timezone(timedelta(hours=-3)))

        list(client.iter_workflow_runs(limit=10, since=since))

        assert "created=%3E%3D2026-10-01T15%3A30%3A00Z" in calls[0]

    def test_the_matching_total_is_reported(self):
        def transport(url, headers):
            return {"total_count": 1682, "workflow_runs": []}, {}

        listing = {}
        list(GitHubClient(repo="acme/web", transport=transport).iter_workflow_runs(limit=10, listing=listing))

        assert listing == {"total": 1682}

    def test_no_period_no_filter(self):
        calls = []

        def transport(url, headers):
            calls.append(url)
            return {"workflow_runs": []}, {}

        list(GitHubClient(repo="acme/web", transport=transport).iter_workflow_runs(limit=10))

        assert "created=" not in calls[0]


def logs(count):
    return [
        BuildLog(
            build_id=str(index),
            steps=[StepExecution(name="test / Run tests", duration_seconds=60.0)],
            total_duration=60.0,
            timestamp=datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(hours=index),
            metadata={"branch": "main"},
        )
        for index in range(count)
    ]


class TestCli:
    def run(self, monkeypatch, argv, count):
        captured = {}

        def fake_fetch(**kwargs):
            captured.update(kwargs)
            if kwargs.get("listing") is not None:
                kwargs["listing"]["total"] = count
            return logs(min(count, kwargs["limit"]))

        monkeypatch.setattr("ci_time_tracker.cli.fetch_build_logs", fake_fetch)
        monkeypatch.setattr("sys.argv", ["ci-time-tracker", "--github", "acme/web", "--no-cache", *argv])
        assert main() == 0
        return captured

    def test_a_period_is_analyzed_whole(self, monkeypatch, capsys):
        captured = self.run(monkeypatch, ["--since", "30d"], count=120)

        assert captured["limit"] == MAX_LISTED_RUNS
        assert captured["since"] is not None
        assert "Warning: stopped" not in capsys.readouterr().err

    def test_last_still_caps_a_period(self, monkeypatch, capsys):
        captured = self.run(monkeypatch, ["--since", "30d", "--last", "40"], count=120)

        assert captured["limit"] == 40
        assert "Warning: stopped at 40 runs, as --last asks; the analysis covers 2026-10-01 00:00 UTC onwards." \
            in capsys.readouterr().err

    def test_a_period_beyond_what_github_lists_is_flagged(self, monkeypatch, capsys):
        """Seen on pydantic: a week holds 1682 runs, the listing stops at 1000."""
        self.run(monkeypatch, ["--since", "1w"], count=1682)

        assert (
            f"Warning: the period holds 1682 runs, but GitHub lists only the newest {MAX_LISTED_RUNS}; "
            "the analysis covers 2026-10-01 00:00 UTC onwards."
        ) in capsys.readouterr().err

    def test_without_a_period_the_default_is_unchanged(self, monkeypatch):
        captured = self.run(monkeypatch, [], count=120)

        assert captured["limit"] == 50
        assert captured["since"] is None
