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


REF = github.feature_ref(1964)


def test_link_pr_appends_closing_reference_to_body(monkeypatch):
    captured = _fake_pr_body_editor(monkeypatch, "Some PR description.")
    assert github.link_pr_to_feature(1910, 1964) is True
    # A closing keyword, so merging the PR closes the tracking issue with nobody remembering to.
    assert captured["body"] == f"Some PR description.\n\n{REF}\n"
    assert captured["body"].startswith("Some PR description.\n\nCloses #1964 ")


def test_link_pr_is_idempotent_when_already_linked(monkeypatch):
    captured = _fake_pr_body_editor(monkeypatch, f"Desc.\n\n{REF}\n")
    # Already references this issue — no edit, no duplicate line.
    assert github.link_pr_to_feature(1910, 1964) is False
    assert "body" not in captured


def test_link_pr_replaces_stale_reference(monkeypatch):
    captured = _fake_pr_body_editor(monkeypatch, f"Desc.\n\n{github.feature_ref(1981)}\n")
    # Re-linking to a different issue drops the old reference rather than stacking a second one.
    assert github.link_pr_to_feature(1910, 1964) is True
    assert captured["body"] == f"Desc.\n\n{REF}\n"
    assert "#1981" not in captured["body"]


def test_link_pr_upgrades_the_old_non_closing_reference(monkeypatch):
    # PRs opened before the reference became a closing one carry `Part of #<issue>`; linking again
    # replaces that line instead of leaving two references to the same issue behind.
    captured = _fake_pr_body_editor(monkeypatch, "Desc.\n\nPart of #1964\n")
    assert github.link_pr_to_feature(1910, 1964) is True
    assert captured["body"] == f"Desc.\n\n{REF}\n"
    assert "Part of" not in captured["body"]


def test_link_pr_leaves_hand_written_problem_closures_alone(monkeypatch):
    # The whole point of the marker: a PR body's own `Closes #<problem>` lines — including a lone
    # one on its own line — must survive re-linking, or re-running `feature pr` would silently stop
    # those problem sub-issues from auto-closing.
    captured = _fake_pr_body_editor(monkeypatch, "Desc.\n\nCloses #2173\n")
    assert github.link_pr_to_feature(1910, 1964) is True
    assert captured["body"] == f"Desc.\n\nCloses #2173\n\n{REF}\n"


def test_link_pr_handles_empty_body(monkeypatch):
    captured = _fake_pr_body_editor(monkeypatch, "")
    assert github.link_pr_to_feature(1910, 1964) is True
    assert captured["body"] == f"{REF}\n"


def test_pr_states_batches_into_one_aliased_query(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "repo", "view"]:
            return "acme\tnavari"
        return '{"p2494": {"number": 2494, "state": "MERGED"}, "p2506": {"number": 2506, "state": "OPEN"}}'

    monkeypatch.setattr(github, "run", fake_run)
    assert github.pr_states([2506, 2494, 2506]) == {2494: "MERGED", 2506: "OPEN"}
    # One repo lookup + ONE graphql call for both PRs (a `gh pr view` each is what made the sweep
    # too slow to run from a hook).
    assert len(calls) == 2
    query = next(arg for arg in calls[1] if arg.startswith("query="))
    assert "p2494: pullRequest(number:2494)" in query
    assert "p2506: pullRequest(number:2506)" in query


def test_pr_states_asks_nothing_when_given_nothing(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise AssertionError(f"should not have run: {cmd}")

    monkeypatch.setattr(github, "run", fake_run)
    assert github.pr_states([]) == {}


def test_feature_issues_flattens_paginated_pages(monkeypatch):
    # `gh api graphql --paginate --jq` prints one JSON array per page; every page must land in the
    # result, or the oldest features stay invisible to the reconcile sweep forever.
    page1 = '[{"number": 1, "title": "A", "state": "CLOSED", "body": "x"}]'
    page2 = '[{"number": 2, "title": "B", "state": "OPEN", "body": "y"}]'

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "repo", "view"]:
            return "acme\tnavari"
        assert "--paginate" in cmd
        return f"{page1}\n{page2}"

    monkeypatch.setattr(github, "run", fake_run)
    assert [i["number"] for i in github.feature_issues()] == [1, 2]
