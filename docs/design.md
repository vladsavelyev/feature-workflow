# Parallel feature workflow

A system for working on many features in parallel with Claude: each feature gets a
branch, a matching PR, a matching worktree, and a GitHub tracking issue. Features can be
**stacked** on each other. Review-found problems are tracked as sub-issues and must all be
resolved before the PR merges. A new Claude session on an existing branch reconstructs full
context (base branch, last prompt, last review, open problems) from GitHub.

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
  "schema": 1,
  "branch": "feat-add-oauth",
  "base": "feat-user-model",
  "pr": 123,
  "status": "in-review",
  "review_runs": 2,
  "last_prompt": "Add PKCE support to the OAuth flow",
  "last_review": {
    "run": 2,
    "sha": "abc1234",
    "new_problems": 0,
    "summary": "No new issues; token-refresh race from run 1 still open."
  },
  "updated": "2026-07-13T10:22:00Z"
}
```

`status` ∈ `planning | in-progress | in-review | ready | merged`. The block is delimited by
`<!--FEATURE-STATE:BEGIN-->` / `<!--FEATURE-STATE:END-->` so it can be replaced
idempotently no matter what humans add around it. Never blind-string-replace.

## Lifecycle

```
feature create ─┐
                ▼
        planning/in-progress ──(open PR)──▶ in-review
                                              │
                          ┌───────────────────┤ review record  (adds problems, bumps run)
                          ▼                    ▼
                  problem add            problem resolve
                          │                    │
                          └────────┬───────────┘
                                   ▼
                      gate: 0 open problems AND last_review.new_problems == 0
                                   ▼
                                 ready ──▶ (human merges) ──▶ merged, close feature issue
```

### The merge gate (pure query)

The gate opens when **both** hold:

1. The feature issue has **zero open sub-issues** (all problems resolved).
2. The **last review run reported `new_problems == 0`** (a review that found nothing new).

Condition 2 matters: it forces at least one clean review pass *after* the last fix, so a fix
that introduced a new problem can't slip through. The human still clicks merge; the gate only
gives a green light.

`feature sync` upholds condition 2 across base changes: when the base branch advances, the
last clean review predates the newly merged code, so sync clears `last_review` — the gate
reopens and a fresh review + `review record` is required before merge.

## Session bootstrap

A `SessionStart` hook runs `feature status` for the current branch and injects the result into
context. Resolution is exact, not fuzzy:

1. `branch` ← `git rev-parse --abbrev-ref HEAD`
2. `base` ← `git config git-town-branch.<branch>.parent`
3. `issue` ← `git config branch.<branch>.feature-issue`
4. Fetch the issue, parse the state block, list open sub-issues.

Result injected: *"On `feat-add-oauth`, based on `feat-user-model`, PR #123, status in-review.
Last prompt: '…'. Last review (run 2): 0 new. Open problems: 1 (#46 sev:high token refresh
race)."*

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
| `feature review record --sha <sha> --new <n> --summary <text>` | Record an in-session review run: bump `review_runs`, set `last_review`, post a timeline comment. Refuses if no PR is linked, or if the PR's base branch ≠ the feature's tracked parent (either means a wrongly-scoped review). See "The in-session review". |
| `feature problem add --title <t> --sev <high\|med\|low> [--body <b> \| --body-file <path\|->]` | Create a problem sub-issue linked to the feature. `--body-file -` reads the detail from stdin (multi-line failure scenarios with code snippets survive intact); mutually exclusive with `--body`. |
| `feature problem resolve <number> [--commit <sha>]` | Close a problem sub-issue with a fixing-commit reference. |
| `feature problem list [--open]` | List problem sub-issues and their state. |
| `feature gate [--branch <b>]` | Exit 0 if the merge gate is open, else 1 with the reason. |
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
   parent NOW loads tracked state, and for each finding decides BOTH (real code + open problems):
       ├─ reproducible?  no ─▶ drop (false positive)
       └─ duplicate of a tracked problem? (judged by MEANING)
              ├─ matches an OPEN issue   ─▶ skip (already tracked)
              ├─ matches a CLOSED issue  ─▶ REGRESSION: `gh issue reopen` + count as new
              └─ distinct                ─▶ `feature problem add` (new sub-issue)
                    │
   `feature review record`: new_problems = filed + regressed  (refuses to run if no PR linked)
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

`feature review record --new <n>` takes the count of genuinely-new plus regressed problems; a
clean pass is `--new 0`, which (with zero open sub-issues) opens the gate.

## Open questions / follow-ups

- `addSubIssue` GraphQL is still preview-ish; behavior is pinned in the CLI wrapper.
- Whether to mirror problems to PR review threads (native resolved-thread UX) — deferred.
- Whether to version state in git (orphan `feature-state` branch) as a diffable backup —
  deferred; Issues + API history cover the current need.
