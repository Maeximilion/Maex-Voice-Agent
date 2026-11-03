"""Tests for scripts/link_stacks.py: which chains of open PRs get linked as a stack."""

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "link_stacks", Path(__file__).resolve().parents[2] / "scripts" / "link_stacks.py"
)
assert _SPEC is not None and _SPEC.loader is not None
link_stacks = importlib.util.module_from_spec(_SPEC)
# A dataclass looks its module up while it is built.
sys.modules["link_stacks"] = link_stacks
_SPEC.loader.exec_module(link_stacks)
PR = link_stacks.PR


def test_a_chain_without_a_stack_is_linked_bottom_first():
    """#536, #532 and #537 on 03.11.2026: chained by base, never linked."""
    prs = [
        PR(537, "fix/526", "fix/525", None),
        PR(536, "main", "fix/523", None),
        PR(532, "fix/523", "fix/526", None),
    ]

    assert link_stacks.plan(prs) == ([[536, 532, 537]], [])


def test_a_chain_already_in_one_stack_is_left_alone():
    prs = [PR(535, "main", "task/a", 539), PR(538, "task/a", "task/b", 539)]

    assert link_stacks.plan(prs) == ([], [])


def test_a_new_pr_on_top_of_a_stack_is_linked():
    prs = [PR(535, "main", "task/a", 539), PR(538, "task/a", "task/b", None)]

    assert link_stacks.plan(prs) == ([[535, 538]], [])


def test_a_pr_on_main_alone_is_no_chain():
    prs = [PR(510, "main", "fix/x", None), PR(512, "main", "fix/y", None)]

    assert link_stacks.plan(prs) == ([], [])


def test_a_fork_is_reported_and_not_linked():
    prs = [
        PR(1, "main", "a", None),
        PR(2, "a", "b", None),
        PR(3, "a", "c", None),
    ]

    chains, forks = link_stacks.plan(prs)

    assert chains == []
    assert forks == ["#1 has #2, #3 on top: not linked"]


def test_a_chain_on_a_branch_without_a_pr_starts_at_its_lowest_pr():
    """The handover branch has no PR of its own; what sits on it still chains."""
    prs = [PR(10, "docs/handover", "task/x", None), PR(11, "task/x", "task/y", None)]

    assert link_stacks.plan(prs) == ([[10, 11]], [])
