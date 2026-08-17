"""Tests for the subprocess wrappers — a failure must say why it failed."""

import subprocess

import pytest

from feature_workflow.shell import CommandFailed, run, try_run


def test_run_returns_stripped_stdout():
    assert run(["echo", "  hi  "]) == "hi"


def test_failure_message_includes_stderr():
    with pytest.raises(CommandFailed) as exc:
        run(["sh", "-c", "echo 'boom: no server available' >&2; exit 1"])
    assert "boom: no server available" in str(exc.value)
    # Still a CalledProcessError, so existing handlers keep catching it.
    assert isinstance(exc.value, subprocess.CalledProcessError)
    assert exc.value.returncode == 1


def test_try_run_reports_exit_1_as_absent():
    assert try_run(["sh", "-c", "exit 1"]) is None


def test_try_run_still_raises_on_other_failures():
    with pytest.raises(CommandFailed):
        try_run(["sh", "-c", "echo bad >&2; exit 2"])
