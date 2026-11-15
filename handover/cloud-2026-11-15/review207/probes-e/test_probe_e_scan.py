"""Probe area E: scan many caller sentences on the sushi menu."""
import os

from api.core.errors import Ambiguous, NotFound
from api.domain.menu.search import position_parts, search_menu
from api.tests.test_domain_menu_search import NOW, KARTE, engine, session, sushi, tenant_id  # noqa: F401


def outcome(s, tid, said):
    try:
        parts = position_parts(s, tid, said, now=NOW)
    except Ambiguous as e:
        parts = f"AMB({e.say})"
    except NotFound as e:
        parts = f"NF({e.say})"
    try:
        r = search_menu(s, tid, said, now=NOW)
        res = (r.match_type, [h.number for h in r.results], r.wish and (r.wish.kind, r.wish.text, r.wish.note), r.say)
    except Ambiguous as e:
        res = f"AMB({e.say})"
    except NotFound as e:
        res = f"NF({e.say})"
    return parts, res


def test_scan(session, sushi):
    path = os.environ["PROBE_IN"]
    for line in open(path, encoding="utf-8"):
        said = line.rstrip("\n")
        if not said or said.startswith("#"):
            continue
        parts, res = outcome(session, sushi, said)
        print(f"{said!r}\n    parts={parts}\n    search={res}")
