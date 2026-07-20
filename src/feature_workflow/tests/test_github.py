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


def _fake_pr_body_editor(monkeypatch, body: str) -> dict:
    """Stub github.run so `gh pr view` returns `body` and `gh pr edit` captures the new body."""
    captured: dict = {}

    def fake_run(cmd, *, input_text=None):
        if cmd[:3] == ["gh", "pr", "view"]:
            return body
        if cmd[:3] == ["gh", "pr", "edit"]:
            captured["body"] = input_text
            return ""
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(github, "run", fake_run)
    return captured


def test_link_pr_appends_reference_to_body(monkeypatch):
    captured = _fake_pr_body_editor(monkeypatch, "Some PR description.")
    assert github.link_pr_to_feature(1910, 1964) is True
    assert captured["body"] == "Some PR description.\n\nPart of #1964\n"


def test_link_pr_is_idempotent_when_already_linked(monkeypatch):
    captured = _fake_pr_body_editor(monkeypatch, "Desc.\n\nPart of #1964\n")
    # Already references this issue — no edit, no duplicate line.
    assert github.link_pr_to_feature(1910, 1964) is False
    assert "body" not in captured


def test_link_pr_replaces_stale_reference(monkeypatch):
    captured = _fake_pr_body_editor(monkeypatch, "Desc.\n\nPart of #1981\n")
    # Re-linking to a different issue drops the old reference rather than stacking a second one.
    assert github.link_pr_to_feature(1910, 1964) is True
    assert captured["body"] == "Desc.\n\nPart of #1964\n"
    assert "#1981" not in captured["body"]


def test_link_pr_handles_empty_body(monkeypatch):
    captured = _fake_pr_body_editor(monkeypatch, "")
    assert github.link_pr_to_feature(1910, 1964) is True
    assert captured["body"] == "Part of #1964\n"
