"""Tests for review-budget sizing."""

from feature_workflow.budget import BASE_ROUNDS, MAX_ROUNDS, auto_rounds, sensitive_matches


def test_small_diff_gets_the_base_budget():
    rounds, why = auto_rounds(changed_files=3, changed_lines=80, sensitive_hits=[])
    assert rounds == BASE_ROUNDS
    assert why == [f"base {BASE_ROUNDS} round(s)"]


def test_large_diff_earns_a_round():
    by_lines, why = auto_rounds(changed_files=3, changed_lines=900, sensitive_hits=[])
    by_files, _ = auto_rounds(changed_files=40, changed_lines=80, sensitive_hits=[])
    assert by_lines == by_files == BASE_ROUNDS + 1
    assert any("large diff" in line for line in why)


def test_sensitive_paths_earn_a_round():
    rounds, why = auto_rounds(changed_files=2, changed_lines=30, sensitive_hits=["src/auth/token.py"])
    assert rounds == BASE_ROUNDS + 1
    assert any("sensitive" in line for line in why)


def test_every_bump_together_stays_within_the_cap():
    rounds, _ = auto_rounds(changed_files=200, changed_lines=9000, sensitive_hits=["src/auth/token.py"])
    assert BASE_ROUNDS < rounds <= MAX_ROUNDS


def test_sensitive_matches_crosses_directory_levels():
    paths = ["src/auth/oauth/token.py", "src/ui/button.tsx", "migrations/003_add_col.sql"]
    hits = sensitive_matches(paths, ["src/auth/*", "migrations/*"])
    assert hits == ["src/auth/oauth/token.py", "migrations/003_add_col.sql"]


def test_no_configured_patterns_means_no_hits():
    assert sensitive_matches(["src/auth/token.py"], []) == []
