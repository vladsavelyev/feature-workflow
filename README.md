# feature-workflow

A CLI + SessionStart hook for running multiple Claude features in parallel: each feature gets
a branch, a worktree, a PR, and a GitHub tracking issue. Features can be **stacked** on each
other (via git-town). Review-found problems are tracked as GitHub sub-issues, and a **merge
gate** blocks until the *blocking* ones are resolved and a review comes back clean. A new Claude
session on an existing branch reconstructs full context (base branch, last prompt, last review,
blocking problems) from GitHub.

The gate has three verdicts, because "review until a run finds nothing" never terminates — a real
reviewer always finds *something*:

| `feature gate` | Exit | Meaning |
| --- | --- | --- |
| `OPEN` | 0 | No blocking problems, last review clean. Ship it. |
| `REVIEW_AGAIN` | 10 | Blocking work left, review budget remains. |
| `NEEDS_DECISION` | 20 | Budget spent, or the loop is churning. A human decides: ship the debt, split, or redesign. |

Only an explicit `sev:low` is non-blocking: it's tracked debt that ships. A problem with no
severity label blocks until someone labels it, so a missing label can't quietly loosen the gate.
Anything you decide to ship unfixed gets an explicit, reasoned disposition (`feature problem
defer`) instead of a silent severity downgrade, and reviews are budgeted (2–4 rounds, sized from
the diff) so a non-converging loop ends in a recorded decision rather than unbounded spend.

See [docs/design.md](docs/design.md) for the full design, and
[docs/motivation.md](docs/motivation.md) for the design history — why it's shaped this way and
what we tried and backed out of (read before making structural changes).

## Install

```sh
uv tool install --editable .     # exposes the `feature` command globally
# or, in a project:
uv sync && uv run feature --help
```

## Prerequisites

- [`gh`](https://cli.github.com/) authenticated (`gh auth status`).
- [`git-town`](https://www.git-town.com/) installed (`brew install git-town`) — needed for
  restacking stacked branches. Base-branch tracking works without it.
- Optional, per repo: declare the paths where review mistakes are expensive, so a PR touching
  them earns an extra review round:
  ```sh
  git config --add feature.sensitive-path 'src/auth/*'
  git config --add feature.sensitive-path 'migrations/*'
  ```

Reviews run **in-session** (not as a spawned `claude` subprocess): the agent already in a Claude
Code session runs `/code-review` on the feature's **PR** in a fresh subagent so each re-run finds
blind, then dedups the findings against tracked problems, files the new ones, and records the run
via this CLI. A linked PR is required — the PR diff defines the review scope (correct even for
stacked branches), and `feature review record` refuses to run without one. There is no headless
review subcommand.

## Quick start

```sh
feature create add-oauth --init-labels        # branch + worktree + tracking issue
feature prompt "Add PKCE support"              # record what you asked Claude to do
gh pr create ... && feature pr 123             # link the PR (required before any review)
# review in-session: finder subagent reviews PR #123, parent dedups vs tracked problems, then:
feature problem add --title "…" --sev high    # file each genuinely-new problem
feature review record --sha $(git rev-parse HEAD) --new-blocking 2 --new-low 3 \
    --summary "Token refresh race + unchecked PKCE verifier; 3 nits filed as low"
feature problem list --blocking                # what actually holds the merge
# ... fix the blocking ones, then:
feature problem resolve 46 --commit abc1234
feature problem reject 48 --reason "verifier is validated upstream in middleware.py:40"
# re-review, then record a clean pass (0 new blocking) to open the gate:
feature review record --sha $(git rev-parse HEAD) --new-blocking 0 --new-low 1 \
    --summary "Re-reviewed oauth.py; #46 fixed; one naming nit filed"
feature gate                                   # exit 0 = safe to merge, 20 = ask a human
```

Adopt a branch you already have:

```sh
feature adopt --init-labels
```

## Commands

| Command | Does |
| --- | --- |
| `feature create <name> [--base <b>] [--init-labels]` | Branch + worktree + tracking issue; wire git config. |
| `feature adopt [--branch <b>] [--base <b>]` | Wire an existing branch into tracking. |
| `feature prompt <text>` | Record the latest prompt. |
| `feature pr <number>` | Link a PR to the feature. |
| `feature review record --sha <s> --new-blocking <n> [--new-low <n>] [--regressions <n>] --summary <t>` | Record an in-session review run (moves the gate) and print the resulting verdict. |
| `feature problem add --title <t> --sev <high\|med\|low>` | Add a problem sub-issue (anything but `low` blocks the gate). |
| `feature problem resolve <number> [--commit <sha>]` | Close a problem sub-issue as fixed. |
| `feature problem defer <number> --reason <why>` | Real problem, shipped unfixed: stays open, stops blocking, reason recorded. |
| `feature problem reject <number> --reason <why>` | False positive / by design: closed with the reasoning. |
| `feature problem list [--open] [--blocking]` | List problem sub-issues with severity and disposition. |
| `feature budget [--set <n> \| --auto]` | Show or override the review-round budget. |
| `feature escalate --reason <t>` | Park at `needs-decision` with your recommendation for a human. |
| `feature status [--branch <b>] [--json]` | Reconstruct and print feature state. |
| `feature sync [--branch <b>] [--stack]` | Sync the branch with its base via git-town; if the base advanced, invalidates the last review so the gate reopens. |
| `feature gate [--branch <b>]` | Exit 0 = OPEN, 10 = REVIEW_AGAIN, 20 = NEEDS_DECISION (1 and 2 stay "the command failed"). |
| `feature merge [--branch <b>] [--force]` | Mark merged and close the issue (checks gate; names the debt that ships). |
| `feature migrate [--branch <b>]` | Upgrade a repo for this CLI version: (re-)create the workflow labels and the feature issue's state block. Run it once per repo after upgrading. |

## SessionStart hook

Wire the hook so every Claude session on a tracked branch gets its context injected. In your
`.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "feature-session-context"}]}
    ]
  }
}
```

(or `uv run python -m feature_workflow.session_start_hook`).

## Claude skill

`skills/feature-workflow/SKILL.md` teaches Claude Code how to drive this CLI. It's tracked here
(source of truth) and made live by symlinking it into your Claude skills dir — no copying, edits
apply instantly:

```sh
ln -s "$PWD/skills/feature-workflow" ~/.claude/skills/feature-workflow
```

Claude discovers it on the next session. Keep it in sync with the CLI: when you add or change a
command, update the SKILL.md in the same commit.

## Development

```sh
uv sync
uv run pytest        # unit tests (state block, gate, review/verify schemas)
uv run ruff check .
```
