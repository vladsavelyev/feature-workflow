---
name: feature-workflow
description: Manage parallel feature development with branches, worktrees, PRs, and GitHub-issue-backed review tracking with a merge gate. Use when the user wants to start a new feature, stack a feature on another, adopt an existing branch into tracking, run a tracked code review that files problems as issues, check whether a feature is safe to merge, or resume work on a feature branch and needs its status. On a branch tracked by this workflow (one with a wired feature issue), this skill OWNS reviewing — any request to "run a review", "review this", "do a code review", or "review the PR" while on a tracked feature branch means this skill's review flow (finder subagent → dedup → file problems → record the run so the gate moves), NOT a standalone/generic code reviewer and NOT a search for a project-local code-review skill. Trigger phrases include "start a feature", "new feature branch", "stack this on", "adopt this branch", "run a review", "review this", "review the PR", "review this feature", "can I merge", "what's the status of this branch", "feature gate".
---

# Feature workflow

Drives the `feature` CLI (globally installed; repo at `/Users/kbww511/git/feature-workflow`).
Each feature = a branch + worktree + a GitHub tracking issue holding machine-readable state.
Review-found problems become sub-issues; a **merge gate** opens only when all problems are
resolved AND the last review found nothing new. Full design: that repo's `docs/design.md`.

## When to use which command

Run `feature <cmd>` from inside the repo you're working in. All state lives in GitHub + git
config, so any session on the branch can reconstruct it.

| Situation | Command |
| --- | --- |
| Start a brand-new feature | `feature create <name> [--base <branch>]` — makes branch, worktree under `.claude/worktrees/<name>/`, and the tracking issue. Add `--init-labels` the first time in a repo. **Aborts if the base branch has local-only (unpushed) commits** — those would leak into the PR diff (computed against the *remote* base); push/reset the base first, or pass `--allow-local-base` if intentional. |
| Stack a feature on another | `feature create <child> --base <parent-branch>` |
| Track a branch that already exists | `feature adopt` (run on the branch; `--init-labels` first time) |
| Record what you were asked to do | `feature prompt "<the request>"` |
| Link the PR after opening it | `feature pr <number>` (required before any review) |
| Run a code review that files problems | **in-session** — see "Reviewing a feature" below (needs a linked PR; there is no headless review subcommand) |
| Record an in-session review so the gate moves | `feature review record --sha <sha> --new <count> --summary "<one line>"` |
| See open problems | `feature problem list --open` |
| File a problem you found | `feature problem add --title "<t>" --sev <high\|med\|low> [--body "<detail>" \| --body-file <path\|->]` |
| Mark a problem fixed | `feature problem resolve <issue#> --commit <sha>` |
| Sync branch with base (recursive) | `feature sync` (or `--stack` for the whole stack) — delegates to git-town; reopens the gate if the base moved |
| Check if safe to merge | `feature gate` (exit 0 = open) |
| After merging, close out | `feature merge` |
| Resume / understand a branch | `feature status` |

## Starting a session on an existing branch

FIRST run `feature status`. It prints the base branch, PR, last prompt, last review result,
and open problems — everything you need to resume. If a SessionStart hook is wired
(`feature-session-context`), this is injected automatically; otherwise run it yourself.

## Reviewing a feature (the core workflow)

Reviews run **in this session's process** — no separate headless `claude` (the retired `feature
review run` spawned a second, long-lived `claude` subprocess that a session manager reaps
after ~10 min, so it failed non-deterministically). But the **finder runs in a fresh subagent**,
not in your own context, and here's why:

> The gate's safety depends on the *final* review being a genuine independent sweep. If you run
> the finder in your own context, the second and later runs are poisoned — you remember the
> earlier runs, so instead of rediscovering issues blind you slip into "let me confirm #12 and
> #14 got fixed" and miss bugs the fixes themselves introduced. A subagent gets a clean context
> every run, so run 1 and run 3 find with equal rigor. It's a sub-task inside THIS process (no
> second `claude` to reap), so it keeps the reaping fix while restoring reviewer independence.

Split of labor: the **subagent finds** (blind); **you dedup and verify** the findings against the
tracked problem list. The finder stays blind by **ordering** — you run it BEFORE loading tracked
state, so there is simply no open-problem list in your context to leak into its prompt. Do this
when asked to review:

1. **A PR must exist first, opened against the feature's own base** — reviews scope against the
   PR diff. Push the branch and open the PR **against the parent branch** (`gh pr create --base
   <parent>`; for a top-level feature the parent is `main`, for a stacked feature it is the
   branch below it — NOT `main`). Link it with `feature pr <number>`. This is load-bearing: a
   PR's diff is `git diff <pr-base>...HEAD`, so if a stacked feature's PR is opened against `main`
   instead of its parent, the review silently pulls in the parent's commits — re-reviewing
   already-reviewed code and re-filing the parent's problems. `feature review record` enforces
   both halves: it refuses to run with no PR linked, AND if the PR's base branch ≠ the feature's
   tracked parent (so a PR mistakenly opened against `main` is caught, not silently mis-scoped).
2. **Run the finder in a fresh subagent — FIRST, before loading any tracked state** — spawn one
   subagent (`Agent` tool) whose whole job is to review the linked PR and return the raw
   findings. It reviews the PR with the **built-in reviewer**: invoke `Skill(review, <PR#>)`
   (the built-in "Review a GitHub pull request" skill; optionally add `--effort high`). Do NOT
   go hunting for a project-local `code-review` *skill* — there isn't one (a custom navari
   `code-review` skill was retired in favour of the built-in). `/code-review` also exists as a
   built-in slash *command* but reviews the local working diff, not a PR by number; for this
   flow the PR-scoped `Skill(review, <PR#>)` is the correct call. The PR number scopes the
   diff against its base — correct by construction for stacked branches. Because you haven't
   loaded the problem list or prior-review context yet, there is nothing to accidentally hand it
   — it finds blind. Same on the first review of the session or the fifth. Never run the finder
   in your own context, and never spawn a headless `claude`.
   - **Pin the subagent to the feature worktree.** The feature worktree lives *under* the main
     checkout (`.claude/worktrees/<name>`), so the main checkout's path is a prefix of it and
     holds the SAME files on a DIFFERENT branch (the base). When the finder reads enclosing
     functions, a stray `cd` toward "the repo root" lands it in the main checkout, where it
     reviews the *pre-feature* code — phantom findings, missed real ones. So tell the subagent
     its **working directory is the feature worktree** (the absolute path you are running in —
     `feature status` also prints it as `Worktree`), and that it must **read code only from
     there and never `cd` to the main checkout or any other worktree.**
3. **Now load tracked state for YOUR dedup** — after the finder returns, run `feature status` and
   `feature problem list --open` (each open problem is `#num — title`). These are the
   already-known bugs you must NOT re-file.
4. **Verify + dedup each finding yourself**, against the real code and the step-3 problem list:
   - **reproducible?** Read the actual code and construct the failing input/state. Drop
     anything you can't concretely reproduce — a false positive must never close the gate.
   - **duplicate?** Judge by *meaning*, not wording (the same bug is titled differently every
     review). Matches an OPEN problem → skip it. Matches a CLOSED problem → **regression**:
     reopen it (`gh issue reopen <#>`) and count it as new. No match → genuinely new.
5. **File the genuinely-new ones** — `feature problem add --title "…" --sev high|med|low`, putting
   the concrete failure scenario + `file:line` in the body. The body is usually multi-line and
   has code snippets, so pipe it via `--body-file -` (`… --body-file - <<'EOF' … EOF`) rather
   than `--body "…"`, which mangles backticks/quotes/newlines through the shell.
6. **Record the run so the gate moves** — `feature review record --sha $(git rev-parse HEAD)
   --new <count-of-new-plus-regressed> --summary "<one line>"`. Skipping this leaves the gate
   at "no review run recorded" even after you've filed problems.
   - The `--summary` must be a **real sentence derived from the review**, not boilerplate — draw
     it from the finder subagent's returned conclusion plus your dedup result. The `code-review`
     skill's structured result is an empty findings array on a clean pass (no prose), so the
     tracking issue and `last_review` only ever capture what you put here; if you downgrade the
     real conclusion to "clean pass", the durable record loses the one piece of evidence the
     review actually ran.
   - On a **zero-new run**, state what you reviewed and why it's clean, e.g. "Re-reviewed the 4
     files touching gate.py; the off-by-one from #12 is fixed; no new issues." A bare "clean
     pass" is not acceptable — an empty findings array plus a boilerplate summary is
     indistinguishable from a review that silently did nothing.

Then the loop: fix each problem → `feature problem resolve <#> --commit <sha>` → review
in-session again → `feature review record …`. The gate stays closed until a review records
**zero new** problems (this forces a clean pass after your last fix). When `feature gate` exits
0, the human merges.

## Rules

- Don't hand-edit the JSON block in a feature issue — the CLI owns it.
- The gate is advisory: it gives a green light; the human clicks merge. Don't merge for them.
- Prereqs: `gh` authenticated, `git-town` installed (for stacking). If a command aborts,
  surface the error — the CLI is deliberately fail-fast.
- To create a stacked feature, you can run `feature create --base` from anywhere; the new
  worktree is placed under the MAIN checkout's `.claude/worktrees/`, not nested in the current
  one.
