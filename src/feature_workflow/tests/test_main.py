"""Tests for the command-layer logic worth pinning: the merge close-out's ordering and wording."""

import pytest

from feature_workflow import __main__ as cli
from feature_workflow import github, problems
from feature_workflow.shell import CommandFailed
from feature_workflow.state import SCHEMA, new_state, render_block


def _problem(number: int, sev: str, *, state: str = "OPEN", extra: tuple[str, ...] = ()) -> dict:
    return {"number": number, "title": f"problem {number}", "state": state, "labels": [sev, *extra]}


def _merged_state() -> dict:
    state = new_state(branch="add-oauth", base="main", updated="2026-08-21T00:00:00Z")
    state["pr"] = 123
    state["status"] = "in-review"
    return state


def _stub_github(monkeypatch, *, calls: list[str], failing: str | None = None) -> None:
    def record(name):
        def fake(*_args, **_kwargs):
            calls.append(name)
            if name == failing:
                raise CommandFailed(1, ["gh"], "", "boom")
            return ""

        return fake

    monkeypatch.setattr(github, "comment", record("comment"))
    monkeypatch.setattr(github, "close_issue", record("close_issue"))
    monkeypatch.setattr(github, "set_issue_body", record("set_issue_body"))
    monkeypatch.setattr(github, "get_issue_body", lambda number: render_block(_merged_state()))


def test_record_merged_closes_then_writes_state(monkeypatch):
    calls: list[str] = []
    _stub_github(monkeypatch, calls=calls)
    state = _merged_state()
    cli._record_merged(42, state, "✅ Merged.", already_closed=False)
    # The state write is LAST, and the close carries the note (one call, not close + comment).
    assert calls == ["close_issue", "set_issue_body"]
    assert state["status"] == "merged"


def test_record_merged_only_comments_when_already_closed(monkeypatch):
    # The PR's `Closes #<issue>` reference usually closes the issue seconds before anyone runs the
    # close-out; `gh issue close` on a closed issue is not the way to find that out.
    calls: list[str] = []
    _stub_github(monkeypatch, calls=calls)
    cli._record_merged(42, _merged_state(), "✅ Merged.", already_closed=True)
    assert calls == ["comment", "set_issue_body"]


def test_record_merged_leaves_state_unwritten_if_closing_fails(monkeypatch):
    # ORDER IS THE RETRY STRATEGY: a feature written as `merged` whose issue is still open would be
    # skipped by every future `feature reconcile` — orphaned permanently. Failing before the write
    # means a re-run does the whole thing again.
    calls: list[str] = []
    _stub_github(monkeypatch, calls=calls, failing="close_issue")
    state = _merged_state()
    with pytest.raises(CommandFailed):
        cli._record_merged(42, state, "✅ Merged.", already_closed=False)
    assert "set_issue_body" not in calls
    assert state["status"] == "in-review"


def test_reconcile_note_names_blocking_problems_that_shipped():
    triaged = problems.triage([_problem(7, "sev:high"), _problem(8, "sev:low")])
    note = cli._reconcile_note(123, triaged)
    # Recorded after the fact means the gate never vetted this merge — say so, with the numbers.
    assert "#7 [sev:high]" in note
    assert "1 blocking problem(s) were still open" in note
    assert "#8 [sev:low/open]" in note


def test_reconcile_note_is_plain_when_nothing_was_blocking():
    triaged = problems.triage([_problem(8, "sev:low")])
    note = cli._reconcile_note(123, triaged)
    assert "were still open" not in note
    assert "⚠️" not in note
    assert "#8 [sev:low/open]" in note


def test_state_block_of_a_fresh_feature_is_current_schema():
    # Guards the assumption `reconcile` leans on when it reads a state block without a schema check.
    assert new_state(branch="b", base="main", updated="now")["schema"] == SCHEMA
