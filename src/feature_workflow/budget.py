"""How many review rounds a feature is worth — pure sizing logic.

A review round is a full independent sweep of the PR by a subagent: the most expensive thing
this workflow does. So the number of rounds is bounded, and the bound scales with the risk the
diff actually carries — a two-file tweak does not deserve the same spend as a 900-line change
to authentication. The budget is what turns "review until clean" (unbounded) into "review until
clean or until the budget says a human should decide" (bounded).

Sizing inputs are deliberately cheap to obtain: the PR's own diff stats, plus whether it touches
paths the repo has declared sensitive (`git config --add feature.sensitive-path '<glob>'`).
Anything an agent can't compute from the diff — "this feature matters more than its size
suggests" — is a human override: `feature budget --set <n>`.
"""

from fnmatch import fnmatch

BASE_ROUNDS = 2
MAX_ROUNDS = 4
LARGE_FILES = 15
LARGE_LINES = 500


def sensitive_matches(paths: list[str], patterns: list[str]) -> list[str]:
    """Changed paths matching any configured sensitive glob.

    Matching is `fnmatch`, which is not path-aware: `*` crosses `/`, so `src/auth/*` also
    matches `src/auth/oauth/token.py`. That is the behavior we want for "anything under here".
    """
    return [p for p in paths if any(fnmatch(p, pattern) for pattern in patterns)]


def auto_rounds(*, changed_files: int, changed_lines: int, sensitive_hits: list[str]) -> tuple[int, list[str]]:
    """Auto-size the review budget from diff shape. Returns (rounds, why-lines)."""
    rounds = BASE_ROUNDS
    why = [f"base {BASE_ROUNDS} round(s)"]

    if changed_files > LARGE_FILES or changed_lines > LARGE_LINES:
        rounds += 1
        why.append(f"+1 large diff ({changed_files} files, {changed_lines} lines)")
    if sensitive_hits:
        shown = ", ".join(sensitive_hits[:3]) + (" …" if len(sensitive_hits) > 3 else "")
        rounds += 1
        why.append(f"+1 touches sensitive path(s): {shown}")

    # Ceiling, not a live branch: the bumps above happen to add up to exactly MAX_ROUNDS today. It
    # stays as the one place a future bump can't quietly escape, and `feature budget --set` is the
    # documented way past it.
    return min(rounds, MAX_ROUNDS), why
