"""The feature-state JSON block embedded in a feature issue body.

The block is delimited by HTML-comment markers so it can be located and replaced
idempotently regardless of what humans write around it. We never blind-string-replace.
"""

import json
import re

BEGIN = "<!--FEATURE-STATE:BEGIN-->"
END = "<!--FEATURE-STATE:END-->"
SCHEMA = 2

VALID_STATUS = {"planning", "in-progress", "in-review", "needs-decision", "ready", "merged"}

# Non-greedy match of everything between the markers, across newlines.
_BLOCK_RE = re.compile(re.escape(BEGIN) + r"(.*?)" + re.escape(END), re.DOTALL)
_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def render_block(state: dict) -> str:
    """Render the full marker-delimited block for a state dict."""
    body = json.dumps(state, indent=2, sort_keys=False)
    return f"{BEGIN}\n```json\n{body}\n```\n{END}"


def parse_block_raw(state_body: str) -> dict:
    """Extract and parse the state dict *without* checking its schema version.

    Only the migration path should use this — everything else wants `parse_block`, which refuses
    to operate on a state block it doesn't understand.
    """
    block_match = _BLOCK_RE.search(state_body)
    if not block_match:
        raise ValueError("No FEATURE-STATE block found in issue body")
    json_match = _JSON_RE.search(block_match.group(1))
    if not json_match:
        raise ValueError("FEATURE-STATE block present but contains no ```json fence")
    return json.loads(json_match.group(1))


def parse_block(issue_body: str) -> dict:
    """Extract and parse the state dict from an issue body. Aborts if absent/malformed/stale.

    A version mismatch aborts with the fix rather than limping along on missing keys: reading a
    schema-1 block as schema 2 would mean guessing at review counts the old block never recorded.
    """
    state = parse_block_raw(issue_body)
    version = state.get("schema")
    if version != SCHEMA:
        raise ValueError(
            f"Feature state is schema {version}, but this CLI speaks schema {SCHEMA}. "
            f"Run `feature migrate` to upgrade this feature issue's state block."
        )
    return state


def replace_block(issue_body: str, state: dict) -> str:
    """Return `issue_body` with its state block replaced by the rendered `state`.

    If no block exists yet, append one. Idempotent w.r.t. surrounding human-written text.
    """
    new_block = render_block(state)
    if _BLOCK_RE.search(issue_body):
        return _BLOCK_RE.sub(lambda _m: new_block, issue_body, count=1)
    sep = "" if issue_body.endswith("\n") else "\n"
    return f"{issue_body}{sep}\n{new_block}\n"


def set_status(state: dict, status: str) -> None:
    """Move the feature's status, rejecting anything outside the known lifecycle."""
    if status not in VALID_STATUS:
        raise ValueError(f"Unknown status '{status}'; expected one of {sorted(VALID_STATUS)}")
    state["status"] = status


def new_state(*, branch: str, base: str, updated: str) -> dict:
    """Initial state for a freshly created feature."""
    return {
        "schema": SCHEMA,
        "branch": branch,
        "base": base,
        "pr": None,
        "status": "planning",
        # None = auto-size the review budget from the PR diff at query time, so it tracks the
        # diff as it grows instead of freezing a guess made when the branch was empty.
        "review_budget": None,
        "review_runs": 0,
        # Every review run ever, plus `{"event": ...}` markers when a base sync invalidates the
        # runs before it. The gate reads the trend from here; `last_review` is only the currently
        # valid clean-pass evidence (nulled on invalidation).
        "review_history": [],
        "last_prompt": None,
        "last_review": None,
        "updated": updated,
    }


def review_entry(*, run: int, sha: str, new_blocking: int, new_low: int, regressions: int, summary: str) -> dict:
    """One review run's record, as stored in `review_history` and `last_review`."""
    return {
        "run": run,
        "sha": sha,
        "new_blocking": new_blocking,
        "new_low": new_low,
        "regressions": regressions,
        "summary": summary,
    }


def migrate(state: dict) -> dict:
    """Upgrade a state dict to the current schema. One-way, and only 1 → 2 exists.

    Delete this once no schema-1 feature issues remain in flight; it exists so the severity-aware
    gate could land without bricking features mid-review, not as a permanent compatibility layer.
    """
    version = state.get("schema")
    if version == SCHEMA:
        return state
    if version != 1:
        raise ValueError(f"Cannot migrate feature state from schema {version} to {SCHEMA}")

    old = state.pop("last_review")
    state["schema"] = SCHEMA
    state["review_budget"] = None
    # Schema 1 had a single `new_problems` count with no severity split. Treat every one of them
    # as blocking: that keeps a gate that was closed still closed, where the reverse guess could
    # open it on unreviewed evidence.
    if old is None:
        state["review_history"] = []
        state["last_review"] = None
    else:
        entry = review_entry(
            run=old["run"],
            sha=old["sha"],
            new_blocking=old["new_problems"],
            new_low=0,
            regressions=0,
            summary=old["summary"],
        )
        state["review_history"] = [entry]
        state["last_review"] = entry
    return state
