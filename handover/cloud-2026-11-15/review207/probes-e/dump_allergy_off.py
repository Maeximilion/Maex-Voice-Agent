import sys
sys.path.insert(0, ".")
import api.domain.menu.wishes as W
W._theirs_behind_a_denial = lambda text, groups: None
exec(open(sys.argv[1]).read().split("for line in sys.stdin")[0])
for line in sys.stdin:
    t = line.rstrip("\n")
    w = classify_wish(t, G)
    print(f"{t!r}\n    -> {None if w is None else (w.kind, w.ingredient, w.text, w.note)}")
