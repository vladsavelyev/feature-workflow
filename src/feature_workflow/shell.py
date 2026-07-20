"""Thin subprocess helpers. Abort-fast: a failing command raises, never returns empty."""

import subprocess


def run(cmd: list[str], *, cwd: str | None = None, input_text: str | None = None, timeout: float | None = None) -> str:
    """Run a command, return stdout stripped. Raises CalledProcessError on non-zero exit.

    We deliberately do NOT swallow errors: a missing branch, an unauthenticated gh, or a
    failed GraphQL call must crash loudly, not produce a misleading empty result. A `timeout`
    (seconds) guards against a hung child (e.g. `claude -p` stalling on a flaky API) — on
    expiry subprocess raises TimeoutExpired, which propagates rather than hanging forever.
    """
    result = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def try_run(cmd: list[str], *, cwd: str | None = None) -> str | None:
    """Run a command that is *expected* to sometimes have no result (e.g. a git-config key
    that isn't set yet). Returns None only on exit code 1 (git config's "not found").

    Any other non-zero exit still raises — we only tolerate the one documented "absent" case.
    """
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1:
        return None
    raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
