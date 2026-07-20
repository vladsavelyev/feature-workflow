"""Tests for the feature-state block parser/replacer — the deterministic core."""

import pytest

from feature_workflow.state import (
    BEGIN,
    END,
    new_state,
    parse_block,
    render_block,
    replace_block,
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
