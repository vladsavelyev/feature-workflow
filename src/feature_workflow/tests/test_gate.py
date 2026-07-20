"""Tests for the merge-gate decision logic."""

from feature_workflow.gate import evaluate

CLEAN_REVIEW = {"run": 2, "sha": "abc", "new_problems": 0, "summary": "clean"}
DIRTY_REVIEW = {"run": 1, "sha": "abc", "new_problems": 2, "summary": "found 2"}


def test_open_when_no_problems_and_clean_review():
    d = evaluate(open_problem_count=0, last_review=CLEAN_REVIEW)
    assert d.open
    assert d.reasons == []


def test_closed_when_problems_open():
    d = evaluate(open_problem_count=1, last_review=CLEAN_REVIEW)
    assert not d.open
    assert "1 open problem(s)" in d.reasons


def test_closed_when_last_review_found_new():
    d = evaluate(open_problem_count=0, last_review=DIRTY_REVIEW)
    assert not d.open
    assert "last review found 2 new problem(s)" in d.reasons


def test_closed_when_no_review_recorded():
    d = evaluate(open_problem_count=0, last_review=None)
    assert not d.open
    assert "no review run recorded" in d.reasons


def test_both_reasons_reported_together():
    d = evaluate(open_problem_count=3, last_review=DIRTY_REVIEW)
    assert not d.open
    assert len(d.reasons) == 2
