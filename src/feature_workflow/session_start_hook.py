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
from .state import StaleSchema


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
    try:
        with redirect_stdout(buf):
            cmd_status(_Args())
    except StaleSchema as exc:
        # A state block this CLI doesn't understand is the one failure with a known, one-command fix,
        # and it fires on EVERY session start until someone applies it. Surfacing the message (which
        # names `feature migrate`) beats a traceback in the session-context hook. Caught by its own
        # exception type, not by `ValueError`: `cmd_status` also makes several `gh` calls whose
        # parsing raises plain ValueErrors, and reporting those as "run feature migrate" would be
        # advice that cannot fix them. Nothing else is softened.
        print("<feature-context>")
        print(f"This branch is a TRACKED FEATURE, but its state could not be read: {exc}")
        print("Run `feature migrate` on this branch before using any other `feature` command.")
        print("</feature-context>")
        return

    print("<feature-context>")
    print(buf.getvalue().rstrip())
    print()
    # Steer review requests into the tracked flow. Without this, a bare "run a review" on a
    # tracked branch gets served by whatever generic reviewer the model reaches for first — it
    # never loads the feature-workflow skill, so the finding never becomes a tracked problem and
    # the merge gate never moves. Naming the skill explicitly (not just its trigger phrases)
    # makes the routing deterministic regardless of skill auto-matching.
    print(
        "This branch is a TRACKED FEATURE. Any request to review it, record a review, file a "
        "problem, check the merge gate, or resume its status MUST go through the "
        "`feature-workflow` skill — invoke `Skill(feature-workflow)` and follow it. In "
        "particular, 'run a review' / 'review this' means the feature-workflow review flow "
        "(finder subagent → dedup → `feature problem add` → `feature review record`), NOT a "
        "standalone code reviewer and NOT a search for a project-local `code-review` skill."
    )
    # A merged PR whose feature never closed out is the one state where the gate's advice is moot,
    # and the branch you're on is the last place anyone will notice — after this session it gets
    # deleted and the tracking issue is orphaned for good. Say so before the review instructions.
    print(
        "If the status above warns that the PR is MERGED but the feature was never closed out, do "
        "that FIRST: `feature merge` (or `feature reconcile` to sweep the whole repo). Reviewing a "
        "merged feature is not useful; recording what shipped is."
    )
    # The gate line above already says what to do; make the stop condition explicit, because the
    # failure mode this replaced was an agent reviewing round after round chasing a clean sweep.
    print(
        "The gate above is authoritative about whether to review again: REVIEW_AGAIN means fix "
        "the blocking problems and run ONE more review round; NEEDS_DECISION means STOP "
        "reviewing and `feature escalate --reason '<recommendation>'` for a human. Only "
        "high/med problems block — never downgrade a severity to open the gate, use "
        "`feature problem defer <#> --reason '<why>'` so the decision is recorded."
    )
    print("</feature-context>")


if __name__ == "__main__":
    main()
