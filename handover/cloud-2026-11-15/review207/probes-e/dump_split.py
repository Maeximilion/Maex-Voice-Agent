import sys
sys.path.insert(0, ".")
exec(open(sys.argv[1]).read().split("for line in sys.stdin")[0])
for line in sys.stdin:
    t = line.rstrip("\n")
    if not t: continue
    c = wish_candidates(t)
    if not c:
        print(repr(t), "-> no wish"); continue
    dish, wish, seg = c[0]
    w = classify_wish(wish, G)
    print(repr(t), "-> dish", repr(dish), "wish", repr(wish), "has_number", has_number(wish), "->", None if w is None else (w.kind, w.option, w.price_delta_cents, w.note))
