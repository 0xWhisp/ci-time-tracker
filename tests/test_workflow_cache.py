"""Workflow files are cached by the commit they were read at, so an analysis
whose runs are all cached makes no request for them either."""

import base64
from datetime import datetime, timedelta, timezone

from ci_time_tracker.cache import RunCache
from ci_time_tracker.cli import _fetch_workflows, _workflow_refs
from ci_time_tracker.github_api import GitHubClient
from ci_time_tracker.models import BuildLog

START = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
PATH = ".github/workflows/ci.yml"
YAML = "jobs:\n  test:\n    steps:\n      - run: pip install .\n"


def log(index, sha="", path=PATH, head_repository="acme/web"):
    return BuildLog(
        build_id=str(index),
        head_sha=sha or f"{index:040x}",
        timestamp=START + timedelta(hours=index),
        metadata={"workflow_path": path, "head_repository": head_repository},
    )


class Recorder:
    """Stands in for GitHubClient(repo, token) and records the URLs asked for."""

    urls: list[str] = []

    def __new__(cls, repo, token=None):
        def transport(url, headers):
            Recorder.urls.append(url)
            return {"encoding": "base64", "content": base64.b64encode(YAML.encode()).decode()}, {}

        return GitHubClient(repo=repo, transport=transport)


class TestCacheTable:
    def test_a_file_is_cached_per_commit(self, tmp_path):
        with RunCache(tmp_path / "runs.db") as cache:
            cache.store_workflow_file("acme/web", PATH, "abc", YAML)

            assert cache.get_workflow_file("acme/web", PATH, "abc") == YAML
            assert cache.get_workflow_file("acme/web", PATH, "def") is None
            assert cache.get_workflow_file("acme/api", PATH, "abc") is None

    def test_an_existing_database_gains_the_table(self, tmp_path):
        RunCache(tmp_path / "runs.db").close()

        with RunCache(tmp_path / "runs.db") as cache:
            cache.store_workflow_file("acme/web", PATH, "abc", YAML)
            assert cache.get_workflow_file("acme/web", PATH, "abc") == YAML


class TestWorkflowRefs:
    def test_the_newest_run_picks_the_commit(self):
        refs = _workflow_refs("acme/web", [log(1, "a" * 40), log(3, "c" * 40), log(2, "b" * 40)])

        assert refs == {PATH: "c" * 40}

    def test_a_fork_commit_is_not_used(self):
        refs = _workflow_refs("acme/web", [log(1, "a" * 40), log(2, "f" * 40, head_repository="fork/web")])

        assert refs == {PATH: "a" * 40}

    def test_only_fork_commits_read_the_default_branch(self):
        assert _workflow_refs("acme/web", [log(1, head_repository="fork/web")]) == {PATH: None}

    def test_runs_without_a_workflow_file_are_ignored(self):
        assert _workflow_refs("acme/web", [log(1, path="dynamic/dependabot/x")]) == {}


class TestFetchWorkflows:
    def test_a_second_analysis_makes_no_request(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ci_time_tracker.cli.GitHubClient", Recorder)
        Recorder.urls = []
        logs = [log(1, "a" * 40)]

        with RunCache(tmp_path / "runs.db") as cache:
            first = _fetch_workflows("acme/web", logs, cache)
            second = _fetch_workflows("acme/web", logs, cache)

        assert first == second
        assert PATH in first
        assert len(Recorder.urls) == 1
        assert "ref=" + "a" * 40 in Recorder.urls[0]

    def test_a_new_commit_is_fetched_again(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ci_time_tracker.cli.GitHubClient", Recorder)
        Recorder.urls = []

        with RunCache(tmp_path / "runs.db") as cache:
            _fetch_workflows("acme/web", [log(1, "a" * 40)], cache)
            _fetch_workflows("acme/web", [log(1, "a" * 40), log(2, "b" * 40)], cache)

        assert len(Recorder.urls) == 2

    def test_the_default_branch_is_never_cached(self, tmp_path, monkeypatch):
        monkeypatch.setattr("ci_time_tracker.cli.GitHubClient", Recorder)
        Recorder.urls = []
        logs = [log(1, head_repository="fork/web")]

        with RunCache(tmp_path / "runs.db") as cache:
            _fetch_workflows("acme/web", logs, cache)
            _fetch_workflows("acme/web", logs, cache)

        assert len(Recorder.urls) == 2
        assert "ref=" not in Recorder.urls[0]

    def test_without_a_cache_files_are_still_read(self, monkeypatch):
        monkeypatch.setattr("ci_time_tracker.cli.GitHubClient", Recorder)
        Recorder.urls = []

        assert PATH in _fetch_workflows("acme/web", [log(1, "a" * 40)])
