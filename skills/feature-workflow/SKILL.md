---
name: feature-workflow
description: Manage parallel feature development with branches, worktrees, PRs, and GitHub-issue-backed review tracking with a merge gate. Use when the user wants to start a new feature, stack a feature on another, adopt an existing branch into tracking, run a tracked code review that files problems as issues, check whether a feature is safe to merge, or resume work on a feature branch and needs its status. On a branch tracked by this workflow (one with a wired feature issue), this skill OWNS reviewing — any request to "run a review", "review this", "do a code review", or "review the PR" while on a tracked feature branch means this skill's review flow (finder subagent → dedup → file problems → record the run so the gate moves), NOT a standalone/generic code reviewer and NOT a search for a project-local code-review skill. Trigger phrases include "start a feature", "new feature branch", "stack this on", "adopt this branch", "run a review", "review this", "review the PR", "review this feature", "can I merge", "what's the status of this branch", "feature gate".
---

# Feature workflow

Drives the `feature` CLI (globally installed; repo at `/Users/kbww511/git/feature-workflow`).
Each feature = a branch + worktree + a GitHub tracking issue holding machine-readable state.
Review-found problems become sub-issues; a **merge gate** opens when nothing *blocking* is left
and the last review found no new blocking problems. Reviews are budgeted, and when the loop
stops converging the gate says **NEEDS_DECISION** instead of asking for another round forever.
Full design: that repo's `docs/design.md`.

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
| Record an in-session review so the gate moves | `feature review record --sha <sha> --new-blocking <n> [--new-low <n>] [--regressions <n>] --summary "<one line>"` |
| See what actually blocks the merge | `feature problem list --blocking` (or `--open` for everything open) |
| File a problem you found | `feature problem add --title "<t>" --sev <high\|med\|low> [--body "<detail>" \| --body-file <path\|->]` |
| Mark a problem fixed | `feature problem resolve <issue#> --commit <sha>` |
| Ship a real problem unfixed (on the record) | `feature problem defer <issue#> --reason "<why it's acceptable>"` |
| Kill a false positive (on the record) | `feature problem reject <issue#> --reason "<why it isn't real>"` |
| Put a deferred/rejected problem back in the way | `feature problem block <issue#> --reason "<what changed>"` — revokes the disposition (and re-opens if it was closed) |
| See / change how many review rounds this feature gets | `feature budget` (`--set <n>` to override, `--auto` to go back to diff-sized) |
| Hand a non-converging loop to a human | `feature escalate --reason "<what you found + what you recommend>"` |
| Sync branch with base (recursive) | `feature sync` (or `--stack` for the whole stack) — delegates to git-town; reopens the gate if the base moved |
| Check if safe to merge | `feature gate` — **exit 0 = OPEN (ship), 10 = REVIEW_AGAIN, 20 = NEEDS_DECISION (stop, ask the human)**; 1/2 still mean the command itself failed |
| After merging, close out | `feature merge` |
| Resume / understand a branch | `feature status` |
| A repo/feature issue predating this CLI version | `feature migrate` — (re-)creates the workflow labels AND upgrades the state block. Run it once per repo after the CLI is upgraded; `problem defer`/`reject` abort without the newer labels. |

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
     `feature problem block <#> --reason "rediscovered in review N: <repro>"` and count it in
     `--new-blocking` and `--regressions` (if it is high/med). No match → genuinely new.
     **Use `problem block`, not `gh issue reopen`** — a problem that was deferred keeps its
     `deferred` label through `resolve`, so a raw re-open leaves it OPEN and *non-blocking*, and
     the gate would go green with a live high-severity regression sitting outside it.
   - **caused by THIS PR?** If the finding is a pre-existing issue in a file the PR merely
     touches, or an adjacent improvement the PR didn't create, it is **never blocking** — file
     it `--sev low`, or file it and `feature problem defer <#> --reason "pre-existing, not
     introduced by this PR"`. This rule matters: a reviewer wandering outside the diff is a
     large share of why review loops never end.
   - **severity** — this is now load-bearing, so assign it honestly:
     - `high` — data loss, security hole, crash, or a silently wrong result on a realistic path.
     - `med` — a real bug on a narrower path, or a correctness/robustness defect that will bite.
     - `low` — style, naming, duplication, altitude, hypotheticals, anything you couldn't tie to
       a concrete failure. **Low never blocks the merge**, and it is the ONLY non-blocking
       severity: a problem with no `sev:` label blocks until labelled (fail-safe), so always pass
       `--sev`.
5. **File the genuinely-new ones** — `feature problem add --title "…" --sev high|med|low`, putting
   the concrete failure scenario + `file:line` in the body. The body is usually multi-line and
   has code snippets, so pipe it via `--body-file -` (`… --body-file - <<'EOF' … EOF`) rather
   than `--body "…"`, which mangles backticks/quotes/newlines through the shell.
6. **Record the run so the gate moves** — `feature review record --sha $(git rev-parse HEAD)
   --new-blocking <high+med filed, plus blocking regressions> --new-low <lows filed>
   --regressions <closed BLOCKING problems reopened> --summary "<one line>"`. Skipping this leaves the gate
   at "no valid review run recorded" even after you've filed problems. The command prints the
   resulting gate verdict and your next step — **read it and obey it** (see below).
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

Then the loop: fix each blocking problem → `feature problem resolve <#> --commit <sha>` → review
in-session again → `feature review record …`. The gate opens when nothing blocking is left AND the
last recorded review covers the current branch head — so any commit after a review (including your
fixes) requires one more round, while *deferring* the rest needs no round, because it changes no
code. When `feature gate` exits 0, the human merges.

## When to stop reviewing (the part that used to loop forever)

A reviewer always finds *something*, so "review until a run comes back empty" never terminates.
The gate therefore has three verdicts, and reviews are budgeted. **Let the verdict drive you** —
don't decide on vibes, and don't launch another finder subagent without one telling you to:

| `feature gate` | Exit | What you do |
| --- | --- | --- |
| `OPEN` | 0 | Report that it's safe to merge, name the debt that ships, stop. The human merges. |
| `REVIEW_AGAIN` | 10 | Fix the blocking problems, then run **one** more review round. |
| `NEEDS_DECISION` | 20 | **STOP. Do not run another review.** `feature escalate --reason "…"` and hand the human a recommendation. |

(Exit 1 or 2 means the *command* failed — bad flag, missing wiring, `gh` error. Read the message; don't treat it as a verdict.)

`NEEDS_DECISION` fires when the review budget (2–4 rounds, auto-sized from diff size and whether
the PR touches paths the repo marked sensitive) is spent, or earlier when the loop is visibly not
converging — blocking findings stopped decreasing, or a round re-opened problems that were already
fixed once. When you escalate, recommend exactly one of:

- **Ship it** — the remainder is genuinely acceptable debt. Say which problems and why, and defer
  them (`feature problem defer <#> --reason "…"`) so the decision is on the record.
- **Buy another round** — you have a specific reason to believe one more pass converges
  (`feature budget --set <n>`). Don't use this to avoid making a call.
- **Split the PR** — the diff is too big to converge; land the settled part, move the rest out.
- **Redesign** — repeated regressions in the same area mean the approach is wrong, not the code.

Three rules that keep this honest:

- **Never lower a severity to open the gate.** If a high/med problem should ship unfixed, `defer`
  it with a reason — same outcome, but the decision is attributable instead of hidden in a label.
- **A disposition is revocable.** If a later round rediscovers a deferred problem with a repro that
  changes the call, `feature problem block <#> --reason "…"` puts it back in the way. A `rejected`
  problem you re-open (the regression rule in step 4) blocks again on its own — rejection is
  expressed by the issue being closed, so re-opening one overturns it.
- **Never `feature merge --force`** to get around a closed gate. Escalate; the human decides.
- **Deferred and low problems stay OPEN on purpose** and are listed in the merge comment. Debt you
  can't see isn't tracked.

## Rules

- Don't hand-edit the JSON block in a feature issue — the CLI owns it.
- The gate is advisory: it gives a green light; the human clicks merge. Don't merge for them.
- Prereqs: `gh` authenticated, `git-town` installed (for stacking). If a command aborts,
  surface the error — the CLI is deliberately fail-fast.
- To create a stacked feature, you can run `feature create --base` from anywhere; the new
  worktree is placed under the MAIN checkout's `.claude/worktrees/`, not nested in the current
  one.
