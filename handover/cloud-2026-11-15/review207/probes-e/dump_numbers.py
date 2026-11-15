import sys
sys.path.insert(0, ".")
from api.domain.menu.numberwords import CardFormat, sole_item_number, find_quantity
items = [(f"S{i}", "Sushi") for i in range(1, 54)] + [(f"SM{i}", "Sushi") for i in range(1, 7)]
items += [(str(i), "Haupt") for i in range(1, 120)] + [("25G", "Haupt"), ("30a", "Haupt"), ("26d", "Haupt"), ("60G", "Haupt"), ("40", "Haupt")]
card = CardFormat.from_items(items)
for line in sys.stdin:
    t = line.rstrip("\n")
    if not t or t.startswith("#"): continue
    ref, unclear = sole_item_number(t, card)
    r = None if ref is None else (ref.text, sorted(ref.cards) if hasattr(ref, "cards") else None, ref.heard, ref.valid)
    print(f"{t!r:45} -> ref={r} unclear={unclear} qty={find_quantity(t)}")
