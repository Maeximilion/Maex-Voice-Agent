"""`n8n/team_events.json`: the export handles what the dispatcher sends.

The workflow lives in n8n and is edited there; the repo only holds the export.
Nothing ties it to `api/events/types.py` except this test: a new event type
that reaches n8n without a branch would be answered with 422 and end as
`failed` in the outbox, noticed only in production.

Static checks, no n8n needed. What n8n does with the export (import, dedupe,
answers) was run by hand against a real instance, see `n8n/README.md`.
"""

import json
import re
from functools import cache
from typing import get_args
from urllib.parse import urlparse

from api.config import Settings
from api.events import types
from api.schemas.callbacks import CallbackReason
from api.tests.test_commands import REPO_ROOT

WORKFLOW = REPO_ROOT / "n8n" / "team_events.json"
API_DIR = REPO_ROOT / "api"

# Listed in `types.ALL`, but no code enqueues it yet, so there is no payload to
# build a message from. The moment a producer appears, the test below fails
# until the workflow has a branch for it.
WITHOUT_PRODUCER = {types.DAILY_REPORT}

PRODUCER_RE = re.compile(r"event_type=([A-Z_]+)")
PLACEHOLDER_PREFIX = "REPLACE ME"
SECRET_KEY_RE = re.compile(r"pass|secret|token|api_?key|authorization", re.IGNORECASE)
PHONE_RE = re.compile(r"\+\d{7,}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.\w+")


@cache
def _workflow() -> dict:
    return json.loads(WORKFLOW.read_text(encoding="utf-8"))


def _nodes() -> dict[str, dict]:
    return {node["name"]: node for node in _workflow()["nodes"]}


def _of_type(suffix: str) -> list[dict]:
    return [n for n in _workflow()["nodes"] if n["type"] == f"n8n-nodes-base.{suffix}"]


def _targets(name: str) -> list[list[str]]:
    """Per output of a node: the names of the nodes it leads to."""
    outputs = _workflow()["connections"].get(name, {}).get("main", [])
    return [[link["node"] for link in output or []] for output in outputs]


def _switch() -> dict:
    (switch,) = _of_type("switch")
    return switch


def _branches() -> dict[str, str]:
    """Event type of each switch rule to the node its output leads to."""
    rules = _switch()["parameters"]["rules"]["values"]
    outputs = _targets(_switch()["name"])
    branches = {}
    for rule, targets in zip(rules, outputs, strict=False):
        (condition,) = rule["conditions"]["conditions"]
        assert condition["leftValue"] == "={{ $json.event_type }}"
        assert condition["operator"]["operation"] == "equals"
        (target,) = targets
        assert condition["rightValue"] not in branches, "one rule per event type"
        branches[condition["rightValue"]] = target
    assert len(branches) == len(rules)
    return branches


def _channel() -> dict:
    """The one node every message runs into."""
    successors = {tuple(_targets(node)[0]) for node in _branches().values()}
    assert len(successors) == 1, "every branch leads into the same channel node"
    ((name,),) = successors
    return _nodes()[name]


def _produced() -> set[str]:
    """Event types some code outside the tests enqueues."""
    names = set()
    for path in API_DIR.rglob("*.py"):
        if "tests" in path.parts:
            continue
        names.update(PRODUCER_RE.findall(path.read_text(encoding="utf-8")))
    return {getattr(types, name) for name in names}


def _walk(value: object, key: str = ""):
    """Every (key, string) pair in the export, however deep."""
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield child_key, child_key
            yield from _walk(child, child_key)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child, key)
    elif isinstance(value, str):
        yield key, value


def test_export_is_a_workflow_n8n_can_import() -> None:
    workflow = _workflow()
    assert workflow["name"]
    names = [node["name"] for node in workflow["nodes"]]
    assert len(names) == len(set(names)), "n8n connects nodes by name"
    for source, outputs in workflow["connections"].items():
        assert source in names
        for output in outputs["main"]:
            for link in output or []:
                assert link["node"] in names
    # Imported inactive: the two manual steps from the README come first.
    assert workflow["active"] is False


def test_webhook_matches_what_the_dispatcher_posts_to() -> None:
    (webhook,) = _of_type("webhook")
    params = webhook["parameters"]
    default_url = Settings.model_fields["n8n_webhook_url"].default
    assert urlparse(default_url).path == f"/webhook/{params['path']}"
    assert params["httpMethod"] == "POST"
    # The dispatcher sends basic auth; without it anyone who reaches n8n could
    # push a fake alarm to the team.
    assert params["authentication"] == "basicAuth"
    # The answer comes from a Respond node, so a failed run is a 5xx and the
    # dispatcher retries instead of counting the event as sent.
    assert params["responseMode"] == "responseNode"


def test_one_branch_for_exactly_the_event_types_that_reach_n8n() -> None:
    expected = set(types.ALL) - set(types.KITCHEN) - WITHOUT_PRODUCER
    assert set(_branches()) == expected


def test_a_type_without_a_branch_has_no_producer() -> None:
    produced = _produced()
    # The scan sees the producers that exist; otherwise it proves nothing.
    assert {types.RESERVATION_CONFIRMED, types.CALLBACK_CREATED} <= produced
    assert produced <= set(types.ALL)
    assert not WITHOUT_PRODUCER & produced
    assert produced - set(types.KITCHEN) == set(_branches())


def test_unknown_event_type_is_refused_not_swallowed() -> None:
    switch = _switch()
    assert switch["parameters"]["options"]["fallbackOutput"] == "extra"
    (fallback,) = _targets(switch["name"])[len(_branches())]
    node = _nodes()[fallback]
    assert node["type"] == "n8n-nodes-base.respondToWebhook"
    # 4xx makes the dispatcher retry and end in `failed` with its alarm.
    assert node["parameters"]["options"]["responseCode"] >= 400


def test_every_branch_builds_a_message_for_the_channel() -> None:
    for event_type, name in _branches().items():
        node = _nodes()[name]
        assert node["type"] == "n8n-nodes-base.set", event_type
        fields = {a["name"] for a in node["parameters"]["assignments"]["assignments"]}
        assert fields == {"title", "text", "priority"}, event_type
        # Nothing but the message goes on: the payload holds names, phone
        # numbers and call text, and a channel node may forward its whole input.
        assert not node["parameters"].get("includeOtherFields", False), event_type


def test_placeholder_channel_fails_closed() -> None:
    channel = _channel()
    if channel["name"].startswith(PLACEHOLDER_PREFIX):
        # Until a real channel is chosen, no event may count as delivered.
        assert channel["type"] == "n8n-nodes-base.stopAndError"


def test_event_id_is_remembered_only_after_the_channel() -> None:
    nodes = _nodes()
    writers = [
        n["name"]
        for n in _of_type("code")
        if re.search(r"\.delivered\s*=", n["parameters"]["jsCode"])
    ]
    # Marked before the channel, a failed notification would make the retry a
    # duplicate and the team would never hear of the event.
    assert writers == _targets(_channel()["name"])[0]
    (after_webhook,) = _targets(_of_type("webhook")[0]["name"])[0]
    check = nodes[after_webhook]["parameters"]["jsCode"]
    assert "x-idempotency-key" in check
    assert "$getWorkflowStaticData" in check
    (answer,) = _targets(writers[0])[0]
    assert nodes[answer]["type"] == "n8n-nodes-base.respondToWebhook"
    assert nodes[answer]["parameters"]["options"]["responseCode"] == 200


def test_every_callback_reason_has_a_label() -> None:
    node = _nodes()[_branches()[types.CALLBACK_CREATED]]
    text = json.dumps(node["parameters"], ensure_ascii=False)
    for reason in get_args(CallbackReason):
        assert reason in text, reason


def test_no_secrets_or_personal_data_in_the_export() -> None:
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert not PHONE_RE.search(raw)
    assert not EMAIL_RE.search(raw)
    for key, value in _walk(_workflow()):
        assert not SECRET_KEY_RE.search(key), key
        # n8n stores a header as {"name": "Authorization", "value": ...}.
        if key == "name":
            assert not SECRET_KEY_RE.search(value), value
    # A credential reference (id and name) is fine, its values are not part of
    # an export. The first version carries none: they are assigned in n8n.
    for node in _workflow()["nodes"]:
        for reference in node.get("credentials", {}).values():
            assert set(reference) <= {"id", "name"}
