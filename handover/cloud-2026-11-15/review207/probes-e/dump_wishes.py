import sys
from api.schemas.menu import OptionGroup, OptionOut  # noqa
from api.domain.menu.wishes import classify_wish, wish_candidates, has_number

G = [
    OptionGroup(group="Fleisch", required=True, options=[
        OptionOut(name="Ente", price_delta_cents=0, default=True),
        OptionOut(name="Huhn", price_delta_cents=-100, default=False)]),
    OptionGroup(group="Beilage", required=False, options=[
        OptionOut(name="Reis", price_delta_cents=0, default=True),
        OptionOut(name="Nudeln", price_delta_cents=300, default=False)]),
    OptionGroup(group="Extra", required=False, options=[
        OptionOut(name="Ei", price_delta_cents=100, default=False),
        OptionOut(name="Tofu", price_delta_cents=150, default=False),
        OptionOut(name="Käse", price_delta_cents=120, default=False)]),
]
for line in sys.stdin:
    t = line.rstrip("\n")
    if not t: continue
    w = classify_wish(t, G)
    print(repr(t), "->", None if w is None else (w.kind, w.option, w.price_delta_cents, w.note, w.text))
