"""Git/git-town wiring: branches, worktrees, parent pointers, and the branch->issue link.

All per-branch wiring lives in git config so a fresh session reconstructs it exactly:
  git-town-branch.<branch>.parent   -> base branch (owned by git-town)
  branch.<branch>.feature-issue     -> feature issue number (owned by us)
"""

from pathlib import Path

from .shell import run, try_run

WORKTREE_ROOT = ".claude/worktrees"


def current_branch(cwd: str | None = None) -> str:
    return run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)


def repo_root(cwd: str | None = None) -> str:
    return run(["git", "rev-parse", "--show-toplevel"], cwd=cwd)


def main_checkout_root(cwd: str | None = None) -> str:
    """The MAIN working tree's root, regardless of which worktree we're called from.

    `--show-toplevel` returns the *current* worktree, so placing new worktrees relative to it
    nests them inside whichever feature worktree you're standing in. The common git dir
    (`--git-common-dir`) always points at the main checkout's `.git`; its parent is the main
    working tree. This keeps `.claude/worktrees/<name>` a flat sibling layout under main.
    """
    common_dir = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd)
    return str(Path(common_dir).parent)


def run_head_sha(cwd: str | None = None) -> str:
    return run(["git", "rev-parse", "--short", "HEAD"], cwd=cwd)


def worktree_for_branch(branch: str, cwd: str | None = None) -> str | None:
    """Absolute path of the worktree that has `branch` checked out, or None if none does.

    A reviewer must read the branch's code from ITS worktree — the feature worktree lives under
    the main checkout (`.claude/worktrees/<name>`), so the main checkout's path is a prefix of it
    and holds the SAME files on a DIFFERENT branch. Reading from the wrong one reviews the wrong
    code. `git worktree list --porcelain` emits `worktree <path>` then `branch refs/heads/<name>`
    per entry."""
    out = run(["git", "worktree", "list", "--porcelain"], cwd=cwd)
    path: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def diff_against(base: str, cwd: str | None = None) -> str:
    """The branch's diff vs the merge-base with `base` (what a reviewer should look at)."""
    merge_base = run(["git", "merge-base", "HEAD", base], cwd=cwd)
    return run(["git", "diff", f"{merge_base}...HEAD"], cwd=cwd)


def git_town_sync(stack: bool = False, cwd: str | None = None) -> str:
    """Sync the current branch via git-town: recursively pull ancestor branches, merge the
    parent down, and push. `stack=True` syncs the whole stack the branch belongs to.

    We delegate entirely to git-town (the maintained stacked-branch tool) rather than
    reimplement recursive restacking. Runs with a clean, non-interactive flag set; a merge
    CONFLICT makes git-town exit non-zero, which propagates loudly — we do NOT auto-resolve,
    leaving the user to fix it in the worktree (fail-fast, per repo policy).
    """
    cmd = ["git", "town", "sync", "--no-detached"]
    if stack:
        cmd.append("--stack")
    return run(cmd, cwd=cwd)


def parent_branch(branch: str, cwd: str | None = None) -> str | None:
    """The base branch for a stacked feature, per git-town. None if git-town isn't tracking it."""
    return try_run(["git", "config", f"git-town-branch.{branch}.parent"], cwd=cwd)


def set_parent_branch(branch: str, base: str, cwd: str | None = None) -> None:
    """Record the git-town parent pointer for a branch (the base a session bootstraps from)."""
    run(["git", "config", f"git-town-branch.{branch}.parent", base], cwd=cwd)


def feature_issue(branch: str, cwd: str | None = None) -> int | None:
    raw = try_run(["git", "config", f"branch.{branch}.feature-issue"], cwd=cwd)
    return int(raw) if raw else None


def set_feature_issue(branch: str, issue: int, cwd: str | None = None) -> None:
    run(["git", "config", f"branch.{branch}.feature-issue", str(issue)], cwd=cwd)


def local_only_commits(base: str, cwd: str | None = None) -> list[str]:
    """Short SHAs on local `base` that its upstream remote branch does not contain.

    Empty when `base` is in sync with its remote, has no upstream, or doesn't exist locally.
    These commits would leak into the PR diff — which is computed against the *remote* base —
    of every branch cut from `base`, so `feature create` refuses to branch off a non-empty
    result. `git for-each-ref` reports an absent upstream as an empty field (exit 0), so
    "no upstream" is a legitimate empty answer, not an error to swallow.
    """
    upstream = run(["git", "for-each-ref", "--format=%(upstream:short)", f"refs/heads/{base}"], cwd=cwd)
    if not upstream:
        return []
    out = run(["git", "rev-list", "--abbrev-commit", f"{upstream}..{base}"], cwd=cwd)
    return out.splitlines() if out else []


def sensitive_patterns(cwd: str | None = None) -> list[str]:
    """Globs the repo has declared review-sensitive, from git config (multi-valued):

        git config --add feature.sensitive-path 'src/auth/*'

    A PR touching one of these earns an extra review round (see `budget.py`). An unset key is a
    legitimate "this repo declared none" — `git config` exits 1 for a missing key, which
    `try_run` reports as None — not a swallowed failure.
    """
    raw = try_run(["git", "config", "--get-all", "feature.sensitive-path"], cwd=cwd)
    return raw.splitlines() if raw else []


def create_branch_and_worktree(name: str, base: str, cwd: str | None = None) -> str:
    """Create branch `name` off `base` in a worktree under WORKTREE_ROOT/<name>.

    Registers the parent pointer with git-town so the stack is tracked. Returns the
    worktree path. Aborts if the worktree path already exists.
    """
    root = main_checkout_root(cwd=cwd)
    worktree_path = Path(root) / WORKTREE_ROOT / name
    if worktree_path.exists():
        raise FileExistsError(f"Worktree path already exists: {worktree_path}")

    # Create the branch + worktree off the base branch.
    run(["git", "worktree", "add", "-b", name, str(worktree_path), base], cwd=cwd)

    # Register the stack parent with git-town (idempotent; sets the config key we read back).
    set_parent_branch(name, base, cwd=cwd)

    return str(worktree_path)
