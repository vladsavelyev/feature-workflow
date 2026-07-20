# Motivation & design history

This records *why* feature-workflow is shaped the way it is — the decisions, the dead ends, and
the course-corrections from the session that built it. `design.md` describes the system as it
stands; this file explains how it got there and what we deliberately rejected. Read it before
making a structural change, so you don't reintroduce something we already backed out of.

## The problem

Work on many features in parallel with an AI agent: each feature needs a branch, a matching PR,
a matching worktree, and some features stack on each other. We wanted an issue tracker where a
ticket matches a feature, and where a review populates found problems with statuses — so that
once no *new* problems are found and all are resolved, the feature can merge and the ticket
closes. And a new session on an existing branch should reconstruct its context: base branch,
last prompt, last review, open problems.

## Decisions that stuck

**GitHub Issues are the source of truth, not GitHub Projects.** Projects is a nice human
dashboard but a poor machine-readable store (verbose GraphQL, weak sub-item modeling, rate
limits). Issues give org-wide access, permissions, notifications, and search for free. The
compromise that made Issues work as a *machine* store: a fenced ```json block between
HTML-comment markers in the feature issue body — humans read a normal issue, the CLI reads one
deterministic block. Review problems are **sub-issues** so each gets its own state and timeline.

**State lives remotely (Issues) keyed by branch, never in a file on the feature branch.** A
state file committed to the feature branch would churn and conflict on every rebase/restack
across a stack. Per-branch wiring (base branch, issue number) lives in git config instead.

**Don't reinvent stacking — delegate to git-town.** git-town stores the parent pointer in
`git-town-branch.<name>.parent`, which is exactly the "base branch" a session bootstrap needs.
We verified the interop empirically: our `create`/`adopt` write that key, and `git town
config get-parent` / `git town branch` read it back and render the full stack. `feature sync`
is likewise a thin wrapper over `git town sync` (recursive ancestor sync); we do **not**
implement restacking ourselves.

**The merge gate is a pure function of two conditions:** zero open problems AND the last review
found zero new problems. The second condition is the subtle one — it forces at least one clean
review pass *after* the last fix, so a fix that introduces a new bug can't slip through. This is
also why `feature sync` clears `last_review` when the base advances: the prior clean pass no
longer covers the newly merged code.

**Abort-fast, never hide problems.** Every subprocess wrapper crashes loudly rather than
returning an empty/default result. This earned its keep repeatedly: when a review subprocess
hit a transient API error or a timeout, the wrapper *crashed instead of recording a false clean
review*, so the gate was never wrongly opened. A killed run left no partial state.

## Course-corrections (things we built then backed out of)

These are the expensive lessons. Re-litigate them only with new evidence.

**1. Fingerprint-based dedup → semantic dedup.** The first review loop stamped a
`sha1(file + normalized-title)` fingerprint in each problem's body to detect duplicates and
regressions deterministically across runs. It worked, but it was machinery in service of a
headless reviewer we later removed (see #3). With reviews now running in-session, the reviewing
agent dedups by *meaning* (the same bug is titled differently every review) against the tracked
open/closed problems it already sees. The fingerprint stamping was removed as dead weight.

**2. Hand-rolled review prompt → built-in `/code-review`.** The reviewer started as a
hand-written prompt ("you are a senior reviewer, use your read-only tools to inspect
surrounding code…"). That was wrong twice over: it reimplemented a maintained skill, and the
"inspect surrounding code" instruction actively *amplified* the reviewer's exploration time.
The correction: invoke the real built-in `/code-review`, which already knows how to review and
runs its own multi-angle finders. We verified headlessly that `/code-review` + `--json-schema`
lands validated findings in the result envelope's `structured_output` — so structured capture
*was* possible, contradicting an earlier wrong assumption that it only posts PR comments.
(Note: a project-local `code-review` skill was retired; the built-in is the one to use.)

**3. Headless `claude -p` reviewer → in-session review.** Even with `/code-review`, spawning a
separate `claude -p` subprocess to run it was the deepest dead end. It was slow (~20–30 min per
run, a single long serial chain), and — the killer — a second long-lived `claude` subprocess
got reaped by the session manager after ~10 min, so runs failed non-deterministically.
Mitigations we tried and then abandoned: a 600s timeout (too aggressive — killed a healthy
review pass), then 1200s + `--timeout`, then parallelizing the verification pass with a thread
pool. All of that complexity evaporated once we moved the review **in-session**: no second
`claude` process to reap, no cold start. `feature review run`, its adversarial-verification
pass, the timeout knobs, and the concurrency were all removed; `feature review record` remains
for the agent to report the result.

**4. In-context finder → fresh-subagent finder.** Moving in-session (see #3) first ran the
finder in the agent's *own* context. That surfaced a subtler bug: re-running a review in the
same session poisons it. On run 2+ the agent remembers run 1, so instead of rediscovering issues
blind it drifts into "confirm the tracked problems got fixed" — a verification mindset that
misses bugs the fixes themselves introduced. Since the gate's safety hinges on the *final*
review being an independent sweep, this quietly undermined the whole point. The correction: run
the finder in a fresh **subagent** (a sub-task of the live session process — still nothing for
the session manager to reap), passing only the diff scope and *not* the tracked-problem list, so every run finds
blind. The parent keeps dedup + recording, because that half genuinely needs the tracked state.

The through-line of all three: **don't reimplement or spawn what the surrounding tools already
provide.** Delegate stacking to git-town, reviewing to `/code-review`, and run the review in the
session that's already open.

## Process notes

- **The tool was dogfooded on itself.** Running `feature review run` against its own code (back
  when it existed) surfaced four real bugs in this codebase — a hardcoded pagination cap, a
  blanket `except` that violated abort-fast, a closed-fingerprint regression hole, and an
  `adopt --base` override that was silently dropped — all fixed. Later, it drove a real
  frontend change (responsive status bar) end to end, where the (then-present) verification pass
  correctly refuted a plausible-but-false CSS finding before it could close the gate.
- **The tool was extracted from a larger project to its own repo** once it stabilized, to be
  usable across other repos before/without merging back. The original branch/PR/issue were
  closed and deleted to avoid divergence; this repo is the sole source of truth.

## Deliberately deferred

- Mirroring problems to PR review threads (native resolved-thread UX).
- Versioning state in git (an orphan `feature-state` branch) as a diffable backup — Issues +
  their API history cover the current need.
