"""Tests for the feature-state block parser/replacer — the deterministic core."""

import pytest

from feature_workflow.state import (
    BEGIN,
    END,
    SCHEMA,
    migrate,
    new_state,
    parse_block,
    render_block,
    replace_block,
    set_status,
)


def test_round_trip():
    state = new_state(branch="feat-x", base="main", updated="2026-07-13T00:00:00Z")
    body = f"## Title\n\nblurb\n\n{render_block(state)}\n"
    assert parse_block(body) == state


def test_replace_is_idempotent_and_preserves_human_text():
    state = new_state(branch="feat-x", base="main", updated="t0")
    body = f"## Title\n\nHuman notes here.\n\n{render_block(state)}\n\nMore human text.\n"

    state["status"] = "in-review"
    state["pr"] = 123
    updated = replace_block(body, state)

    # Human text on both sides survives.
    assert "Human notes here." in updated
    assert "More human text." in updated
    # Exactly one block remains.
    assert updated.count(BEGIN) == 1
    assert updated.count(END) == 1
    # New values are parsed back.
    parsed = parse_block(updated)
    assert parsed["status"] == "in-review"
    assert parsed["pr"] == 123

    # Replacing again yields the same result (idempotent).
    assert replace_block(updated, state) == updated


def test_replace_appends_when_no_block_present():
    state = new_state(branch="feat-x", base="main", updated="t0")
    body = "## Just a human issue\n\nNo machine block yet.\n"
    updated = replace_block(body, state)
    assert "No machine block yet." in updated
    assert parse_block(updated)["branch"] == "feat-x"


def test_parse_missing_block_aborts():
    with pytest.raises(ValueError, match="No FEATURE-STATE block"):
        parse_block("## Title\n\njust prose, no block\n")


def test_parse_block_without_json_fence_aborts():
    body = f"{BEGIN}\nno json here\n{END}"
    with pytest.raises(ValueError, match="no ```json fence"):
        parse_block(body)


def test_parse_rejects_a_stale_schema_with_the_fix():
    stale = {"schema": 1, "branch": "feat-x"}
    body = render_block(stale)
    with pytest.raises(ValueError, match="feature migrate"):
        parse_block(body)


def test_set_status_rejects_an_unknown_status():
    state = new_state(branch="feat-x", base="main", updated="t0")
    set_status(state, "needs-decision")
    assert state["status"] == "needs-decision"
    with pytest.raises(ValueError, match="Unknown status"):
        set_status(state, "almost-done")


SCHEMA_1_STATE = {
    "schema": 1,
    "branch": "feat-x",
    "base": "main",
    "pr": 123,
    "status": "in-review",
    "review_runs": 2,
    "last_prompt": "do the thing",
    "last_review": {"run": 2, "sha": "abc1234", "new_problems": 3, "summary": "found three"},
    "updated": "t0",
}


def test_migrate_treats_old_findings_as_blocking():
    """Schema 1 had no severity split; assuming blocking keeps a closed gate closed."""
    migrated = migrate(dict(SCHEMA_1_STATE))
    assert migrated["schema"] == SCHEMA
    assert migrated["last_review"]["new_blocking"] == 3
    assert migrated["last_review"]["regressions"] == 0
    assert migrated["review_history"] == [migrated["last_review"]]
    assert migrated["review_budget"] is None
    # Untouched fields survive.
    assert migrated["pr"] == 123
    assert migrated["last_prompt"] == "do the thing"
    assert parse_block(render_block(migrated)) == migrated


def test_migrate_with_no_prior_review():
    migrated = migrate(dict(SCHEMA_1_STATE) | {"last_review": None})
    assert migrated["last_review"] is None
    assert migrated["review_history"] == []


def test_migrate_is_idempotent_and_refuses_unknown_versions():
    current = new_state(branch="feat-x", base="main", updated="t0")
    assert migrate(current) == current
    with pytest.raises(ValueError, match="Cannot migrate"):
        migrate({"schema": 99})
