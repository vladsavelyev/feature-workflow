"""The merge gate — pure decision logic, kept free of I/O so it's unit-testable.

The gate has **three** outcomes, because a review loop has three real endings:

  OPEN            — nothing blocking is left, and a review has covered the code that is on the
                    branch right now. Ship it.
  REVIEW_AGAIN    — blocking work is outstanding and there is review budget left to spend.
  NEEDS_DECISION  — the loop is not converging on its own: the budget is spent, or the trend
                    says another round won't help. A human decides: ship with recorded
                    deferrals, buy another round, split the PR, or redesign.

Why three. The old gate demanded a review run with *zero* findings. But the reviewer is built
to report at every altitude — real defects, nits, pre-existing issues in touched files — so a
zero-finding run is an event that never happens, and the loop had no other exit. Only *blocking*
findings (see `problems.py`) hold the gate now, and when even those won't settle, the gate says
so out loud instead of asking for one more expensive round forever.
"""

from dataclasses import dataclass
from enum import StrEnum

# What to do next, per verdict — the gate's most useful output for an agent driving the loop.
_NEXT_OPEN = "Human merges the PR, then `feature merge`."
_NEXT_DECISION = (
    "STOP reviewing — a human decides. Run `feature escalate --reason '<what you recommend>'`, "
    "then one of: ship the rest as debt (`feature problem defer <#> --reason '<why>'`), buy "
    "another round (`feature budget --set <n>`), or split/redesign the PR."
)


class Verdict(StrEnum):
    OPEN = "OPEN"
    REVIEW_AGAIN = "REVIEW_AGAIN"
    NEEDS_DECISION = "NEEDS_DECISION"


# Exit codes are the machine interface. Deliberately NOT 1 and 2: those are already spoken for by
# "the command failed" — an uncaught exception and `sys.exit("message")` exit 1, argparse usage
# errors exit 2. If REVIEW_AGAIN were 1, a lost `feature-issue` git-config key or a `gh` 503 would
# read as "spend another review round", and a mistyped flag would read as "escalate to a human".
EXIT_CODES = {Verdict.OPEN: 0, Verdict.REVIEW_AGAIN: 10, Verdict.NEEDS_DECISION: 20}


@dataclass(frozen=True)
class GateDecision:
    verdict: Verdict
    reasons: list[str]
    next_step: str
    rounds_used: int
    budget: int

    @property
    def open(self) -> bool:
        return self.verdict is Verdict.OPEN

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]

    def __str__(self) -> str:
        used = f"reviews {self.rounds_used}/{self.budget}"
        if self.open:
            return f"OPEN: nothing blocking, last review clean ({used}). Safe to merge."
        return f"{self.verdict}: " + "; ".join([*self.reasons, used])


def rounds_since_invalidation(history: list[dict]) -> list[dict]:
    """The review runs that still describe the current code, newest last.

    `review_history` interleaves review entries (which have a `run`) with invalidation markers
    written by `feature sync` when the base advances (which have an `event`). Everything before
    the newest marker reviewed code that has since changed underneath it, so neither the budget
    nor the convergence trend should count it — a base sync legitimately buys a fresh round.
    """
    runs: list[dict] = []
    for entry in reversed(history):
        if "run" not in entry:
            break
        runs.append(entry)
    return list(reversed(runs))


def churn_reason(rounds: list[dict]) -> str | None:
    """Why spending another review round looks futile, or None if the loop is converging.

    Two signals, both cheap and explainable:
      * the last round re-opened previously-closed *blocking* problems — the fixes are
        reintroducing known bugs, which is a design smell, not a review-more smell;
      * blocking findings stopped decreasing round over round — the loop is treading water.

    Both key off blocking counts only. A re-opened low-severity nit is not evidence that the design
    is wrong, and escalating on it would contradict the rule that low severity never gates.
    """
    if not rounds or rounds[-1].get("placeholder"):
        return None
    last = rounds[-1]
    if last["regressions"]:
        return (
            f"run {last['run']} re-opened {last['regressions']} previously-closed blocking "
            f"problem(s) — the fixes are reintroducing known bugs"
        )
    # Placeholder rounds carry no counts (see `state.migrate`), so a trend through one is
    # meaningless — the budget check below is what catches those features.
    if len(rounds) >= 2 and not any(r.get("placeholder") for r in rounds[-2:]):
        previous, current = rounds[-2]["new_blocking"], last["new_blocking"]
        # Strictly increasing, and both nonzero. One flat round (1 then 1) is the most common shape
        # of a converging review, and firing on it made the auto-sized 3rd and 4th rounds nearly
        # unreachable — the advertised 2-4 budget was effectively always 2. A rising count is the
        # real "getting worse" signal; a plateau is left to the budget to bound.
        if current and previous and current > previous:
            return (
                f"blocking findings are rising ({previous} then {current}) — "
                f"another round is unlikely to converge"
            )
    return None


def covers_head(last_review: dict | None, head_sha: str) -> bool:
    """Whether the last valid review looked at the code that is on the branch right now.

    Short and long shas both occur (the CLI hands out `--short`, a caller may pass a full one), so
    compare by prefix in either direction rather than demanding identical strings.
    """
    if last_review is None:
        return False
    reviewed = last_review["sha"]
    return reviewed.startswith(head_sha) or head_sha.startswith(reviewed)


def evaluate(
    *,
    blocking_open: int,
    last_review: dict | None,
    history: list[dict],
    budget: int,
    head_sha: str,
    budget_is_explicit: bool = False,
) -> GateDecision:
    """Decide the gate from the open blocking problems and whether a review covers the current code.

    `blocking_open` counts only problems that hold the merge (see `problems.is_blocking`):
    low-severity findings and anything deferred with a recorded reason are excluded by design.

    Note what the second condition is NOT. It used to be "the last review reported zero new blocking
    problems", which reads a *count* — and a count goes stale the moment a problem is disposed of:
    after a human said "ship the rest as debt" and the agent deferred everything, the gate still saw
    the old nonzero count and stayed shut with no way to reopen it. Coverage asks the question the
    safety property actually cares about — *has this code been reviewed?* — and answers both cases
    correctly: a fix commits, so HEAD moves and a fresh review is required; a deferral doesn't, so
    the existing review still stands and the human's decision takes effect. Counts remain in
    `review_history` for the convergence trend.
    """
    rounds = rounds_since_invalidation(history)
    used = len(rounds)

    reasons: list[str] = []
    if blocking_open > 0:
        reasons.append(f"{blocking_open} blocking problem(s) open")
    if last_review is None:
        reasons.append("no valid review run recorded")
    elif not covers_head(last_review, head_sha):
        reasons.append(
            f"the last review looked at {last_review['sha']}, but the branch is now at {head_sha} — "
            f"that code has never been reviewed"
        )

    if not reasons:
        return GateDecision(Verdict.OPEN, [], _NEXT_OPEN, used, budget)

    # An explicitly raised budget outranks the churn heuristic. Churn exists to escalate *early*, so
    # a human who has already seen that escalation and chosen to spend another round has answered it;
    # re-deriving churn from the same unchanged history would make `budget --set` a silent no-op.
    churn = None if (budget_is_explicit and used < budget) else churn_reason(rounds)
    if churn:
        return GateDecision(Verdict.NEEDS_DECISION, [*reasons, churn], _NEXT_DECISION, used, budget)
    if used >= budget:
        return GateDecision(
            Verdict.NEEDS_DECISION,
            [*reasons, f"review budget spent ({used}/{budget})"],
            _NEXT_DECISION,
            used,
            budget,
        )
    next_step = (
        "Fix the blocking problems (or give one a recorded disposition), then re-review and "
        "`feature review record`."
        if blocking_open > 0
        else "Review the PR and `feature review record` the run."
    )
    return GateDecision(Verdict.REVIEW_AGAIN, reasons, next_step, used, budget)
