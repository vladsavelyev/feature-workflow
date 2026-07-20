"""The merge gate — pure decision logic, kept free of I/O so it's unit-testable.

The gate opens only when BOTH hold:
  1. zero open problem sub-issues, and
  2. the last review run found 0 new problems (a clean pass AFTER the last fix).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GateDecision:
    open: bool
    reasons: list[str]

    def __str__(self) -> str:
        if self.open:
            return "OPEN: 0 open problems and a clean last review. Safe to merge."
        return "CLOSED: " + "; ".join(self.reasons)


def evaluate(open_problem_count: int, last_review: dict | None) -> GateDecision:
    reasons: list[str] = []
    if open_problem_count > 0:
        reasons.append(f"{open_problem_count} open problem(s)")
    if last_review is None:
        reasons.append("no review run recorded")
    elif last_review["new_problems"] != 0:
        reasons.append(f"last review found {last_review['new_problems']} new problem(s)")
    return GateDecision(open=not reasons, reasons=reasons)
