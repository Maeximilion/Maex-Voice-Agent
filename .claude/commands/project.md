Maintain the GitHub Project board. This is the only command allowed to write to it.

1. Read `docs/16_GITHUB_PROJECT.md` if the conventions are not already in context.
2. Run the daily check locally and show the report:
   `PROJECT_TOKEN=$PROJECT_TOKEN PROJECT_OWNER=Maeximilion PROJECT_NUMBER=$PROJECT_NUMBER python scripts/project_report.py`
   If the token or number is missing: name the missing one, point to `docs/16_GITHUB_PROJECT.md` section 5, stop.
3. Group the findings: what GitHub's built-in workflows should have handled (a misconfiguration, fix the workflow), and what needs a human decision.
4. Propose the concrete changes as a list, one line each. Wait for confirmation before any write.
5. After the write: state what changed. Do not touch the board again until the next `/project`.

Never create a second project. Never add an item twice. Never set Status by hand.
