"""The merge gate — pure decision logic, kept free of I/O so it's unit-testable.

The gate has **three** outcomes, because a review loop has three real endings:

  OPEN            — nothing blocking is left and the last review found no new blocking
                    problems. Ship it.
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


# Exit codes are the machine interface: 0 ship, 1 keep going, 2 human needed.
EXIT_CODES = {Verdict.OPEN: 0, Verdict.REVIEW_AGAIN: 1, Verdict.NEEDS_DECISION: 2}


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
      * the last round re-opened previously-closed problems — the fixes are reintroducing known
        bugs, which is a design smell, not a review-more smell;
      * blocking findings stopped decreasing round over round — the loop is treading water.
    """
    if not rounds:
        return None
    last = rounds[-1]
    if last["regressions"]:
        return (
            f"run {last['run']} re-opened {last['regressions']} previously-closed problem(s) — "
            f"the fixes are reintroducing known bugs"
        )
    if len(rounds) >= 2 and last["new_blocking"] and last["new_blocking"] >= rounds[-2]["new_blocking"]:
        return (
            f"blocking findings are not decreasing ({rounds[-2]['new_blocking']} then "
            f"{last['new_blocking']}) — another round is unlikely to converge"
        )
    return None


def evaluate(*, blocking_open: int, last_review: dict | None, history: list[dict], budget: int) -> GateDecision:
    """Decide the gate from the blocking-problem count, the last review, and the trend.

    `blocking_open` counts only problems that hold the merge (see `problems.is_blocking`):
    low-severity findings and anything given a recorded disposition are excluded by design.
    """
    rounds = rounds_since_invalidation(history)
    used = len(rounds)

    reasons: list[str] = []
    if blocking_open > 0:
        reasons.append(f"{blocking_open} blocking problem(s) open")
    if last_review is None:
        reasons.append("no valid review run recorded")
    elif last_review["new_blocking"] != 0:
        reasons.append(f"last review found {last_review['new_blocking']} new blocking problem(s)")

    if not reasons:
        return GateDecision(Verdict.OPEN, [], _NEXT_OPEN, used, budget)

    churn = churn_reason(rounds)
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
