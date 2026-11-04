#!/usr/bin/env python3
"""Link every chain of open pull requests as a native GitHub stack.

/done step 7c (CLAUDE.md section 6 "Stacked PRs"): an open PR whose base is the
branch of another open PR forms a chain with it, whoever opened them. A chain
whose PRs are not all in one stack yet is linked with `gh stack link`, bottom
first. A PR based on `main` alone is no chain. Two PRs on the same branch make a
fork, which a stack cannot hold: it is reported, never linked.

    python3 scripts/link_stacks.py            # link what is missing
    python3 scripts/link_stacks.py --dry-run  # only say what it would link

Needs an authenticated `gh` with the gh-stack extension (docs/19), stdlib only.
Exit 1 when a call failed; what could be done is done and printed anyway.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass

QUERY = """query($owner:String!,$name:String!){repository(owner:$owner,name:$name){
pullRequests(states:OPEN,first:100){nodes{number baseRefName headRefName stack{number}}}}}"""


@dataclass(frozen=True)
class PR:
    number: int
    base: str
    head: str
    stack: int | None


def plan(prs: list[PR]) -> tuple[list[list[int]], list[str]]:
    """The chains to link, bottom first, and the forks that cannot be linked."""
    by_head = {pr.head: pr for pr in prs}
    children: dict[str, list[PR]] = {}
    for pr in prs:
        if pr.base in by_head:
            children.setdefault(pr.base, []).append(pr)
    chains, forks = [], []
    for bottom in prs:
        if bottom.base in by_head or bottom.head not in children:
            continue  # not the bottom of a chain
        chain, node = [bottom], bottom
        while node.head in children:
            above = children[node.head]
            if len(above) > 1:
                numbers = ", ".join(f"#{pr.number}" for pr in above)
                forks.append(f"#{node.number} has {numbers} on top: not linked")
                break
            node = above[0]
            chain.append(node)
        stacks = {pr.stack for pr in chain}
        if len(chain) > 1 and (None in stacks or len(stacks) > 1):
            chains.append([pr.number for pr in chain])
    return chains, forks


def open_prs() -> list[PR]:
    repo = subprocess.run(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "owner,name",
            "--jq",
            '.owner.login+" "+.name',
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    out = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={QUERY}",
            "-F",
            f"owner={repo[0]}",
            "-F",
            f"name={repo[1]}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    nodes = json.loads(out)["data"]["repository"]["pullRequests"]["nodes"]
    return [
        PR(
            n["number"],
            n["baseRefName"],
            n["headRefName"],
            (n["stack"] or {}).get("number"),
        )
        for n in nodes
    ]


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    try:
        chains, forks = plan(open_prs())
    except (subprocess.CalledProcessError, KeyError, ValueError) as exc:
        print(f"could not read the open pull requests: {exc}", file=sys.stderr)
        return 1
    failed = False
    for chain in chains:
        line = " ".join(str(n) for n in chain)
        if dry:
            print(f"would link: {line}")
            continue
        done = subprocess.run(
            ["gh", "stack", "link", *line.split()], capture_output=True, text=True
        )
        print(
            f"linked: {line}"
            if done.returncode == 0
            else f"failed: {line}: {done.stderr.strip()}"
        )
        failed |= done.returncode != 0
    for fork in forks:
        print(fork)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
