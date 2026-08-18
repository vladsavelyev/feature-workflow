"""Tests for problem classification — what does and doesn't hold the merge gate."""

from feature_workflow.problems import disposition, is_blocking, severity, triage


def sub(number: int, *, sev: str = "sev:high", state: str = "OPEN", extra: list[str] | None = None) -> dict:
    return {
        "number": number,
        "title": f"problem {number}",
        "state": state,
        "labels": ["problem", sev, *(extra or [])],
    }


def test_high_and_med_block_low_does_not():
    assert is_blocking(sub(1, sev="sev:high"))
    assert is_blocking(sub(2, sev="sev:med"))
    assert not is_blocking(sub(3, sev="sev:low"))


def test_closed_problems_never_block():
    assert not is_blocking(sub(1, state="CLOSED"))


def test_disposition_removes_a_high_severity_problem_from_the_blocking_set():
    deferred = sub(1, sev="sev:high", extra=["deferred"])
    assert not is_blocking(deferred)
    assert disposition(deferred) == "deferred"
    # It keeps its real severity — deferring is not a downgrade.
    assert severity(deferred) == "sev:high"


def test_a_problem_with_no_severity_label_blocks():
    """Fail safe: only an explicit sev:low is non-blocking, so a missing label can't loosen the gate."""
    unlabelled = {"number": 1, "title": "t", "state": "OPEN", "labels": ["problem"]}
    assert severity(unlabelled) == "sev:?"
    assert is_blocking(unlabelled)
    assert [s["number"] for s in triage([unlabelled]).blocking] == [1]


def test_triage_splits_blocking_from_debt_and_drops_closed():
    subs = [
        sub(1, sev="sev:high"),
        sub(2, sev="sev:low"),
        sub(3, sev="sev:med", extra=["deferred"]),
        sub(4, sev="sev:med", state="CLOSED"),
    ]
    t = triage(subs)
    assert [s["number"] for s in t.blocking] == [1]
    assert [s["number"] for s in t.debt] == [2, 3]


def test_triage_summary_names_the_debt_that_ships():
    t = triage([sub(7, sev="sev:high", extra=["deferred"])])
    assert "#7" in t.summary()
    assert "deferred" in t.summary()


def test_triage_summary_when_there_is_no_debt():
    assert triage([sub(1, sev="sev:high")]).summary() == "no open non-blocking problems"
