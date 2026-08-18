"""Tests for the merge-gate decision logic — the tri-state verdict, coverage, and churn signals."""

from feature_workflow.gate import EXIT_CODES, Verdict, churn_reason, covers_head, evaluate, rounds_since_invalidation

HEAD = "head99"


def run(n: int, *, blocking: int = 0, low: int = 0, regressions: int = 0, sha: str = HEAD) -> dict:
    return {
        "run": n,
        "sha": sha,
        "new_blocking": blocking,
        "new_low": low,
        "regressions": regressions,
        "summary": f"run {n}",
    }


def gate(*, blocking_open, last_review, history, budget=3, head_sha=HEAD, pinned=False):
    return evaluate(
        blocking_open=blocking_open,
        last_review=last_review,
        history=history,
        budget=budget,
        head_sha=head_sha,
        budget_is_explicit=pinned,
    )


CLEAN = run(2)
DIRTY = run(1, blocking=2)


def test_open_when_nothing_blocking_and_the_review_covers_head():
    d = gate(blocking_open=0, last_review=CLEAN, history=[DIRTY, CLEAN])
    assert d.verdict is Verdict.OPEN
    assert d.open
    assert d.exit_code == 0
    assert d.reasons == []


def test_low_severity_findings_do_not_hold_the_gate():
    """The whole point: a run that found only nits is a clean pass."""
    nits_only = run(2, low=4)
    d = gate(blocking_open=0, last_review=nits_only, history=[nits_only])
    assert d.verdict is Verdict.OPEN


def test_deferring_the_findings_opens_the_gate_without_another_round():
    """The escape hatch has to actually work: nothing is blocking, and no code changed.

    The gate used to read `last_review["new_blocking"] != 0`, a count that went stale the moment a
    problem was disposed of — so "ship the rest as debt" left the gate shut with no way to reopen it.
    """
    found_two = run(2, blocking=2)
    d = gate(blocking_open=0, last_review=found_two, history=[run(1, blocking=2), found_two], budget=2)
    assert d.verdict is Verdict.OPEN


def test_commits_after_a_clean_review_close_the_gate_again():
    """Coverage, not counts: code nobody reviewed must not ship behind an older green light."""
    d = gate(blocking_open=0, last_review=CLEAN, history=[CLEAN], head_sha="newsha")
    assert not d.open
    assert any("never been reviewed" in r for r in d.reasons)
    assert d.verdict is Verdict.REVIEW_AGAIN


def test_covers_head_matches_short_against_long_shas():
    assert covers_head({"sha": "abc1234"}, "abc1234def5678")
    assert covers_head({"sha": "abc1234def5678"}, "abc1234")
    assert not covers_head({"sha": "abc1234"}, "def5678")
    assert not covers_head(None, "abc1234")


def test_review_again_while_budget_remains():
    d = gate(blocking_open=2, last_review=DIRTY, history=[DIRTY])
    assert d.verdict is Verdict.REVIEW_AGAIN
    assert "2 blocking problem(s) open" in d.reasons
    assert d.rounds_used == 1


def test_needs_decision_when_budget_spent():
    history = [run(1, blocking=3), run(2, blocking=1)]
    d = gate(blocking_open=1, last_review=history[-1], history=history, budget=2)
    assert d.verdict is Verdict.NEEDS_DECISION
    assert any("budget spent (2/2)" in r for r in d.reasons)


def test_needs_decision_on_churn_before_budget_is_spent():
    """Findings rising → escalate early rather than burn the remaining rounds."""
    history = [run(1, blocking=2), run(2, blocking=3)]
    d = gate(blocking_open=3, last_review=history[-1], history=history, budget=4)
    assert d.verdict is Verdict.NEEDS_DECISION
    assert any("rising" in r for r in d.reasons)


def test_buying_a_round_overrides_the_churn_escalation():
    """Churn escalates EARLY; a human who saw that and chose to spend more has answered it.

    Without this, `feature budget --set` — one of the two remedies the gate itself prints — was a
    silent no-op, because churn was re-derived from the same unchanged history and checked first.
    """
    history = [run(1, blocking=2), run(2, blocking=3)]
    stuck = gate(blocking_open=3, last_review=history[-1], history=history, budget=4)
    assert stuck.verdict is Verdict.NEEDS_DECISION
    bought = gate(blocking_open=3, last_review=history[-1], history=history, budget=4, pinned=True)
    assert bought.verdict is Verdict.REVIEW_AGAIN


def test_a_pinned_budget_that_is_spent_still_needs_a_decision():
    history = [run(1, blocking=2), run(2, blocking=3)]
    d = gate(blocking_open=3, last_review=history[-1], history=history, budget=2, pinned=True)
    assert d.verdict is Verdict.NEEDS_DECISION


def test_regression_is_immediate_churn():
    history = [run(1, blocking=2), run(2, blocking=1, regressions=1)]
    d = gate(blocking_open=1, last_review=history[-1], history=history, budget=4)
    assert d.verdict is Verdict.NEEDS_DECISION
    assert any("re-opened 1 previously-closed" in r for r in d.reasons)


def test_one_flat_round_is_not_churn():
    """1-then-1 is the commonest shape of a converging review; firing on it wasted the budget."""
    history = [run(1, blocking=1), run(2, blocking=1)]
    assert churn_reason(history) is None
    d = gate(blocking_open=1, last_review=history[-1], history=history, budget=4)
    assert d.verdict is Verdict.REVIEW_AGAIN


def test_a_clean_round_followed_by_a_dirty_one_is_not_churn():
    history = [run(1, blocking=0), run(2, blocking=1)]
    assert churn_reason(history) is None


def test_exit_codes_never_collide_with_command_failure_codes():
    """1 and 2 already mean "the command failed" (sys.exit(msg), argparse); verdicts must not."""
    codes = {v: EXIT_CODES[v] for v in Verdict}
    assert codes[Verdict.OPEN] == 0
    assert set(codes.values()).isdisjoint({1, 2})
    assert len(set(codes.values())) == len(codes)


def test_a_placeholder_round_spends_budget_but_not_the_trend():
    """Migrated features carry counted-but-detail-free rounds; the trend must ignore them."""
    placeholder = {"run": 1, "placeholder": True, "summary": "(pre-upgrade)"}
    history = [placeholder, run(2, blocking=3)]
    assert churn_reason(history) is None
    d = gate(blocking_open=3, last_review=history[-1], history=history, budget=2)
    # Two rounds are spent (the placeholder counts), so the budget check — not churn — escalates.
    assert d.rounds_used == 2
    assert d.verdict is Verdict.NEEDS_DECISION
    assert any("budget spent (2/2)" in r for r in d.reasons)


def test_converging_run_still_reviews_again():
    history = [run(1, blocking=5), run(2, blocking=2)]
    d = gate(blocking_open=2, last_review=history[-1], history=history, budget=4)
    assert d.verdict is Verdict.REVIEW_AGAIN


def test_closed_when_no_review_recorded():
    d = gate(blocking_open=0, last_review=None, history=[], budget=2)
    assert not d.open
    assert "no valid review run recorded" in d.reasons
    assert d.verdict is Verdict.REVIEW_AGAIN


def test_no_review_and_no_budget_left_needs_decision():
    """A sync-invalidated review with an exhausted budget is a human call, not a silent block."""
    history = [run(1, blocking=1), run(2, blocking=1)]
    d = gate(blocking_open=0, last_review=None, history=history, budget=2)
    assert d.verdict is Verdict.NEEDS_DECISION


def test_rounds_reset_after_a_base_change():
    """`feature sync` marks the history; rounds before the marker don't spend the new budget."""
    history = [run(1, blocking=2), run(2, blocking=2), {"event": "base-advanced", "sha": "abc"}]
    assert rounds_since_invalidation(history) == []
    d = gate(blocking_open=0, last_review=None, history=history, budget=2)
    assert d.verdict is Verdict.REVIEW_AGAIN
    assert d.rounds_used == 0


def test_churn_trend_ignores_rounds_before_a_base_change():
    history = [run(1, blocking=2), run(2, blocking=3), {"event": "base-advanced", "sha": "abc"}, run(3, blocking=3)]
    assert churn_reason(rounds_since_invalidation(history)) is None


def test_both_reasons_reported_together():
    d = gate(blocking_open=3, last_review=DIRTY, history=[DIRTY], head_sha="newsha")
    assert len(d.reasons) == 2
