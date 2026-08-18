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

from . import budget, gate, github, problems
from .gitwiring import (
    create_branch_and_worktree,
    current_branch,
    feature_issue,
    git_town_sync,
    local_only_commits,
    parent_branch,
    run_head_sha,
    sensitive_patterns,
    set_feature_issue,
    set_parent_branch,
    worktree_for_branch,
)
from .state import (
    SCHEMA,
    migrate,
    new_state,
    parse_block,
    parse_block_raw,
    render_block,
    replace_block,
    record_review,
    set_status,
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


def _resolve_budget(state: dict) -> tuple[int, list[str]]:
    """The feature's review-round budget: an explicit override, else auto-sized from the PR diff.

    Auto-sizing happens at query time rather than being frozen into the state block, so a branch
    that grows from 20 to 900 lines earns its extra round without anyone re-running a command.
    """
    explicit = state["review_budget"]
    if explicit is not None:
        return explicit, [f"explicit override: {explicit} round(s)"]
    if state["pr"] is None:
        return budget.BASE_ROUNDS, ["no PR linked yet, so no diff to size — base budget for now"]
    files, lines = github.pr_diff_stats(state["pr"])
    hits = budget.sensitive_matches(github.pr_changed_paths(state["pr"]), sensitive_patterns())
    return budget.auto_rounds(changed_files=files, changed_lines=lines, sensitive_hits=hits)


def _triage(issue: int) -> problems.Triage:
    return problems.triage(github.sub_issues(issue))


def _require_problem(branch: str, number: int) -> dict:
    """Return `number` as a problem sub-issue of this feature, aborting if it isn't one.

    Resolving and disposing edit exactly the state the gate reads. Pointed at the wrong number —
    a typo, a PR number, the feature issue itself — they would quietly change what blocks a merge
    or close something unrelated. One API call to rule it out is worth it.
    """
    parent = _resolve_issue(branch)
    sub = next((s for s in github.sub_issues(parent) if s["number"] == number), None)
    if sub is None:
        sys.exit(
            f"#{number} is not a problem sub-issue of feature #{parent} — refusing to touch it. "
            f"Run `feature problem list` to see this feature's problems."
        )
    return sub


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
        set_status(state, "in-review")
        _save_state(issue, body, state)
        github.link_pr_to_feature(pr, issue)
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
        set_status(state, "in-progress")
    _save_state(issue, body, state)
    print(f"Recorded prompt for #{issue}")


def cmd_pr(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    issue, state, body = _load_state(branch)
    state["pr"] = args.number
    set_status(state, "in-review")
    _save_state(issue, body, state)
    # Native link: `Part of #<issue>` in the PR body so GitHub shows the connection in the PR
    # sidebar and the issue timeline — not just a comment that no linked-issues view reads.
    github.link_pr_to_feature(args.number, issue)
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
    entry = record_review(
        state,
        sha=args.sha,
        new_blocking=args.new_blocking,
        new_low=args.new_low,
        regressions=args.regressions,
        summary=args.summary,
    )
    run_num = entry["run"]

    # ORDER IS THE RETRY STRATEGY: every fallible call happens BEFORE the single state write, so a
    # failure anywhere leaves the round unrecorded and re-running the command records it exactly
    # once. (A duplicated timeline comment, the one thing a retry can repeat, is harmless noise; a
    # half-recorded round is not.) Reporting the verdict here is the point of the command — it's when
    # the agent learns whether to fix and review again or to stop and escalate.
    triaged = _triage(issue)
    rounds, _ = _resolve_budget(state)
    decision = gate.evaluate(
        blocking_open=len(triaged.blocking),
        last_review=state["last_review"],
        history=state["review_history"],
        budget=rounds,
    )
    github.comment(
        issue,
        f"🔍 Review run {run_num} ({args.sha}): {args.new_blocking} new blocking, {args.new_low} new low, "
        f"{args.regressions} regression(s). {len(triaged.blocking)} blocking open. {args.summary}\n\n"
        f"Gate: {decision}",
    )
    _save_state(issue, body, state)
    print(f"Recorded review run {run_num} for #{issue}")
    print(f"GATE {decision}")
    print(f"  → {decision.next_step}")


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
    _require_problem(args.branch or current_branch(), args.number)
    note = f"Fixed in {args.commit}." if args.commit else "Resolved."
    github.close_issue(args.number, note)
    print(f"Resolved problem #{args.number}")


def cmd_problem_defer(args: argparse.Namespace) -> None:
    """Real problem, not fixed in this PR: stays OPEN and visible, stops holding the gate.

    Deliberately not closed — deferred work is debt, and debt you can't see isn't tracked. It
    keeps its severity (so nobody has to lie about how bad it is) and gains a `deferred` label
    plus a reason on its timeline, which is what makes shipping it a decision rather than a leak.
    """
    sub = _require_problem(args.branch or current_branch(), args.number)
    github.add_label(args.number, problems.DEFERRED)
    github.comment(args.number, f"⏸ Deferred — not fixed in this PR, no longer blocking: {args.reason}")
    print(f"Deferred problem #{args.number} [{problems.severity(sub)}] (still open, no longer blocking)")


def cmd_problem_reject(args: argparse.Namespace) -> None:
    """Not a real problem (false positive or by design): closed with the reasoning recorded."""
    _require_problem(args.branch or current_branch(), args.number)
    github.add_label(args.number, problems.REJECTED)
    github.close_issue(args.number, f"🚫 Rejected — not a real problem: {args.reason}")
    print(f"Rejected problem #{args.number}")


def cmd_problem_block(args: argparse.Namespace) -> None:
    """Revoke a deferral: this problem holds the merge after all.

    The inverse of `defer`, and it needs to exist. A later review round can rediscover a deferred
    problem with a repro that changes the call, and without this the deferral was permanent — the
    problem would sit outside the gate with no way to put it back.
    """
    branch = args.branch or current_branch()
    sub = _require_problem(branch, args.number)
    if problems.DEFERRED in sub["labels"]:
        github.remove_label(args.number, problems.DEFERRED)
    if sub["state"] != "OPEN":
        github.reopen_issue(args.number, f"↩️ Re-opened: {args.reason}")
    github.comment(args.number, f"⛔ Blocking again — the earlier disposition is revoked: {args.reason}")
    print(f"Problem #{args.number} [{problems.severity(sub)}] blocks the gate again")


def _problem_line(sub: dict) -> str:
    disposed = problems.disposition(sub)
    mark = "BLOCKING" if problems.is_blocking(sub) else (disposed or "non-blocking")
    return f"  #{sub['number']:>5}  {sub['state']:<6}  {problems.severity(sub):<9}  {mark:<12}  {sub['title']}"


def cmd_problem_list(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    parent = _resolve_issue(branch)
    subs = github.sub_issues(parent)
    if args.blocking:
        subs = [s for s in subs if problems.is_blocking(s)]
    elif args.open:
        subs = [s for s in subs if s["state"] == "OPEN"]
    if not subs:
        print("No problems.")
        return
    for s in subs:
        print(_problem_line(s))


def cmd_status(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    issue = feature_issue(branch)
    if issue is None:
        # Not a tracked feature — report plainly rather than crash (bootstrap-friendly).
        print(f"Branch '{branch}' is not a tracked feature (no wired issue).")
        return
    _, state, _ = _load_state(branch)
    triaged = _triage(issue)
    rounds, rounds_why = _resolve_budget(state)
    decision = gate.evaluate(
        blocking_open=len(triaged.blocking),
        last_review=state["last_review"],
        history=state["review_history"],
        budget=rounds,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "state": state,
                    "base": parent_branch(branch) or state.get("base") or "main",
                    "worktree": worktree_for_branch(branch),
                    "review_budget": {"rounds": rounds, "used": decision.rounds_used, "why": rounds_why},
                    "gate": {
                        "verdict": str(decision.verdict),
                        "reasons": decision.reasons,
                        "next_step": decision.next_step,
                    },
                    "blocking_problems": triaged.blocking,
                    "non_blocking_problems": triaged.debt,
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
        print(
            f"Last review: run {lr['run']} ({lr['sha']}) — {lr['new_blocking']} new blocking, "
            f"{lr['new_low']} low, {lr['regressions']} regression(s) — {lr['summary']}"
        )
    else:
        print("Last review: (none valid)")
    print(f"Reviews    : {decision.rounds_used}/{rounds} rounds used ({'; '.join(rounds_why)})")
    print(f"Gate       : {decision}")
    print(f"  → {decision.next_step}")
    print(f"Blocking problems ({len(triaged.blocking)}):")
    for s in triaged.blocking:
        print(f"  #{s['number']} [{problems.severity(s)}] {s['title']}")
    print(f"Non-blocking, open ({len(triaged.debt)}):")
    for s in triaged.debt:
        print(f"  #{s['number']} [{problems.severity(s)}/{problems.disposition(s) or 'open'}] {s['title']}")


def _gate_for(branch: str) -> tuple[int, dict, problems.Triage, gate.GateDecision]:
    issue, state, _ = _load_state(branch)
    triaged = _triage(issue)
    rounds, _ = _resolve_budget(state)
    decision = gate.evaluate(
        blocking_open=len(triaged.blocking),
        last_review=state["last_review"],
        history=state["review_history"],
        budget=rounds,
    )
    return issue, state, triaged, decision


def cmd_gate(args: argparse.Namespace) -> None:
    branch = args.branch or current_branch()
    issue, _, triaged, decision = _gate_for(branch)
    print(f"GATE {decision} (#{issue})")
    print(f"  → {decision.next_step}")
    print(f"  {triaged.summary()}")
    # Exit code is the machine interface: 0 ship, 10 review again, 20 human decision needed.
    # (1 and 2 mean the command itself failed — see gate.EXIT_CODES.)
    sys.exit(decision.exit_code)


def cmd_budget(args: argparse.Namespace) -> None:
    """Show — or override — how many review rounds this feature gets before a human decides."""
    branch = args.branch or current_branch()
    issue, state, body = _load_state(branch)
    if args.set is not None:
        if args.set < 1:
            sys.exit(f"A review budget of {args.set} would pin the gate at NEEDS_DECISION; pass 1 or more.")
        state["review_budget"] = args.set
        _save_state(issue, body, state)
        github.comment(issue, f"🎚 Review budget set to {args.set} round(s).")
    elif args.auto:
        state["review_budget"] = None
        _save_state(issue, body, state)
        github.comment(issue, "🎚 Review budget back to auto-sizing from the PR diff.")

    rounds, why = _resolve_budget(state)
    used = len(gate.rounds_since_invalidation(state["review_history"]))
    print(f"Review budget: {rounds} round(s) — {'; '.join(why)}")
    print(f"Used         : {used} round(s) since the last base change")


def cmd_escalate(args: argparse.Namespace) -> None:
    """Hand the feature to a human: the review loop isn't converging on its own.

    This is the recorded form of "stop and decide" — ship the remaining debt, buy another review
    round, split the PR, or redesign. It posts the current gate state alongside the reason so the
    human sees the evidence, and parks the feature at `needs-decision` (visible on the board).
    """
    branch = args.branch or current_branch()
    issue, state, triaged, decision = _gate_for(branch)
    body = github.get_issue_body(issue)
    set_status(state, "needs-decision")
    _save_state(issue, body, state)
    blocking_refs = ", ".join(f"#{s['number']}" for s in triaged.blocking) or "none"
    github.comment(
        issue,
        f"🚧 **Escalated for a human decision.** {args.reason}\n\n"
        f"Gate: {decision}\n\n"
        f"Blocking: {len(triaged.blocking)} — {blocking_refs}\n"
        f"{triaged.summary()}\n\n"
        f"Options: ship the rest as debt (`feature problem defer <#> --reason …`), buy another "
        f"review round (`feature budget --set <n>`), split the PR, or redesign.",
    )
    print(f"Escalated #{issue} for a human decision; status is now needs-decision.")


def cmd_migrate(args: argparse.Namespace) -> None:
    """Upgrade a repo + feature issue to what the current CLI expects (one-way).

    This is the single upgrade entry point, which is why it also (re-)creates the workflow labels:
    `deferred` and `rejected` are newer than the original label set, and `gh issue edit --add-label`
    hard-fails on a label the repo doesn't have — so on every repo onboarded before dispositions
    existed, `feature problem defer` (the main escape hatch out of a closed gate) would abort with
    an opaque error. Labels first, then the state block.
    """
    branch = args.branch or current_branch()
    issue = _resolve_issue(branch)
    github.init_labels()
    body = github.get_issue_body(issue)
    state = parse_block_raw(body)
    if state.get("schema") == SCHEMA:
        print(f"Workflow labels are up to date; #{issue} is already at schema {SCHEMA}.")
        return
    was = state.get("schema")
    _save_state(issue, body, migrate(state))
    print(f"Workflow labels are up to date; migrated #{issue} state from schema {was} to {SCHEMA}.")


def cmd_merge(args: argparse.Namespace) -> None:
    """Final transition: verify the gate, mark merged, close the feature issue.

    Does NOT merge the PR itself — the human clicks merge. This records the outcome
    once the branch is merged and closes the tracking issue.
    """
    branch = args.branch or current_branch()
    issue, state, triaged, decision = _gate_for(branch)
    if not decision.open and not args.force:
        sys.exit(f"Refusing to close #{issue}: gate {decision}. Use --force to override.")

    set_status(state, "merged")
    body = github.get_issue_body(issue)
    _save_state(issue, body, state)
    # Name what actually shipped, derived from the triage rather than asserted. Deferred and
    # low-severity problems stay open on purpose, and on the --force path there may be blocking
    # problems too — a note claiming "no blocking problems" there would put a false statement in the
    # feature's permanent record, which is the exact failure this workflow exists to prevent.
    if decision.open:
        note = f"✅ Merged. No blocking problems; {triaged.summary()}."
    else:
        overridden = ", ".join(f"#{s['number']} [{problems.severity(s)}]" for s in triaged.blocking) or "none"
        note = (
            f"⚠️ Merged with `--force` while the gate was **{decision.verdict}** "
            f"({'; '.join(decision.reasons)}).\n\n"
            f"Blocking problems overridden: {overridden}.\n{triaged.summary()}."
        )
    github.close_issue(issue, note)
    print(f"Marked feature #{issue} merged and closed it.")
    print(f"  {triaged.summary()}")
    if not decision.open:
        print(f"  overridden with --force: {len(triaged.blocking)} blocking problem(s) still open")


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

    if after == before:
        # Base already up to date and nothing merged in — the prior review still holds.
        print(f"Already up to date ({after}); review unchanged.")
        return

    # The base advanced: any prior "clean" review predates the newly merged code, so drop it.
    # The gate reopens and a fresh in-session review + `feature review record` is required first.
    # The marker in the history is load-bearing: the gate counts review rounds and reads the
    # convergence trend only from runs AFTER it, so new base code buys a fresh round of budget
    # instead of inheriting an exhausted one.
    stale = bool(gate.rounds_since_invalidation(state["review_history"]))
    if stale:
        state["review_history"].append({"event": "base-advanced", "sha": after, "at": _now()})
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
    r.add_argument(
        "--new-blocking",
        type=int,
        required=True,
        help="Count of NEW high/med problems filed, plus blocking ones re-opened as regressions",
    )
    r.add_argument("--new-low", type=int, default=0, help="Count of NEW low-severity problems filed (non-blocking)")
    r.add_argument(
        "--regressions",
        type=int,
        default=0,
        help="Count of previously-closed BLOCKING (high/med) problems re-opened this run — a churn signal",
    )
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
    pr.add_argument("--branch")
    pr.set_defaults(func=cmd_problem_resolve)
    pd = psub.add_parser("defer", help="Real problem, not fixed here: keep it open but non-blocking")
    pd.add_argument("number", type=int)
    pd.add_argument("--reason", required=True, help="Why it is acceptable to ship without fixing this")
    pd.add_argument("--branch")
    pd.set_defaults(func=cmd_problem_defer)
    pj = psub.add_parser("reject", help="Not a real problem (false positive / by design): close it")
    pj.add_argument("number", type=int)
    pj.add_argument("--reason", required=True, help="Why this is not a real problem")
    pj.add_argument("--branch")
    pj.set_defaults(func=cmd_problem_reject)
    pb = psub.add_parser("block", help="Revoke a disposition: this problem holds the merge after all")
    pb.add_argument("number", type=int)
    pb.add_argument("--reason", required=True, help="What changed — why it must block now")
    pb.add_argument("--branch")
    pb.set_defaults(func=cmd_problem_block)
    pl = psub.add_parser("list", help="List problem sub-issues")
    which = pl.add_mutually_exclusive_group()
    which.add_argument("--open", action="store_true", help="Only open problems")
    which.add_argument("--blocking", action="store_true", help="Only problems that hold the merge gate")
    pl.add_argument("--branch")
    pl.set_defaults(func=cmd_problem_list)

    c = sub.add_parser("status", help="Reconstruct and print feature state")
    c.add_argument("--branch")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_status)

    c = sub.add_parser(
        "gate",
        help="Merge verdict. Exit 0 = OPEN (ship), 10 = REVIEW_AGAIN, 20 = NEEDS_DECISION (human)",
    )
    c.add_argument("--branch")
    c.set_defaults(func=cmd_gate)

    c = sub.add_parser("budget", help="Show or override the feature's review-round budget")
    knob = c.add_mutually_exclusive_group()
    knob.add_argument("--set", type=int, help="Pin the budget to N review rounds")
    knob.add_argument("--auto", action="store_true", help="Drop the override; auto-size from the PR diff")
    c.add_argument("--branch")
    c.set_defaults(func=cmd_budget)

    c = sub.add_parser("escalate", help="Hand a non-converging review loop to a human decision")
    c.add_argument("--reason", required=True, help="What you found and what you recommend")
    c.add_argument("--branch")
    c.set_defaults(func=cmd_escalate)

    c = sub.add_parser("migrate", help="Upgrade a repo's labels + a feature issue's state block for this CLI")
    c.add_argument("--branch")
    c.set_defaults(func=cmd_migrate)

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
