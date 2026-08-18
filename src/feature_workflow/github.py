"""GitHub layer: issues, sub-issues, labels, PRs — via `gh` and GraphQL.

Sub-issues have no clean `gh` CLI surface yet, so parent/child linkage goes through GraphQL.
The `addSubIssue` mutation is still preview-ish; its behavior is pinned here.
"""

import json
import re

from .shell import run

LABELS = [
    ("feature", "0e8a16", "Feature tracking issue"),
    ("problem", "d93f0b", "Review-found problem"),
    ("sev:high", "b60205", "High severity problem (blocks the merge gate)"),
    ("sev:med", "fbca04", "Medium severity problem (blocks the merge gate)"),
    ("sev:low", "0e8a16", "Low severity problem (tracked debt, does not block)"),
    ("deferred", "c5def5", "Real problem, deliberately not fixed in this PR (reason on the issue)"),
    ("rejected", "cfd3d7", "Not a real problem — false positive or by design (reason on the issue)"),
]


def init_labels() -> None:
    """Create the workflow labels. --force makes it idempotent (updates if present)."""
    for name, color, desc in LABELS:
        run(
            [
                "gh",
                "label",
                "create",
                name,
                "--color",
                color,
                "--description",
                desc,
                "--force",
            ]
        )


def _node_id(number: int) -> str:
    """Resolve an issue number to its GraphQL node id (in the current repo)."""
    owner, name = _owner_repo()
    q = "query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n){id}}}"
    out = run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={q}",
            "-f",
            f"o={owner}",
            "-f",
            f"r={name}",
            "-F",
            f"n={number}",
            "--jq",
            ".data.repository.issue.id",
        ]
    )
    if not out:
        raise ValueError(f"Could not resolve node id for issue #{number}")
    return out


def _owner_repo() -> tuple[str, str]:
    out = run(["gh", "repo", "view", "--json", "owner,name", "--jq", "[.owner.login, .name] | @tsv"])
    owner, name = out.split("\t")
    return owner, name


def create_issue(title: str, body: str, labels: list[str]) -> int:
    args = ["gh", "issue", "create", "--title", title, "--body", body]
    for label in labels:
        args += ["--label", label]
    url = run(args)
    # `gh issue create` prints the issue URL; the trailing path segment is the number.
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def open_pr_for_branch(branch: str) -> int | None:
    """Return the number of the open PR whose head is `branch`, or None if there is none.

    Used by `adopt` so wiring a branch that already has a PR links it automatically instead
    of leaving `pr: null` until someone remembers to run `feature pr`. `gh pr list` returns
    an empty array (not an error) when nothing matches, so no result is a real "no PR" — not
    a swallowed failure — and we surface that as None.
    """
    out = run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number", "--jq", ".[0].number // empty"]
    )
    return int(out) if out else None


_FEATURE_REF_RE = re.compile(r"(?mi)^Part of #\d+\s*$")


def link_pr_to_feature(pr: int, issue: int) -> bool:
    """Add a native `Part of #<issue>` reference to the PR body, creating a real GitHub link
    that shows in the PR's sidebar and the issue's timeline. Returns True if the body changed.

    Non-closing on purpose: the feature (umbrella) issue outlives the PR — it's closed by
    `feature merge` once the gate is clean, not automatically when the PR merges. A `Closes`
    keyword would wrongly auto-close the tracking issue (and all its still-open problem
    sub-issues stay open). We strip any stale `Part of #…` line first so re-linking to a
    different issue doesn't leave two references behind.
    """
    body = run(["gh", "pr", "view", str(pr), "--json", "body", "--jq", ".body"])
    if re.search(rf"(?mi)^Part of #{issue}\s*$", body):
        return False  # already linked to this issue
    stripped = _FEATURE_REF_RE.sub("", body).rstrip()
    new_body = f"{stripped}\n\nPart of #{issue}\n" if stripped else f"Part of #{issue}\n"
    run(["gh", "pr", "edit", str(pr), "--body-file", "-"], input_text=new_body)
    return True


def pr_base_branch(number: int) -> str:
    """The base branch a PR targets (`baseRefName`). This defines the PR's review diff
    (`git diff <base>...HEAD`), so the review scope is only correct when it equals the
    feature's tracked parent branch."""
    out = run(["gh", "pr", "view", str(number), "--json", "baseRefName", "--jq", ".baseRefName"])
    if not out:
        raise ValueError(f"Could not resolve base branch for PR #{number}")
    return out


def pr_diff_stats(number: int) -> tuple[int, int]:
    """(changed_files, changed_lines) for a PR — the size half of review-budget sizing."""
    out = run(
        [
            "gh",
            "pr",
            "view",
            str(number),
            "--json",
            "changedFiles,additions,deletions",
            "--jq",
            "[.changedFiles, (.additions + .deletions)] | @tsv",
        ]
    )
    files, lines = out.split("\t")
    return int(files), int(lines)


def pr_changed_paths(number: int) -> list[str]:
    """Repo-relative paths the PR touches — the sensitivity half of review-budget sizing.

    Scoped by the PR's own base, like every other view of the diff in this workflow.
    """
    out = run(["gh", "pr", "diff", str(number), "--name-only"])
    return out.splitlines() if out else []


def add_label(number: int, label: str) -> None:
    """Add a label to an issue (used to stamp a problem's recorded disposition)."""
    run(["gh", "issue", "edit", str(number), "--add-label", label])


def remove_label(number: int, label: str) -> None:
    """Remove a label from an issue (used to revoke a disposition, so it blocks again)."""
    run(["gh", "issue", "edit", str(number), "--remove-label", label])


def get_issue_body(number: int) -> str:
    return run(["gh", "issue", "view", str(number), "--json", "body", "--jq", ".body"])


def set_issue_body(number: int, body: str) -> None:
    run(["gh", "issue", "edit", str(number), "--body-file", "-"], input_text=body)


def comment(number: int, text: str) -> None:
    run(["gh", "issue", "comment", str(number), "--body", text])


def close_issue(number: int, comment_text: str) -> None:
    run(["gh", "issue", "close", str(number), "--comment", comment_text])


def reopen_issue(number: int, comment_text: str) -> None:
    run(["gh", "issue", "reopen", str(number), "--comment", comment_text])


def link_sub_issue(parent: int, child: int) -> None:
    parent_id = _node_id(parent)
    child_id = _node_id(child)
    mutation = "mutation($p:ID!,$c:ID!){addSubIssue(input:{issueId:$p,subIssueId:$c}){issue{number}}}"
    run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={mutation}",
            "-f",
            f"p={parent_id}",
            "-f",
            f"c={child_id}",
        ]
    )


def sub_issues(parent: int) -> list[dict]:
    """Return [{number, title, state, labels:[...]}] for ALL of a feature's sub-issues.

    Fully paginated — `gh api graphql --paginate` follows the cursor as long as the query
    exposes `pageInfo{hasNextPage,endCursor}` and an `$endCursor` variable. No arbitrary cap:
    the gate open-count and dedup set must see every sub-issue, however many review runs filed.
    """
    owner, name = _owner_repo()
    q = (
        "query($o:String!,$r:String!,$n:Int!,$endCursor:String){repository(owner:$o,name:$r)"
        "{issue(number:$n){subIssues(first:100,after:$endCursor){"
        "pageInfo{hasNextPage,endCursor} nodes{number title state "
        "labels(first:50){nodes{name}}}}}}}"
    )
    # --paginate concatenates each page's JSON document; --jq runs per page, so we get one
    # JSON array of nodes per page. Parse each line and flatten.
    out = run(
        [
            "gh",
            "api",
            "graphql",
            "--paginate",
            "-f",
            f"query={q}",
            "-f",
            f"o={owner}",
            "-f",
            f"r={name}",
            "-F",
            f"n={parent}",
            "--jq",
            ".data.repository.issue.subIssues.nodes",
        ]
    )
    nodes: list[dict] = []
    for page in out.splitlines():
        page = page.strip()
        if page:
            nodes.extend(json.loads(page))
    return [
        {
            "number": n["number"],
            "title": n["title"],
            "state": n["state"],
            "labels": [label["name"] for label in n["labels"]["nodes"]],
        }
        for n in nodes
    ]
