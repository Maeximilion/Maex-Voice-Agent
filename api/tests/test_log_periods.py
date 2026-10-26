"""Log periods by age on the server (T-11.7, docs/03 "Löschkonzept", legal check L8, U4).

Docker's `json-file` and `local` drivers limit a log by size and file count,
never by age, so production writes every container's output to the host's
journald, and `deploy/journald.conf` gives the journal its age limit. The other
stores that may hold call data have their own limits: n8n prunes executions
after 168 hours, Asterisk keeps no call records, and Caddy writes no access log.

Nothing is started here, as in test_deploy_exposure.py. journald's behaviour was
read in its source (journal-vacuum.c, 26.10.2026): a journal file is deleted as
a whole once its first entry is older than MaxRetentionSec, at the next rotation,
and MaxFileSec rotates at least daily, so no entry outlives the sum of the two.
The Asterisk settings were checked against the pinned image (`cdr show status`).
"""

import re

from api.tests.test_commands import REPO_ROOT
from api.tests.test_compose_ports import _load, _services

BASE = REPO_ROOT / "docker-compose.yml"
PROD = REPO_ROOT / "deploy" / "docker-compose.prod.yml"
JOURNALD = REPO_ROOT / "deploy" / "journald.conf"
CADDYFILE = REPO_ROOT / "deploy" / "Caddyfile"
ASTERISK = REPO_ROOT / "deploy" / "asterisk" / "conf"
# docs/03: container and proxy logs at most 14 days.
LOG_DAYS = 14
UNITS = {"s": 1, "min": 60, "h": 3600, "day": 86400, "week": 7 * 86400}


def _seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)\s*(s|min|h|day|week)", value.strip())
    assert match, f"no time span journald reads the same way: {value!r}"
    return int(match[1]) * UNITS[match[2]]


def _ini(text: str, section: str) -> dict[str, str]:
    """Key -> value of one section, `;` and `#` comments dropped."""
    values: dict[str, str] = {}
    current = ""
    for line in text.splitlines():
        line = re.split(r"[;#]", line, maxsplit=1)[0].strip()
        if line.startswith("["):
            current = line.strip("[]")
        elif "=" in line and current == section:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def test_every_production_service_logs_to_journald():
    base, prod = _services(_load(BASE)), _services(_load(PROD))
    missing = [
        name
        for name in base.keys() | prod.keys()
        if (prod.get(name) or {}).get("logging", {}).get("driver") != "journald"
    ]
    assert not missing, f"no age limit on the container log of {sorted(missing)}"


def test_the_journal_drops_entries_within_fourteen_days():
    journal = _ini(JOURNALD.read_text(), "Journal")
    retention = _seconds(journal["MaxRetentionSec"])
    rotation = _seconds(journal["MaxFileSec"])
    assert retention > 0 and rotation > 0
    assert retention + rotation <= LOG_DAYS * 86400


def test_n8n_prunes_executions_after_seven_days():
    environment = _services(_load(BASE))["n8n"]["environment"]
    assert environment["EXECUTIONS_DATA_PRUNE"] == "true"
    assert environment["EXECUTIONS_DATA_MAX_AGE"] == "168"


def test_asterisk_keeps_no_call_records():
    assert _ini((ASTERISK / "cdr.conf").read_text(), "general")["enable"] == "no"
    logger = _ini((ASTERISK / "logger.conf").read_text(), "general")
    assert logger["queue_log"] == "no"
    # Console only: no file in the container that outlives its log rotation.
    files = _ini((ASTERISK / "logger.conf").read_text(), "logfiles")
    assert list(files) == ["console"]


def test_caddy_writes_no_log_file():
    code = "\n".join(
        line.split("#", 1)[0] for line in CADDYFILE.read_text().splitlines()
    )
    assert not re.search(r"\boutput\s+file\b", code)
