"""GitHub layer: issues, sub-issues, labels, PRs — via `gh` and GraphQL.

Sub-issues have no clean `gh` CLI surface yet, so parent/child linkage goes through GraphQL.
The `addSubIssue` mutation is still preview-ish; its behavior is pinned here.
"""

import json

from .shell import run

LABELS = [
    ("feature", "0e8a16", "Feature tracking issue"),
    ("problem", "d93f0b", "Review-found problem"),
    ("sev:high", "b60205", "High severity problem"),
    ("sev:med", "fbca04", "Medium severity problem"),
    ("sev:low", "0e8a16", "Low severity problem"),
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


def pr_base_branch(number: int) -> str:
    """The base branch a PR targets (`baseRefName`). This defines the PR's review diff
    (`git diff <base>...HEAD`), so the review scope is only correct when it equals the
    feature's tracked parent branch."""
    out = run(["gh", "pr", "view", str(number), "--json", "baseRefName", "--jq", ".baseRefName"])
    if not out:
        raise ValueError(f"Could not resolve base branch for PR #{number}")
    return out


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
