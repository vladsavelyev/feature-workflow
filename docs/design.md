# Parallel feature workflow

A system for working on many features in parallel with Claude: each feature gets a
branch, a matching PR, a matching worktree, and a GitHub tracking issue. Features can be
**stacked** on each other. Review-found problems are tracked as sub-issues, and the *blocking*
ones must be resolved (or explicitly disposed of) before the PR merges. Reviews are budgeted, so
a loop that won't converge ends in a recorded human decision rather than another expensive round.
A new Claude session on an existing branch reconstructs full context (base branch, last prompt,
last review, blocking problems) from GitHub.

> Status: alpha. The CLI lives in the `feature_workflow` package. See "CLI reference" below.
> For *why* it's designed this way (and what we tried and rejected), see
> [motivation.md](motivation.md).

## Why this design

The hard requirements and the choices they force:

| Requirement | Choice | Why |
| --- | --- | --- |
| Org-wide access to status/reviews | **GitHub Issues** as source of truth | Everyone already authenticates to GitHub; no new infra, free notifications/search/permissions. |
| Machine-readable feature state | **Fenced JSON block** in the feature issue body, between HTML-comment markers | Humans read a normal issue; Claude reads/writes one deterministic block. |
| Per-problem status + timeline | **Sub-issues** under the feature issue | Native parent/child, each problem gets its own state, assignee, and history. |
| Human dashboard | **GitHub Projects** (auto-add by label) | A board/table view *over* the issues — never the source of truth (GraphQL is painful, sub-item modeling weak). |
| Stacked branches | **git-town** | Stores the parent-branch pointer in git config (`git-town-branch.<name>.parent`) — exactly the "base branch" a session bootstrap needs. Open-source, no account. |
| One workspace per feature | **git worktrees** under `.claude/worktrees/<branch>/` | Project convention (`.claude/rules/project-rules.md`). |

**Source of truth = GitHub Issues. Projects is the view. git config holds the wiring.**

### What we deliberately do *not* do

- We don't store state in a file on the feature branch — rebases/merges across a stack churn
  it and cause conflicts. State lives in the issue (remote), keyed to the branch.
- We don't treat Projects as the store — it's a lens, auto-populated by label.
- We don't reinvent stacking — git-town owns parent pointers and restacking.

## Data model

```
GitHub Project "Features"  ──auto-adds by label──┐
                                                 ▼
Feature issue #45  [feature]  "Add OAuth login"
├─ body: <!--FEATURE-STATE:BEGIN--> ```json {…} ``` <!--FEATURE-STATE:END-->
├─ linked PR #123
├─ sub-issue #46  [problem][sev:high]  "Token refresh race"   OPEN
└─ sub-issue #47  [problem][sev:med]   "Missing PKCE"          CLOSED (fixed abc1234)

git config (per branch, local wiring):
  git-town-branch.<branch>.parent          ← base branch (stacking)
  branch.<branch>.feature-issue            ← issue number (exact bootstrap lookup)
```

### Feature-state JSON (in the issue body)

```json
{
  "schema": 2,
  "branch": "feat-add-oauth",
  "base": "feat-user-model",
  "pr": 123,
  "status": "in-review",
  "review_budget": null,
  "review_runs": 2,
  "review_history": [
    {"run": 1, "sha": "9fe0011", "new_blocking": 2, "new_low": 3, "regressions": 0, "summary": "…"},
    {"run": 2, "sha": "abc1234", "new_blocking": 0, "new_low": 1, "regressions": 0, "summary": "…"}
  ],
  "last_prompt": "Add PKCE support to the OAuth flow",
  "last_review": {
    "run": 2,
    "sha": "abc1234",
    "new_blocking": 0,
    "new_low": 1,
    "regressions": 0,
    "summary": "No new blocking issues; one naming nit filed. Token-refresh race from run 1 is fixed."
  },
  "updated": "2026-07-13T10:22:00Z"
}
```

`status` ∈ `planning | in-progress | in-review | needs-decision | ready | merged`. The block is
delimited by `<!--FEATURE-STATE:BEGIN-->` / `<!--FEATURE-STATE:END-->` so it can be replaced
idempotently no matter what humans add around it. Never blind-string-replace.

`review_budget` is `null` for "auto-size from the PR diff at query time" (so a growing branch earns
its extra round without anyone re-running a command) or an integer a human pinned with
`feature budget --set`. `review_history` is the append-only audit log the gate reads the
convergence trend from; `feature sync` appends an `{"event": "base-advanced", …}` marker to it,
and everything before the newest marker is excluded from both the trend and the budget count —
new base code legitimately buys a fresh round. `last_review` is only the *currently valid* clean-pass
evidence, and is nulled on invalidation, which is why it duplicates the last history entry.

Old schema-1 blocks are refused with a pointer to `feature migrate` (a one-way upgrade) rather
than read with guessed-at defaults.

## Lifecycle

```
feature create ─┐
                ▼
        planning/in-progress ──(open PR)──▶ in-review
                                              │
                          ┌───────────────────┤ review record  (files problems, bumps run)
                          ▼                    ▼
                  problem add            problem resolve / defer / reject
                          │                    │
                          └────────┬───────────┘
                                   ▼
                    gate: 0 blocking problems AND last_review.new_blocking == 0
                       ├─ yes ──────────────▶ ready ──▶ (human merges) ──▶ merged
                       └─ no ──┬─ budget left, converging ──▶ REVIEW_AGAIN (loop)
                               └─ budget spent or churning ──▶ needs-decision (human)
```

### The merge gate (pure query, three outcomes)

| Verdict | Exit | When |
| --- | --- | --- |
| `OPEN` | 0 | Zero **blocking** problems open, and the last valid review reported `new_blocking == 0`. |
| `REVIEW_AGAIN` | 10 | Blocking work outstanding, review budget remains, and the trend is converging. |
| `NEEDS_DECISION` | 20 | The budget is spent, **or** the loop is visibly not converging. A human decides. |

The codes skip 1 and 2 deliberately: those already mean "the command failed" (`sys.exit(message)`
and argparse both use them), so reusing them would make a lost git-config key or a `gh` 503 read as
"spend another review round", and a mistyped flag read as "escalate to a human".

**Only blocking problems hold the gate.** A problem blocks if it is open, carries no disposition
label, and is not explicitly `sev:low`. Stated that way round on purpose: severity is the value the
gate depends on, so an unlabelled problem — a human filing a sub-issue through the GitHub UI without
picking a label — fails *safe* and holds the merge. `sev:low` is tracked debt that ships. The two dispositions —
`deferred` (real, deliberately not fixed here) and `rejected` (not a real problem) — each require a
reason that is posted on the sub-issue, so shipping known debt is an explicit, auditable act rather
than a silent severity downgrade. Deferred problems stay **open** on purpose and are named in the
merge comment: debt you can't see isn't tracked.

Only `deferred` suppresses blocking. `rejected` expresses itself by *closing* the issue, so an OPEN
issue still carrying the label means someone overturned the rejection — a later round rediscovering
it with a real repro — and it blocks like anything else. `feature problem block` is the explicit way
to revoke a deferral; without an inverse, a disposition would be permanent, and a rediscovered
problem could never get back in front of the gate.

**Why not "zero findings".** The original gate demanded a review run that found *nothing*. The
reviewer is built to report at every altitude — real defects, nits, pre-existing issues in files the
PR merely touches — so a zero-finding run is an event that essentially never happens, and the loop
had no other exit than `feature merge --force`, which recorded no rationale. The safety property the
condition was actually protecting is narrower: *a fix must not introduce a new defect*. That needs
`new_blocking == 0`, not `findings == 0`.

**Why a budget.** A review round is the most expensive thing this workflow does (a full independent
sweep by a subagent). `budget.py` sizes it from the diff: base 2 rounds, +1 for a large diff
(>15 files or >500 changed lines), +1 if the PR touches a path the repo declared sensitive
(`git config --add feature.sensitive-path '<glob>'`), capped at 4. A human can pin it with
`feature budget --set <n>` — the "this feature matters more than its size suggests" input no agent
can infer.

**Why churn detection.** Waiting for the budget to run out wastes rounds on a loop that is clearly
stuck, so the gate escalates early on either of two signals: the last round re-opened
previously-closed problems (the fixes are reintroducing known bugs — a design problem, not a
review-more problem), or blocking findings stopped decreasing round over round.

`feature sync` upholds the clean-pass condition across base changes: when the base branch advances,
the last clean review predates the newly merged code, so sync clears `last_review` and appends an
invalidation marker to `review_history` — the gate reopens, the round count resets to the runs after
the marker (new code deserves a fresh budget), and a fresh review + `review record` is required.

The gate is still a pure query: `feature gate` never mutates state. Parking a feature at
`needs-decision` is an explicit act — `feature escalate --reason "…"` — which posts the current gate
state and the agent's recommendation to the tracking issue.

## Session bootstrap

A `SessionStart` hook runs `feature status` for the current branch and injects the result into
context. Resolution is exact, not fuzzy:

1. `branch` ← `git rev-parse --abbrev-ref HEAD`
2. `base` ← `git config git-town-branch.<branch>.parent`
3. `issue` ← `git config branch.<branch>.feature-issue`
4. Fetch the issue, parse the state block, list open sub-issues.

Result injected: *"On `feat-add-oauth`, based on `feat-user-model`, PR #123, status in-review.
Last prompt: '…'. Last review (run 2): 0 new blocking. Reviews 2/3. Gate REVIEW_AGAIN. Blocking
problems: 1 (#46 sev:high token refresh race)."*

## Prerequisites

- `gh` authenticated (`gh auth status`).
- `git-town` installed (`brew install git-town`) and initialized in the repo (`git town config`).
- Labels created once per repo: `feature create --init-labels` does this.
- A Project named "Features" (optional, for the board) with a label-based auto-add workflow.

## CLI reference

All commands are `feature <cmd>` (or the `feature` console script).

| Command | Does |
| --- | --- |
| `feature create <name> [--base <branch>] [--init-labels] [--allow-local-base]` | Create branch (stacked on `--base` via git-town, else main), worktree under `.claude/worktrees/<name>/`, feature issue with state block, and wire git config. Aborts if the base has local-only commits (they'd leak into the PR diff, which is against the *remote* base) unless `--allow-local-base` is given. |
| `feature adopt [--branch <b>] [--base <branch>] [--init-labels]` | Wire an *existing* branch into tracking (no new branch/worktree) — the on-ramp for branches created before adopting this system. |
| `feature pr <number>` | Link an existing PR number to the current branch: record it in the state block and move status to `in-review`. (Open the PR yourself with `gh`/`git town propose`; `adopt` auto-links an already-open PR.) |
| `feature status [--branch <b>] [--json]` | Print reconstructed state for a branch (used by the SessionStart hook). |
| `feature prompt <text>` | Record the latest prompt into the state block. |
| `feature review record --sha <sha> --new-blocking <n> [--new-low <n>] [--regressions <n>] --summary <text>` | Record an in-session review run: append to `review_history`, set `last_review`, post a timeline comment, and print the resulting gate verdict + next step. Refuses if no PR is linked, or if the PR's base branch ≠ the feature's tracked parent (either means a wrongly-scoped review). See "The in-session review". |
| `feature problem add --title <t> --sev <high\|med\|low> [--body <b> \| --body-file <path\|->]` | Create a problem sub-issue linked to the feature. `high`/`med` block the gate; `low` doesn't. `--body-file -` reads the detail from stdin (multi-line failure scenarios with code snippets survive intact); mutually exclusive with `--body`. |
| `feature problem resolve <number> [--commit <sha>]` | Close a problem sub-issue with a fixing-commit reference. |
| `feature problem defer <number> --reason <why>` | Real problem, deliberately not fixed in this PR: labels it `deferred`, posts the reason, keeps it **open** but out of the blocking set. |
| `feature problem reject <number> --reason <why>` | Not a real problem (false positive / by design): labels it `rejected` and closes it with the reasoning. |
| `feature problem block <number> --reason <why>` | Revoke a disposition — drops `deferred`, re-opens if closed — so the problem holds the merge again. The inverse of `defer`, needed because a later round can rediscover a deferred problem with a repro that changes the call. |
| `feature problem list [--open] [--blocking]` | List problem sub-issues with severity and disposition. `--blocking` shows only what holds the gate. |
| `feature budget [--set <n> \| --auto] [--branch <b>]` | Show the review-round budget and how much of it is used; `--set` pins it, `--auto` returns to diff-sizing. |
| `feature escalate --reason <text> [--branch <b>]` | Park the feature at `needs-decision` and post the gate state + your recommendation for a human. |
| `feature migrate [--branch <b>]` | The upgrade path for a repo onboarded on an older CLI: (re-)creates the workflow labels (`deferred`/`rejected` are newer than the original set, and `gh issue edit --add-label` hard-fails on a label the repo lacks) and upgrades the state block one-way. |
| `feature gate [--branch <b>]` | Print the verdict and the next step. Exit 0 = `OPEN`, 10 = `REVIEW_AGAIN`, 20 = `NEEDS_DECISION` (1 and 2 stay "the command itself failed"). |
| `feature merge [--branch <b>] [--force]` | Final transition: verify the gate, set status `merged`, close the feature issue. Does not merge the PR itself. |
| `feature sync [--branch <b>] [--stack]` | Sync the branch with its base via `git town sync` (recursive over ancestors; `--stack` for the whole stack). If the base advanced, invalidates the last review so the gate reopens and a fresh review + `review record` is required. Delegates to git-town; does not auto-resolve conflicts. |

## The in-session review

The review runs **inside the agent's own Claude Code session's process**, not as a spawned
`claude` subprocess. But the *finder* runs in a fresh subagent (a sub-task of this process), and
the parent session does dedup + recording. The split matters:

```
agent session ─▶ spawn finder SUBAGENT (PR# only — NO tracked state loaded yet)
                    │                    │
                    │            Skill(review, <PR#>) ─▶ raw findings returned to parent
                    ▼
   parent NOW loads tracked state, and for each finding decides (real code + open problems):
       ├─ reproducible?      no ─▶ drop (false positive)
       ├─ caused by THIS PR? no ─▶ sev:low, or file + `problem defer` (never blocking)
       └─ duplicate of a tracked problem? (judged by MEANING)
              ├─ matches an OPEN issue   ─▶ skip (already tracked)
              ├─ matches a CLOSED issue  ─▶ REGRESSION: `gh issue reopen` + count it
              └─ distinct                ─▶ `feature problem add --sev high|med|low`
                    │
   `feature review record --new-blocking <high+med filed & regressed> --new-low <n>
                          --regressions <blocking reopened>`   (refuses if no PR linked;
                          idempotent per sha, so a retry after a failed call spends no extra round)
                    │
                    ▼
   prints the verdict: OPEN (ship) / REVIEW_AGAIN (one more round) / NEEDS_DECISION (escalate)
```

**Why scope the review through the PR.** The finder is handed the **PR number**, not a diff
range. A PR's diff is `git diff <pr-base>...HEAD` (three-dot, from the merge-base). Verified
empirically on a stacked scenario: with branch A off `main` and B off A, `git diff main...B`
includes A's commits (wrong scope for reviewing B), while `git diff A...B` is B's own work alone.
So the review is correct **iff B's PR is opened against its parent A**, not `main` — the PR base
is the scope boundary, which is why opening the PR against the parent branch is load-bearing, not
cosmetic. `feature review record` enforces this: it hard-refuses when no PR is linked (no well-defined
scope), AND when the PR's `baseRefName` ≠ the feature's tracked parent (base resolved the same
way `feature status` does — git-town pointer, then recorded base) — a PR accidentally opened
against `main` for a stacked feature is caught before it can record a wrongly-scoped review.
Push + open the PR against the parent + `feature pr <n>` is a precondition of the review loop,
not an afterthought.

**Why the finder runs before tracked state is loaded.** The finder must find blind — no memory of
prior runs, no list of already-filed problems to "just confirm." Rather than load that state and
then instruct the agent not to pass it along (a rule it must remember to honor every run), the
ordering enforces it structurally: the parent spawns the finder FIRST, with only the PR number in
hand, and loads `feature status` / open problems only afterward for its own dedup. There is
nothing to leak because the leakable context doesn't exist yet.

**Why the finder is pinned to the feature worktree.** Feature worktrees live *under* the main
checkout (`.claude/worktrees/<name>`), so the main checkout's path is a prefix of the worktree
path and holds the same files on a *different* branch (the base). A finder that drifts to "the
repo root" — a stray `cd`, a glob from the wrong cwd — ends up reading the pre-feature code while
the PR diff is from the feature branch, producing phantom findings and missing real ones. The
finder subagent is therefore told its working directory is the feature worktree and that it must
never read from the main checkout. `feature status` surfaces the worktree path (`git worktree
list` → the entry whose `branch` is this feature's) so the parent can hand it over explicitly.

**Why the finder runs in a fresh subagent.** The gate's safety property is that the *final*
review is a genuine independent sweep. If the finder ran in the parent's own context, the second
and later runs in a session would be poisoned: the agent remembers the earlier runs, so instead
of rediscovering issues blind it drifts into "confirm #12 and #14 got fixed" — a verification
mindset that misses bugs the fixes themselves introduced. A subagent gets a clean context every
run, so run 1 and run 5 find with equal rigor. Splitting find (blind subagent) from dedup
(parent, which needs the tracked problem list) gives each half the context it needs.

**Why a subagent, not a headless `claude`.** An earlier design spawned `claude -p "/code-review"`
as a *subprocess* and forced its findings into a JSON schema. That second, long-lived `claude`
process was reaped by the session manager after ~10 minutes with SIGTERM→SIGKILL — the
review never finished. Verified empirically: no jetsam (OOM) events fired, and short `claude -p`
calls succeed; only the long review child was killed, and only when spawned as a grandchild of a
managed session. A subagent is a sub-task *inside the already-alive session process* — there is
no second `claude` to reap — so it keeps the reaping fix while restoring reviewer independence.

**Dedup is semantic, not string-based.** An even earlier design fingerprinted `sha1(file +
title)`, but two reviews word the same bug differently every time, so a title fingerprint almost
never matched across runs — breaking both dedup and regression detection. Instead the agent,
which is reading the real code, judges *by meaning* whether each finding is the same underlying
bug as an already-tracked problem: matches an OPEN issue → skip; matches a CLOSED issue →
regression (reopen, count as new); distinct → file. It also confirms reproducibility so false
positives never reach the gate.

`feature review record --new-blocking <n>` takes the count of genuinely-new *blocking* problems plus
blocking regressions; a clean pass is `--new-blocking 0`, which (with zero open blocking sub-issues)
opens the gate. `--new-low` doesn't gate anything — it keeps the record honest about what a "clean"
round actually found. `--regressions` counts re-opened *blocking* problems only, and feeds the churn
signal: a re-opened low-severity nit is not evidence the design is wrong, and escalating on one would
contradict the rule that low severity never gates.

**Severity is now load-bearing, so it's auditable.** An agent could open the gate by calling
everything `sev:low`, so the defenses are made of records, not restrictions: every disposition
demands a reason posted on the issue, `feature status`/`gate`/`merge` always print the non-blocking
debt that ships, and per-severity counts land in `review_history` for every round. A human reading
the tracking issue can see a review that filed five lows and no highs.

## Open questions / follow-ups

- `addSubIssue` GraphQL is still preview-ish; behavior is pinned in the CLI wrapper.
- Whether to mirror problems to PR review threads (native resolved-thread UX) — deferred.
- Whether to version state in git (orphan `feature-state` branch) as a diffable backup —
  deferred; Issues + API history cover the current need.
