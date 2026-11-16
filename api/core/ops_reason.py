"""The reason an operator gives before a stack hands out guest data (docs/23 §8).

Shared by the stack's ops API, which checks the two headers, and the operator
dashboard, which offers the list and sends them. Reads no settings, so the
dashboard can import it.

A reason is a category of a fixed list and a ticket or issue number, never
free text: a sentence would sooner or later hold a guest's name, and the reason
is copied into two audit logs.
"""

import re
from dataclasses import dataclass

REASON_HEADER = "X-Ops-Reason"
ACTOR_HEADER = "X-Ops-Actor"
# The customer the dashboard means. A stack compares it with its restaurant's
# key before it shows a secret or changes anything: an address registered for
# the wrong customer would otherwise change the wrong restaurant.
CUSTOMER_HEADER = "X-Ops-Customer"


@dataclass(frozen=True)
class OpsReader:
    """Who reads guest data or changes a stack over the ops API, and why
    (docs/23 §8)."""

    # `operator:<account>`, as the audit log names the operator.
    actor: str
    reason: str
    reference: str


# Category, and the words the dashboard shows for it.
REASONS = {
    "restaurant_report": "The restaurant reported a problem",
    "guest_complaint": "A guest complained about a call",
    "alarm": "An alarm or a failed call",
    "data_request": "A data subject request",
}
# A ticket or issue number: nothing a name fits into.
REFERENCE = re.compile(r"#[1-9][0-9]{0,8}")
# The name of an operator account (`dashboard/access.py` NAME).
ACTOR = re.compile(r"[a-z0-9._-]{2,40}")


def reason(category: str, reference: str) -> str | None:
    """The header's value for what the operator chose and typed (`812` or
    `#812`), or None when it is no reason."""
    number = "#" + reference.strip().removeprefix("#")
    if category not in REASONS or not REFERENCE.fullmatch(number):
        return None
    return f"{category} {number}"


def parse(value: str) -> tuple[str, str] | None:
    """Category and reference of a header's value, or None."""
    category, _, reference = value.partition(" ")
    if category not in REASONS or not REFERENCE.fullmatch(reference):
        return None
    return category, reference
