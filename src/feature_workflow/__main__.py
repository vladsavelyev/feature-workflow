"""feature — parallel feature workflow CLI.

Usage:
    uv run python -m feature_workflow <command> [options]

See docs/design.md for the design and full command reference.
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import UTC, datetime

from . import gate, github
from .gitwiring import (
    create_branch_and_worktree,
    current_branch,
    feature_issue,
    git_town_sync,
    local_only_commits,
    parent_branch,
    run_head_sha,
    set_feature_issue,
    set_parent_branch,
    worktree_for_branch,
)
from .state import (
    new_state,
    parse_block,
    render_block,
    replace_block,
)

SEV_LABELS = {"high": "sev:high", "med": "sev:med", "low": "sev:low"}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_issue(branch: str) -> int:
    issue = feature_issue(branch)
    if issue is None:
        sys.exit(
            f"No feature issue wired for branch '{branch}'. "
            f"Run `feature create` first, or set branch.{branch}.feature-issue."
        )
    return issue


def _load_state(branch: str) -> tuple[int, dict, str]:
    """Return (issue_number, state_dict, raw_body) for a branch's feature issue."""
    issue = _resolve_issue(branch)
    body = github.get_issue_body(issue)
    return issue, parse_block(body), body


def _save_state(issue: int, body: str, state: dict) -> None:
    state["updated"] = _now()
    github.set_issue_body(issue, replace_block(body, state))


# ── commands ────────────────────────────────────────────────────────────────


def _open_feature_issue(name: str, base: str, title: str | None) -> int:
    """Create the tracking issue with an initial state block and wire it to the branch."""
    state = new_state(branch=name, base=base, updated=_now())
    resolved_title = title or name.replace("-", " ").capitalize()
    body = (
        f"## {resolved_title}\n\n"
        "Feature tracking issue. Machine state below — edited by the `feature` CLI, "
        "don't hand-edit the JSON.\n\n" + render_block(state)
    )
    issue = github.create_issue(resolved_title, body, labels=["feature"])
    set_feature_issue(name, issue)
    return issue


def cmd_create(args: argparse.Namespace) -> None:
    if args.init_labels:
        github.init_labels()

    base = args.base or "main"
    name = args.name

    # Branch off an *intentional* base: local `base` must not sit ahead of its remote. A drifted
    # local base (e.g. unpushed commits on `main`) silently leaks into the branch, and since the
    # PR diff is computed against the remote base, those commits surface as unrelated changes in
    # the PR. Abort with the fix rather than produce a mis-scoped branch. `--allow-local-base`
    # opts in when branching off local-only work is deliberate (e.g. a not-yet-pushed base).
    if not args.allow_local_base:
        stray = local_only_commits(base)
        if stray:
            sys.exit(
                f"Refusing to branch off '{base}': it is {len(stray)} commit(s) ahead of its "
                f"remote, which would leak into the PR diff.\n"
                f"  Local-only commits: {', '.join(stray)}\n"
                f"Push or reset '{base}' to match its remote first (e.g. `git -C <repo> push` or "
                f"`git fetch && git reset --hard @{{u}}` on '{base}'), then retry.\n"
                f"Or pass --allow-local-base if branching off local-only commits is intentional."
            )

    worktree_path = create_branch_and_worktree(name, base)
    issue = _open_feature_issue(name, base, args.title)

    print(f"Created feature '{name}'")
    print(f"  base branch : {base}")
    print(f"  worktree    : {worktree_path}")
    print(f"  issue       : #{issue}")


def cmd_adopt(args: argparse.Namespace) -> None:
    """Wire an already-existing branch into tracking (no new branch/worktree)."""
    if args.init_labels:
        github.init_labels()

    name = args.branch or current_branch()
    if feature_issue(name) is not None:
        sys.exit(f"Branch '{name}' is already tracked (issue #{feature_issue(name)}).")

    # An explicit --base is authoritative and must win everywhere, including the git-town
    # parent pointer that status/gate read first. Without --base, fall back to the existing
    # parent, else main. Only skip the write when the recorded parent already matches.
    base = args.base or parent_branch(name) or "main"
    if parent_branch(name) != base:
        set_parent_branch(name, base)
    issue = _open_feature_issue(name, base, args.title)

    # A branch adopted after its PR was opened should link that PR automatically — otherwise
    # the tracking issue sits at pr:null until someone remembers `feature pr`, and the PR has
    # no visible connection back to the feature. Mirror cmd_pr's state transition here.
    pr = github.open_pr_for_branch(name)
    if pr is not None:
        _, state, body = _load_state(name)
        state["pr"] = pr
        state["status"] = "in-review"
        _save_state(issue, body, state)
        github.comment(issue, f"🔗 Linked PR #{pr}.")

    print(f"Adopted existing branch '{name}'")
    print(f"  base branch : {base}")
    print(f"  issue       : #{issue}")
    if pr is not None:
        print(f"  linked PR   : #{pr}")


def cmd_prompt(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    issue, state, body = _load_state(branch)
    state["last_prompt"] = args.text
    if state["status"] == "planning":
        state["status"] = "in-progress"
    _save_state(issue, body, state)
    print(f"Recorded prompt for #{issue}")


def cmd_pr(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    issue, state, body = _load_state(branch)
    state["pr"] = args.number
    state["status"] = "in-review"
    _save_state(issue, body, state)
    github.comment(issue, f"🔗 Linked PR #{args.number}.")
    print(f"Linked PR #{args.number} to feature #{issue}")


def cmd_review_record(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    issue, state, body = _load_state(branch)
    if state["pr"] is None:
        sys.exit(
            f"Refusing to record a review for #{issue}: no PR is linked. Push the branch and "
            f"open a PR, then `feature pr <number>`. Reviews scope against the PR diff, which is "
            f"defined against the feature's true base (the parent branch for a stacked feature)."
        )
    # The PR's diff is `git diff <baseRefName>...HEAD`, so the review scope is only correct when
    # the PR targets the feature's tracked parent. A PR opened against `main` for a stacked
    # feature would silently review the parent's commits too. Abort on mismatch — same base
    # resolution as `feature status` (git-town pointer, then recorded base).
    expected_base = parent_branch(branch) or state.get("base") or "main"
    pr_base = github.pr_base_branch(state["pr"])
    if pr_base != expected_base:
        sys.exit(
            f"Refusing to record a review for #{issue}: PR #{state['pr']} targets '{pr_base}', "
            f"but this feature's base is '{expected_base}'. The review would scope against the "
            f"wrong diff (pulling in the base branch's own commits). Reopen the PR against "
            f"'{expected_base}' (`gh pr edit {state['pr']} --base {expected_base}`), then retry."
        )
    run_num = state["review_runs"] + 1
    state["review_runs"] = run_num
    state["last_review"] = {
        "run": run_num,
        "sha": args.sha,
        "new_problems": args.new,
        "summary": args.summary,
    }
    _save_state(issue, body, state)

    open_count = sum(1 for s in github.sub_issues(issue) if s["state"] == "OPEN")
    github.comment(
        issue,
        f"🔍 Review run {run_num} ({args.sha}): {args.new} new problem(s). {open_count} open. {args.summary}",
    )
    print(f"Recorded review run {run_num} for #{issue}")


def _add_problem(parent: int, title: str, sev: str, detail: str) -> int:
    """Create a problem sub-issue linked to the feature. Dedup across runs is semantic (the
    reviewing agent judges by meaning), so no fingerprint is stamped in the body."""
    child = github.create_issue(title, detail, labels=["problem", SEV_LABELS[sev]])
    github.link_sub_issue(parent, child)
    return child


def cmd_problem_add(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    parent = _resolve_issue(branch)
    if args.body_file is not None:
        # Multi-line failure scenarios with code snippets are painful (and quoting-fragile) as a
        # `--body` argument; read the body from a file, or from stdin when `-`. No fallback: an
        # unreadable file must crash, not silently file an empty problem.
        detail = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text()
    else:
        detail = args.body or f"Found during review of feature #{parent}."
    child = _add_problem(parent, args.title, args.sev, detail)
    print(f"Added problem #{child} ({args.sev}) under feature #{parent}")


def cmd_problem_resolve(args: argparse.Namespace) -> None:
    note = f"Fixed in {args.commit}." if args.commit else "Resolved."
    github.close_issue(args.number, note)
    print(f"Resolved problem #{args.number}")


def cmd_problem_list(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    parent = _resolve_issue(branch)
    subs = github.sub_issues(parent)
    if args.open:
        subs = [s for s in subs if s["state"] == "OPEN"]
    if not subs:
        print("No problems.")
        return
    for s in subs:
        sev = next((label for label in s["labels"] if label.startswith("sev:")), "sev:?")
        print(f"  #{s['number']:>5}  {s['state']:<6}  {sev:<9}  {s['title']}")


def cmd_status(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    issue = feature_issue(branch)
    if issue is None:
        # Not a tracked feature — report plainly rather than crash (bootstrap-friendly).
        print(f"Branch '{branch}' is not a tracked feature (no wired issue).")
        return
    _, state, _ = _load_state(branch)
    subs = github.sub_issues(issue)
    open_probs = [s for s in subs if s["state"] == "OPEN"]

    if args.json:
        print(
            json.dumps(
                {
                    "state": state,
                    "base": parent_branch(branch) or state.get("base") or "main",
                    "worktree": worktree_for_branch(branch),
                    "open_problems": open_probs,
                },
                indent=2,
            )
        )
        return

    base = parent_branch(branch) or state.get("base") or "main"
    worktree = worktree_for_branch(branch)
    print(f"Branch     : {branch}")
    print(f"Base       : {base}")
    print(f"Worktree   : {worktree or '(not checked out in any worktree)'}")
    print(f"Issue      : #{issue}")
    print(f"PR         : {state['pr']}")
    print(f"Status     : {state['status']}")
    print(f"Last prompt: {state['last_prompt']}")
    lr = state["last_review"]
    if lr:
        print(f"Last review: run {lr['run']} ({lr['sha']}) — {lr['new_problems']} new — {lr['summary']}")
    else:
        print("Last review: (none)")
    print(f"Open problems ({len(open_probs)}):")
    for s in open_probs:
        sev = next((label for label in s["labels"] if label.startswith("sev:")), "sev:?")
        print(f"  #{s['number']} [{sev}] {s['title']}")


def _gate_for(branch: str) -> tuple[int, dict, gate.GateDecision]:
    issue, state, _ = _load_state(branch)
    open_count = sum(1 for s in github.sub_issues(issue) if s["state"] == "OPEN")
    return issue, state, gate.evaluate(open_count, state["last_review"])


def cmd_gate(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    issue, _, decision = _gate_for(branch)
    print(f"GATE {decision} (#{issue})")
    if not decision.open:
        sys.exit(1)


def cmd_merge(args: argparse.Namespace) -> None:
    """Final transition: verify the gate, mark merged, close the feature issue.

    Does NOT merge the PR itself — the human clicks merge. This records the outcome
    once the branch is merged and closes the tracking issue.
    """
    branch = args.branch or current_branch()
    issue, state, decision = _gate_for(branch)
    if not decision.open and not args.force:
        sys.exit(f"Refusing to close #{issue}: gate {decision}. Use --force to override.")

    state["status"] = "merged"
    body = github.get_issue_body(issue)
    _save_state(issue, body, state)
    github.close_issue(issue, "✅ Merged. All problems resolved and review clean.")
    print(f"Marked feature #{issue} merged and closed it.")


def cmd_sync(args: argparse.Namespace) -> None:
    """Sync the branch with its base (recursively, via git-town), then invalidate the last
    review so the gate reopens — the base moved, so the prior clean pass no longer covers
    the code. Delegates the actual sync to git-town; does not auto-resolve conflicts."""
    branch = args.branch or current_branch()
    issue, state, _ = _load_state(branch)

    before = run_head_sha()
    print(f"Syncing {branch} via git-town{' (stack)' if args.stack else ''}…")
    git_town_sync(stack=args.stack)
    after = run_head_sha()

    if after == before and state["last_review"] is not None:
        # Base already up to date and nothing merged in — the prior review still holds.
        print(f"Already up to date ({after}); review unchanged.")
        return

    # The base advanced: any prior "clean" review predates the newly merged code, so drop it.
    # The gate reopens and a fresh in-session review + `feature review record` is required first.
    stale = state["last_review"] is not None
    state["last_review"] = None
    body = github.get_issue_body(issue)
    _save_state(issue, body, state)
    note = f"🔄 Synced with base — now at {after} (was {before})."
    if stale:
        note += " Previous clean review invalidated; re-review and `feature review record` before merge."
    github.comment(issue, note)
    print(f"Synced to {after}." + (" Gate reopened (review invalidated)." if stale else ""))


# ── arg parsing ───────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="feature", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("create", help="Create branch + worktree + feature issue")
    c.add_argument("name", help="Feature/branch name (short, e.g. add-oauth)")
    c.add_argument("--base", help="Base branch to stack on (default: main)")
    c.add_argument("--title", help="Issue title (default: derived from name)")
    c.add_argument("--init-labels", action="store_true", help="Create workflow labels first")
    c.add_argument(
        "--allow-local-base",
        action="store_true",
        help="Branch off the base even if it has local-only (unpushed) commits",
    )
    c.set_defaults(func=cmd_create)

    c = sub.add_parser("adopt", help="Wire an EXISTING branch into tracking (no new worktree)")
    c.add_argument("--branch", help="Branch to adopt (default: current)")
    c.add_argument("--base", help="Base branch (default: git-town parent, else main)")
    c.add_argument("--title", help="Issue title (default: derived from branch name)")
    c.add_argument("--init-labels", action="store_true", help="Create workflow labels first")
    c.set_defaults(func=cmd_adopt)

    c = sub.add_parser("prompt", help="Record the latest prompt")
    c.add_argument("text")
    c.add_argument("--branch")
    c.set_defaults(func=cmd_prompt)

    c = sub.add_parser("pr", help="Link a PR number to the feature")
    c.add_argument("number", type=int)
    c.add_argument("--branch")
    c.set_defaults(func=cmd_pr)

    # Reviews run in-session: the agent invokes /code-review itself, dedups against tracked
    # problems, files new ones via `problem add`, and records the run here. There is no headless
    # `review run` — spawning a second long-lived `claude` subprocess got reaped by the session
    # manager (see the feature-workflow skill's "Reviewing a feature").
    c = sub.add_parser("review", help="Review subcommands")
    rsub = c.add_subparsers(dest="review_command", required=True)
    r = rsub.add_parser("record", help="Record an in-session review run (moves the gate)")
    r.add_argument("--sha", required=True)
    r.add_argument("--new", type=int, required=True, help="Count of NEW problems found")
    r.add_argument("--summary", required=True)
    r.add_argument("--branch")
    r.set_defaults(func=cmd_review_record)

    c = sub.add_parser("problem", help="Problem subcommands")
    psub = c.add_subparsers(dest="problem_command", required=True)
    pa = psub.add_parser("add", help="Add a problem sub-issue")
    pa.add_argument("--title", required=True)
    pa.add_argument("--sev", required=True, choices=list(SEV_LABELS))
    body_src = pa.add_mutually_exclusive_group()
    body_src.add_argument("--body", help="Problem detail as an inline string")
    body_src.add_argument(
        "--body-file", help="Read problem detail from a file, or '-' for stdin (for multi-line detail)"
    )
    pa.add_argument("--branch")
    pa.set_defaults(func=cmd_problem_add)
    pr = psub.add_parser("resolve", help="Close a problem sub-issue")
    pr.add_argument("number", type=int)
    pr.add_argument("--commit")
    pr.set_defaults(func=cmd_problem_resolve)
    pl = psub.add_parser("list", help="List problem sub-issues")
    pl.add_argument("--open", action="store_true", help="Only open problems")
    pl.add_argument("--branch")
    pl.set_defaults(func=cmd_problem_list)

    c = sub.add_parser("status", help="Reconstruct and print feature state")
    c.add_argument("--branch")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_status)

    c = sub.add_parser("gate", help="Exit 0 if safe to merge, else 1")
    c.add_argument("--branch")
    c.set_defaults(func=cmd_gate)

    c = sub.add_parser("merge", help="Mark feature merged and close its issue (checks gate)")
    c.add_argument("--branch")
    c.add_argument("--force", action="store_true", help="Close even if the gate is closed")
    c.set_defaults(func=cmd_merge)

    c = sub.add_parser("sync", help="Sync branch with base via git-town; reopens the gate if base moved")
    c.add_argument("--branch")
    c.add_argument("--stack", action="store_true", help="Sync the whole stack, not just this branch")
    c.set_defaults(func=cmd_sync)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
