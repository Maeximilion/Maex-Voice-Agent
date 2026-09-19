## 2026-09-19 - [N+1 query in N-to-N lookup]
**Learning:** Found an N+1 issue in `api/domain/menu/search.py`. `_by_number` is called repeatedly in a list comprehension without eager loading if multiple marked items are present, but it performs a DB select inside the loop.
**Action:** Always verify loops over external inputs to make sure we load everything via an `IN` clause or single query if possible.
