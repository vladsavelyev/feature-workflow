"""Tests for the GitHub layer helpers that have branching logic worth pinning."""

from feature_workflow import github


def test_open_pr_for_branch_returns_number_when_present(monkeypatch):
    monkeypatch.setattr(github, "run", lambda cmd: "1910")
    assert github.open_pr_for_branch("repl-persistent-kernel") == 1910


def test_open_pr_for_branch_returns_none_when_no_pr(monkeypatch):
    # `gh pr list ... --jq '.[0].number // empty'` prints nothing when no PR matches;
    # that empty result is a real "no PR", not a swallowed error.
    monkeypatch.setattr(github, "run", lambda cmd: "")
    assert github.open_pr_for_branch("some-branch") is None
