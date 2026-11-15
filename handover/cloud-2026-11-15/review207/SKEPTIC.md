# Skeptic brief, daily review #207 batch 2, main @ 69ef3678

You get one area's notes file with findings from a reviewer. Your job: try to DISPROVE every P0, P1 and P2
finding before an issue is filed. You are adversarial to the reviewer, not to the code.

For each finding:
1. Re-run the reviewer's probe (copy it from the probes folder into YOUR worktree's `api/tests/`, run with
   `. /tmp/claude-0/-home-user-Maex-Voice-Agent/57c61f4a-774e-5270-a000-171ac1882a8d/scratchpad/testenv.sh && python3 -m pytest -q -p no:cacheprovider <file>`).
   Check the probe tests what the finding claims, through the real entry point a call takes (not a
   private helper called with inputs the real path never produces).
2. Look for what makes it not a bug: a guard elsewhere in the path (an earlier filter, a later check, the
   prompt, the tool contract in docs/04, the core escalating anyway), a doc that says the behaviour is
   intended (grep docs/ and docs/HISTORY.md for the PR or issue number and Maxi's decisions), a test that
   pins the behaviour on purpose, or an input that cannot happen.
3. Verdict per finding: CONFIRMED (bug holds through the real path), DOWNGRADE (real but lower severity:
   say which and why), REJECTED (with evidence), or UNCLEAR (what would settle it).
4. For CONFIRMED and DOWNGRADE: name the smallest fix direction and the test that would prove it, and
   whether it needs Maxi's decision (product behaviour, law, money) or is a plain fix.

Rules: read-only on tracked files; probes only in your own worktree; never commit or push; never touch
/home/user/Maex-Voice-Agent or another worktree. Read `CLAUDE.md` section 2 for the hard rules.
Large docs only by grep and ranges. Write your verdicts AS YOU GO to your output file.

Output file format, one block per finding:
```
### <finding title as in notes>
Verdict: CONFIRMED | DOWNGRADE to Px | REJECTED | UNCLEAR
Evidence: <probe run result, file:line of the guard or doc, 2-5 lines>
Fix: <direction> · Test: <idea> · Needs Maxi: yes (why) | no
```
Final reply: max 150 words, verdict counts and output path.
