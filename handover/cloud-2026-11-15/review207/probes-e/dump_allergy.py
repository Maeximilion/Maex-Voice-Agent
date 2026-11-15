import sys
sys.path.insert(0, ".")
exec(open(sys.argv[1]).read().split("for line in sys.stdin")[0])
from api.domain.menu.wishes import only_denies, denies_allergy
for line in sys.stdin:
    t = line.rstrip("\n")
    if not t: continue
    w = classify_wish(t, G)
    print(f"{t!r}\n    -> {None if w is None else (w.kind, w.ingredient, w.text, w.note)}  only_denies={only_denies(t)}")
