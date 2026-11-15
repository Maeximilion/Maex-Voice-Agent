"""Skeptic E: the probe inputs through the agent's dispatch (position_parts first)."""
import json

import pytest

from api.agent.dispatch import dispatch
from api.tests.test_domain_draft_order import _call
from api.tests.test_domain_menu_search import NOW, KARTE, engine, session, sushi, tenant_id  # noqa: F401
from api.tests.test_probe_e_722 import prefixed  # noqa: F401
from api.domain.menu.importer import ALIASES_FILE, ALLERGENS_FILE, MENU_FILE, OPTIONS_FILE, apply, parse
from api.tests import test_probe_e_720 as p720


def show(session, tid, said):
    cid = _call(session, tid)
    r = dispatch(session, cid, tid, "search_menu", {"query": said}, now=NOW)
    d = r.data or {}
    if d.get("match_type") == "positions":
        parts = [(p["query"], p.get("match_type") or p.get("error_code"),
                  [h["number"] for h in p.get("results", [])],
                  (p.get("wish") or {}).get("kind"), (p.get("wish") or {}).get("text"),
                  (p.get("wish") or {}).get("option"), (p.get("wish") or {}).get("note"))
                 for p in d["positions"]]
        out = f"positions {parts}"
    else:
        w = d.get("wish") or {}
        out = (f"{d.get('match_type') or r.error_code} {[h['number'] for h in d.get('results', [])]} "
               f"wish={w.get('kind')} text={w.get('text')!r} opt={w.get('option')} "
               f"delta={w.get('price_delta_cents')} note={w.get('note')!r} ingr={w.get('ingredient')!r}")
    print(f"@@ {said!r} -> {out} || say={r.say!r}")


S721 = ["Pho Bo äh die 23", "die 23 äh Pho Bo", "die dreiundzwanzig äh Pho Bo", "Die 23. Pho Bo.",
        "Pho Bo. Die 23.", "Pho Bo? Die 23", "die 23 - Pho Bo", "die 23 also Pho Bo", "Pho Bo dazu die 23",
        "die 23 ohne Zwiebeln, nee, Pho Bo", "die 23 ohne Zwiebeln, äh, Pho Bo", "die 23 ohne Zwiebeln, oder Pho Bo",
        "S12 ohne Wasabi, äh, Pho Bo", "die 23 äh Pho Bo ohne Koriander",
        "die 23, ne, die 24", "die 23, quatsch, die 24", "die 23, sorry, die 24", "die 23, doch die 24",
        "die 23, Moment, die 24", "die 23, nein, die 24", "die 23, nee, die 24",
        "die 23 statt Pho Bo", "die 23 oder Pho Bo", "die 23 oder Ente", "die 23 plus Pho Bo", "Pho Bo mit der 23",
        "Pho Bo, ich habe eine Nussallergie! Ich habe keine andere Allergie, nur meine Frau gegen Sesam",
        "die 13, meine Tochter hat eine Erdnussallergie. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        "die 13, ich habe Zöliakie. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        "die 13, ich vertrage keine Nüsse. Ich habe keine Allergie, nur meine Frau gegen Sesam",
        "die 23 x die 24", "die 13 x die 12", "die dreizehn mal die zwölf", "die 13 mal Nummer 12",
        "Nummer 40 zwei bitte", "die 40 zwei, bitte"]


def test_sushi(session, sushi):
    for s in S721:
        show(session, sushi, s)


S722 = ["S40 zwei Portionen", "die S40 zwei Portionen", "Sushi vierzig zwei Portionen", "Sushi zwanzig zwei Portionen",
        "Sushi vierzig 2x", "S 20 2 Portionen", "S40 zwei mal", "S20 2x", "Nummer S40 zwei Portionen",
        "zwei Portionen S40", "zweimal S40", "S40 bitte, zwei Portionen",
        "Nummer 40 zwei bitte", "die 40 zwei, bitte", "die 23 x die 24", "die 13 x die 12", "die dreizehn mal die zwölf",
        "die 13 mal Nummer 12"]


def test_prefixed(session, prefixed):
    for s in S722:
        show(session, prefixed, s)


S720 = ["die 47 ohne Zwiebeln, plus Tofu", "die 47 ohne Zwiebeln, dazu Tofu", "die 47 ohne Zwiebeln, zusätzlich Tofu",
        "die 47 ohne Zwiebeln, stattdessen Tofu", "die 47 ohne Zwiebeln, aber Tofu", "die 47 ohne Zwiebeln, Tofu dazu",
        "die 47 mit Nudeln, ohne Zwiebeln, plus Tofu", "die 47 mit Nudeln, ohne Zwiebeln, dazu Tofu",
        "die 47 ohne Zwiebeln und Tofu",
        "die 47 ohne Zwiebeln, dafür 2x Tofu", "die 47 ohne Zwiebeln, dafür zweimal Tofu", "die 47 kein Huhn, sondern 2 Stück Tofu",
        "die 47 ohne Koriander, lieber ohne Zwiebeln", "die 47 kein Koriander, dafür ohne Zwiebeln",
        "zweimal die 47, einmal mit Tofu", "zweimal die 47, eine mit Nudeln", "zwei 47er, einmal mit Tofu",
        "die 47 mit 2x Tofu", "die 47 mit zweimal Tofu", "die 47 mit 2 Stück Tofu", "Nummer 47 mit 2x Tofu"]


def test_720(session):
    import uuid
    from scripts.seed import seed
    tid = uuid.UUID(seed(session, tenant_name="ProbeE", timezone="Europe/Berlin").tenant_id)
    plan = parse(p720.KARTE)
    assert plan.ok, plan.errors
    apply(session, tid, plan, now=NOW)
    for s in S720:
        show(session, tid, s)
