"""The feature-state JSON block embedded in a feature issue body.

The block is delimited by HTML-comment markers so it can be located and replaced
idempotently regardless of what humans write around it. We never blind-string-replace.
"""

import json
import re

BEGIN = "<!--FEATURE-STATE:BEGIN-->"
END = "<!--FEATURE-STATE:END-->"
SCHEMA = 1

VALID_STATUS = {"planning", "in-progress", "in-review", "ready", "merged"}

# Non-greedy match of everything between the markers, across newlines.
_BLOCK_RE = re.compile(re.escape(BEGIN) + r"(.*?)" + re.escape(END), re.DOTALL)
_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def render_block(state: dict) -> str:
    """Render the full marker-delimited block for a state dict."""
    body = json.dumps(state, indent=2, sort_keys=False)
    return f"{BEGIN}\n```json\n{body}\n```\n{END}"


def parse_block(issue_body: str) -> dict:
    """Extract and parse the state dict from an issue body. Aborts if absent/malformed."""
    block_match = _BLOCK_RE.search(issue_body)
    if not block_match:
        raise ValueError("No FEATURE-STATE block found in issue body")
    json_match = _JSON_RE.search(block_match.group(1))
    if not json_match:
        raise ValueError("FEATURE-STATE block present but contains no ```json fence")
    return json.loads(json_match.group(1))


def replace_block(issue_body: str, state: dict) -> str:
    """Return `issue_body` with its state block replaced by the rendered `state`.

    If no block exists yet, append one. Idempotent w.r.t. surrounding human-written text.
    """
    new_block = render_block(state)
    if _BLOCK_RE.search(issue_body):
        return _BLOCK_RE.sub(lambda _m: new_block, issue_body, count=1)
    sep = "" if issue_body.endswith("\n") else "\n"
    return f"{issue_body}{sep}\n{new_block}\n"


def new_state(*, branch: str, base: str, updated: str) -> dict:
    """Initial state for a freshly created feature."""
    return {
        "schema": SCHEMA,
        "branch": branch,
        "base": base,
        "pr": None,
        "status": "planning",
        "review_runs": 0,
        "last_prompt": None,
        "last_review": None,
        "updated": updated,
    }
