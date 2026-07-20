#!/usr/bin/env python3
"""SessionStart hook: inject feature context for the current branch into the session.

Wire it in .claude/settings.json:

  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command",
        "command": "uv run python -m feature_workflow.session_start_hook"}]}
    ]
  }

The hook prints a short context block to stdout (which Claude Code injects into context).
If the branch isn't a tracked feature, it prints nothing and exits 0 — a non-feature branch
is a normal case, not an error.
"""

import io
import subprocess
from contextlib import redirect_stdout

from .__main__ import cmd_status
from .gitwiring import current_branch, feature_issue


class _Args:
    branch = None
    json = False


def main() -> None:
    # The only "expected absent" case is running outside a git repo: `git rev-parse` exits
    # non-zero. That's not an error for a hook that may fire anywhere, so we return quietly.
    # ANY OTHER failure (broken wiring, gh unauthenticated, malformed state) propagates and
    # aborts loudly — per repo policy we never hide problems behind a blanket catch.
    try:
        branch = current_branch()
    except subprocess.CalledProcessError:
        return  # not inside a git repo; nothing to inject

    if feature_issue(branch) is None:
        return  # not a tracked feature; nothing to inject

    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_status(_Args())

    print("<feature-context>")
    print(buf.getvalue().rstrip())
    print("</feature-context>")


if __name__ == "__main__":
    main()
