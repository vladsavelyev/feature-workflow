"""Tests for git wiring helpers with branching logic worth pinning."""

from feature_workflow import gitwiring


def test_local_only_commits_empty_when_no_upstream(monkeypatch):
    # `git for-each-ref ... %(upstream:short)` prints nothing when the base has no upstream —
    # a legitimate "in sync / nothing to compare", not an error.
    monkeypatch.setattr(gitwiring, "run", lambda cmd, cwd=None: "")
    assert gitwiring.local_only_commits("main") == []


def test_local_only_commits_empty_when_in_sync(monkeypatch):
    def fake_run(cmd, cwd=None):
        if "for-each-ref" in cmd:
            return "origin/main"
        if "rev-list" in cmd:
            return ""  # no commits ahead of upstream
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(gitwiring, "run", fake_run)
    assert gitwiring.local_only_commits("main") == []


def test_local_only_commits_lists_unpushed(monkeypatch):
    def fake_run(cmd, cwd=None):
        if "for-each-ref" in cmd:
            return "origin/main"
        if "rev-list" in cmd:
            return "abc1234\ndef5678\n0011223"
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(gitwiring, "run", fake_run)
    assert gitwiring.local_only_commits("main") == ["abc1234", "def5678", "0011223"]
