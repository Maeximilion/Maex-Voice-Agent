"""`n8n/team_events.json`: the export handles what the dispatcher sends.

The workflow lives in n8n and is edited there; the repo only holds the export.
Nothing ties it to `api/events/types.py` except this test: a new event type
that reaches n8n without a branch would be answered with 422 and end as
`failed` in the outbox, noticed only in production.

Static checks, no n8n needed. What n8n does with the export (import, dedupe,
answers) was run by hand against a real instance, see `n8n/README.md`.
"""

import ast
import json
import re
from functools import cache
from typing import get_args
from urllib.parse import urlparse

from api.config import Settings
from api.events import types
from api.gui.router import CALLBACK_LABELS
from api.schemas.callbacks import CallbackReason
from api.tests.test_commands import REPO_ROOT

WORKFLOW = REPO_ROOT / "n8n" / "team_events.json"
# Where code that can reach the outbox lives.
PRODUCER_DIRS = ("api", "scripts", "sim")

# Listed in `types.ALL`, but no code enqueues it yet, so there is no payload to
# build a message from. The moment a producer appears, the test below fails
# until the workflow has a branch for it.
WITHOUT_PRODUCER = {types.DAILY_REPORT}

PLACEHOLDER_PREFIX = "REPLACE ME"
SECRET_NAME_RE = re.compile(
    r"pass|secret|token|api[-_ ]?key|authorization", re.IGNORECASE
)
# A secret in a URL: user:password@host, or a query parameter that names one.
SECRET_URL_RE = re.compile(
    r"://[^/\s:@]+:[^/\s@]+@|[?&][\w-]*(?:auth|token|key|secret|pass)[\w-]*=",
    re.IGNORECASE,
)
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


def _assignment(node: dict, field: str) -> str:
    (value,) = (
        a["value"]
        for a in node["parameters"]["assignments"]["assignments"]
        if a["name"] == field
    )
    return value


def _produced() -> set[str]:
    """Event types some code outside the tests enqueues.

    Read from the syntax tree, not by text search: `event_type=types.X` counts
    like `event_type=X`, and an event type handed over in a variable fails here
    instead of slipping past the guard.
    """
    produced = set()
    for directory in PRODUCER_DIRS:
        for path in (REPO_ROOT / directory).rglob("*.py"):
            if "tests" in path.relative_to(REPO_ROOT).parts:
                continue
            for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name != "enqueue":
                    continue
                (value,) = (k.value for k in call.keywords if k.arg == "event_type")
                constant = getattr(value, "id", None) or getattr(value, "attr", None)
                where = f"{path.relative_to(REPO_ROOT)}:{call.lineno}"
                assert constant and constant.isupper() and hasattr(types, constant), (
                    f"{where}: pass the event type as a constant from api.events.types"
                )
                produced.add(getattr(types, constant))
    return produced


def _strings(value: object, key: str = ""):
    """Every string in a node's parameters with the key it stands under."""
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _strings(child, child_key)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child, key)
    elif isinstance(value, str):
        yield key, value


def _keys(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _keys(child)


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


def test_no_run_is_kept_in_the_execution_history() -> None:
    # A saved run holds the whole event: guest name, phone number, note.
    settings = _workflow()["settings"]
    assert settings["saveDataSuccessExecution"] == "none"
    assert settings["saveDataErrorExecution"] == "none"
    assert settings["saveManualExecutions"] is False
    assert settings["saveExecutionProgress"] is False


def test_times_are_shown_in_the_tenant_timezone() -> None:
    # Pinned in the workflow: without it the time in a message follows the
    # environment of the n8n instance, whose default is not our timezone.
    expected = Settings.model_fields["tenant_timezone"].default
    assert _workflow()["settings"]["timezone"] == expected


def test_one_branch_for_exactly_the_event_types_that_reach_n8n() -> None:
    expected = set(types.ALL) - set(types.KITCHEN) - WITHOUT_PRODUCER
    assert set(_branches()) == expected


def test_a_type_without_a_branch_has_no_producer() -> None:
    produced = _produced()
    # The scan sees the producers that exist; otherwise it proves nothing.
    assert {types.RESERVATION_CONFIRMED, types.CALLBACK_CREATED} <= produced
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


def test_channel_node_stops_the_run_when_it_fails() -> None:
    channel = _channel()
    if channel["name"].startswith(PLACEHOLDER_PREFIX):
        # Until the real channel is built, no event may count as delivered.
        assert channel["type"] == "n8n-nodes-base.stopAndError"
    # A channel that does nothing or carries on after an error lets the run
    # reach "delivered" although nobody was notified.
    assert channel["type"] != "n8n-nodes-base.noOp"
    assert not channel.get("continueOnFail", False)
    assert channel.get("onError", "stopWorkflow") == "stopWorkflow"
    assert not channel.get("disabled", False)


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


def test_callback_labels_are_the_ones_on_the_tablet() -> None:
    text = _assignment(_nodes()[_branches()[types.CALLBACK_CREATED]], "text")
    assert set(get_args(CallbackReason)) == set(CALLBACK_LABELS)
    for reason, (_tone, label) in CALLBACK_LABELS.items():
        assert f"{reason}: '{label}'" in text, reason


def test_no_secrets_or_personal_data_in_the_export() -> None:
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert not PHONE_RE.search(raw)
    assert not EMAIL_RE.search(raw)
    for node in _workflow()["nodes"]:
        params = node["parameters"]
        for key in _keys(params):
            assert not SECRET_NAME_RE.search(key), (node["name"], key)
        for key, value in _strings(params):
            assert not SECRET_URL_RE.search(value), (node["name"], key)
            # n8n stores a header as {"name": "Authorization", "value": ...}.
            if key == "name":
                assert not SECRET_NAME_RE.search(value), (node["name"], value)
        # A credential reference is fine, whatever its name: the values are
        # not part of an export.
        for reference in node.get("credentials", {}).values():
            assert set(reference) <= {"id", "name"}
