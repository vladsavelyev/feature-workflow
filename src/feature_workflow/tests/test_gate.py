"""Tests for the merge-gate decision logic — the tri-state verdict and the churn signals."""

from feature_workflow.gate import Verdict, churn_reason, evaluate, rounds_since_invalidation


def run(n: int, *, blocking: int = 0, low: int = 0, regressions: int = 0) -> dict:
    return {
        "run": n,
        "sha": f"sha{n}",
        "new_blocking": blocking,
        "new_low": low,
        "regressions": regressions,
        "summary": f"run {n}",
    }


CLEAN = run(2)
DIRTY = run(1, blocking=2)


def test_open_when_nothing_blocking_and_last_review_clean():
    d = evaluate(blocking_open=0, last_review=CLEAN, history=[DIRTY, CLEAN], budget=3)
    assert d.verdict is Verdict.OPEN
    assert d.open
    assert d.exit_code == 0
    assert d.reasons == []


def test_low_severity_findings_do_not_hold_the_gate():
    """The whole point: a run that found only nits is a clean pass."""
    nits_only = run(2, low=4)
    d = evaluate(blocking_open=0, last_review=nits_only, history=[nits_only], budget=3)
    assert d.verdict is Verdict.OPEN


def test_review_again_while_budget_remains():
    d = evaluate(blocking_open=2, last_review=DIRTY, history=[DIRTY], budget=3)
    assert d.verdict is Verdict.REVIEW_AGAIN
    assert d.exit_code == 1
    assert "2 blocking problem(s) open" in d.reasons
    assert d.rounds_used == 1


def test_needs_decision_when_budget_spent():
    history = [run(1, blocking=3), run(2, blocking=1)]
    d = evaluate(blocking_open=1, last_review=history[-1], history=history, budget=2)
    assert d.verdict is Verdict.NEEDS_DECISION
    assert d.exit_code == 2
    assert any("budget spent (2/2)" in r for r in d.reasons)


def test_needs_decision_on_churn_before_budget_is_spent():
    """Findings not decreasing → escalate early rather than burn the remaining rounds."""
    history = [run(1, blocking=2), run(2, blocking=3)]
    d = evaluate(blocking_open=3, last_review=history[-1], history=history, budget=4)
    assert d.verdict is Verdict.NEEDS_DECISION
    assert any("not decreasing" in r for r in d.reasons)


def test_regression_is_immediate_churn():
    history = [run(1, blocking=2), run(2, blocking=1, regressions=1)]
    d = evaluate(blocking_open=1, last_review=history[-1], history=history, budget=4)
    assert d.verdict is Verdict.NEEDS_DECISION
    assert any("re-opened 1 previously-closed" in r for r in d.reasons)


def test_converging_run_still_reviews_again():
    history = [run(1, blocking=5), run(2, blocking=2)]
    d = evaluate(blocking_open=2, last_review=history[-1], history=history, budget=4)
    assert d.verdict is Verdict.REVIEW_AGAIN


def test_closed_when_no_review_recorded():
    d = evaluate(blocking_open=0, last_review=None, history=[], budget=2)
    assert not d.open
    assert "no valid review run recorded" in d.reasons
    assert d.verdict is Verdict.REVIEW_AGAIN


def test_no_review_and_no_budget_left_needs_decision():
    """A sync-invalidated review with an exhausted budget is a human call, not a silent block."""
    history = [run(1, blocking=1), run(2, blocking=1)]
    d = evaluate(blocking_open=0, last_review=None, history=history, budget=2)
    assert d.verdict is Verdict.NEEDS_DECISION


def test_rounds_reset_after_a_base_change():
    """`feature sync` marks the history; rounds before the marker don't spend the new budget."""
    history = [run(1, blocking=2), run(2, blocking=2), {"event": "base-advanced", "sha": "abc"}]
    assert rounds_since_invalidation(history) == []
    d = evaluate(blocking_open=0, last_review=None, history=history, budget=2)
    assert d.verdict is Verdict.REVIEW_AGAIN
    assert d.rounds_used == 0


def test_churn_trend_ignores_rounds_before_a_base_change():
    history = [run(1, blocking=2), run(2, blocking=2), {"event": "base-advanced", "sha": "abc"}, run(3, blocking=2)]
    assert churn_reason(rounds_since_invalidation(history)) is None


def test_both_reasons_reported_together():
    d = evaluate(blocking_open=3, last_review=DIRTY, history=[DIRTY], budget=3)
    assert len(d.reasons) == 2
