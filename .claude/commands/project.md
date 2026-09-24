Maintain the GitHub Project board. This is the only command allowed to write to it.

1. Read `docs/16_GITHUB_PROJECT.md` if the conventions are not already in context.
2. Run the same pass the daily Action runs, with the local gh login:
   `PROJECT_TOKEN=$(gh auth token) PROJECT_OWNER=Maeximilion PROJECT_NUMBER=2 PROJECT_REPO=Maeximilion/Maex-Voice-Agent python scripts/project_report.py --fix`
3. Apply the remaining mechanical fixes directly, without asking - Maxi gave standing authorization on 2026-09-24:
   - duplicate item (same URL twice): archive the extra entry with `gh project item-archive`, never delete it. Keep the entry with more fields filled; on a tie keep the first one the report lists. Archiving is reversible and leaves the issue untouched
   - item without Status: set it from what is actually finished - open = Todo, closed issue or merged pull request = Done. A pull request closed without merge is not finished: leave it without Done and list it for a decision, like the report does
   - archived entry whose issue or PR is open again: unarchive it with `gh project item-archive --undo`, never add it anew - but only when no active entry with the same URL exists. Otherwise it is an archived duplicate and stays archived; the report already leaves those out
   - open issue or PR missing from the board: add it. The report already counts archived entries as present, so this never duplicates one
4. Propose, do not apply: assigning iterations, archiving anything other than a confirmed duplicate, new views or fields, anything touching docs/07 priorities.
5. Report in at most five lines: what was fixed, what waits for a decision. Then leave the board alone until the next `/project`.

Never create a second project. Never add an item twice. Never delete a project, an item or a field - a duplicate is archived, not deleted. Never touch the board from inside `/task`, `/done` or `/bug`.
