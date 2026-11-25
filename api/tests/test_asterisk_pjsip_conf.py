"""deploy/asterisk/conf/pjsip.conf: every endpoint is identified by address alone.

Without `identify_by=ip` Asterisk takes an endpoint by the user in a From,
from anywhere the ACL lets through, without a password (#252 finding 2). A
global `endpoint_identifier_order` cannot prevent it: Asterisk appends every
identifier the order leaves out (measured on 22.12.0 with `pjsip show
identifiers`), so the line was dropped and this test keeps the real guard.
"""

import re
from pathlib import Path

PJSIP = Path(__file__).resolve().parents[2] / "deploy/asterisk/conf/pjsip.conf"


def endpoints_not_by_ip(text: str) -> list[str]:
    """Names of the endpoint sections whose `identify_by` is not `ip` alone."""
    sections: list[tuple[str, dict[str, str]]] = []
    for raw in text.splitlines():
        line = raw.split(";", 1)[0].strip()
        header = re.match(r"\[([^\]]+)\]", line)
        if header:
            sections.append((header.group(1), {}))
        elif "=" in line and sections:
            key, value = line.split("=", 1)
            sections[-1][1][key.strip()] = value.strip()
    return [
        name
        for name, options in sections
        if options.get("type") == "endpoint" and options.get("identify_by") != "ip"
    ]


def test_every_endpoint_of_the_product_is_identified_by_address_alone():
    text = PJSIP.read_text()
    assert "type=endpoint" in text
    assert endpoints_not_by_ip(text) == []


def test_an_endpoint_without_identify_by_is_named():
    text = "[access]\ntype=endpoint\ncontext=from-access\n[access]\ntype=aor\n"
    assert endpoints_not_by_ip(text) == ["access"]


def test_an_endpoint_that_also_takes_the_from_user_is_named():
    text = (
        "[a]\ntype=endpoint\nidentify_by=ip,username\n"
        "[b]\ntype=endpoint\nidentify_by = ip ; by address\n"
    )
    assert endpoints_not_by_ip(text) == ["a"]
