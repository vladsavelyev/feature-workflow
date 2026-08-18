"""Which problem sub-issues hold the merge gate — pure classification over sub-issue dicts.

Two ideas do the work:

**Severity decides whether a problem blocks.** A reviewer always finds low-severity material
(nits, naming, duplication, hypotheticals), so a gate that waits for "no findings at all" never
opens. `sev:high` and `sev:med` block the merge; `sev:low` is tracked debt that ships.

**A disposition takes a problem out of the blocking set, on the record.** `deferred` (real, but
not being fixed in this PR) and `rejected` (not a real problem) both require a reason that is
posted on the issue, so shipping known debt is an explicit, auditable act — not a silent
severity downgrade. Everything with a disposition is reported at merge time.
"""

from dataclasses import dataclass

# Only an EXPLICIT `sev:low` is non-blocking. Stated this way round on purpose: severity is the
# value the gate now depends on, so an unlabelled problem — a human filing a sub-issue through the
# GitHub UI without picking a label, or a label removed later — must fail *safe* and hold the merge.
# Enumerating the blocking severities instead would silently let `sev:?` through.
NON_BLOCKING_SEVS = ("sev:low",)
UNKNOWN_SEV = "sev:?"

DEFERRED = "deferred"
REJECTED = "rejected"
DISPOSITIONS = (DEFERRED, REJECTED)


def severity(sub: dict) -> str:
    """The `sev:*` label on a problem, or `sev:?` if it has none (which blocks — see above)."""
    return next((label for label in sub["labels"] if label.startswith("sev:")), UNKNOWN_SEV)


def disposition(sub: dict) -> str | None:
    """The recorded disposition label (`deferred`/`rejected`), or None if undisposed."""
    return next((label for label in sub["labels"] if label in DISPOSITIONS), None)


def is_blocking(sub: dict) -> bool:
    """True if this sub-issue holds the merge gate."""
    return sub["state"] == "OPEN" and severity(sub) not in NON_BLOCKING_SEVS and disposition(sub) is None


@dataclass(frozen=True)
class Triage:
    """A feature's open sub-issues split the way every caller needs them."""

    blocking: list[dict]  # open, not explicitly low, no disposition — these hold the gate
    debt: list[dict]  # open, non-blocking (low severity, or disposed) — ships with the PR

    def summary(self) -> str:
        """One line naming the debt that ships, so it is never invisible at merge time."""
        if not self.debt:
            return "no open non-blocking problems"
        parts = [f"#{s['number']} [{severity(s)}{'/' + (disposition(s) or 'open')}]" for s in self.debt]
        return f"{len(self.debt)} non-blocking problem(s) ship as debt: " + ", ".join(parts)


def triage(subs: list[dict]) -> Triage:
    open_subs = [s for s in subs if s["state"] == "OPEN"]
    return Triage(
        blocking=[s for s in open_subs if is_blocking(s)],
        debt=[s for s in open_subs if not is_blocking(s)],
    )
