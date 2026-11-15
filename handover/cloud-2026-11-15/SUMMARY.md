# Summary for Maxi: cloud session 15.11.2026 (18:13 to 21:40)

Nothing merged, nothing on GitHub except this carrier branch. Base: `main` 69ef3678. Take-over: README.md.

## 1. Branches (in `branches.bundle`, commits in `commits.md`, report per branch in `build/`)

| Branch | What | Tests | Evals (scripted) | State |
|---|---|---|---|---|
| fix/764-allergen-guard | #747: model allergen statements never reach the guest; #748: absence forms ("ist keine Erdnuss drin") count as claims; short allergens (Kiwi, Mais, Zimt) stay off callback cards | 39 new, red first | 103/103 | ready for /done |
| fix/764-dialog-batch | #744: the order verb is no longer read as the complaint wish (order went lost); #750: a draft made 00:00-04:59 reads weekday and date, never heute/morgen | 18 new | 103/103 | ready, 1 decision (Q6) |
| fix/764-staff-weekday | #754: close then reopen today no longer drops "wie Sonntag" without the owner | new, red first | not needed | ready |
| fix/764-outage-path | #754: the outage sentence plays before the team number is read (`speak_now`); #743: the hang-up card names a confirmed booking | new, red first | 103/103 | ready; drills on the workbench (report) |
| fix/207-signin-repeat-address | #731: a replayed code from another address is audited `factor`, not `repeat` | 3 new | not needed | ready |
| fix/207-phone-trunk-zero | #726: spoken trunk zero dropped after 26 sourced European codes (Italy and others keep it) | 9 new | not run | ready, 1 decision (Q7) |
| fix/207-menu-regressions | WIP: restores the two regressions of #721 and #735 | probes only | **allergie_0026 red** | WIP, see commit body |
| feat/574-selftest | `python -m sim.selftest [--print-slip]`: health, DB head, config, menu, a rolled-back simulated reservation, optional test slip | 17 new | path unchanged | ready, 1 decision (Q8) |
| feat/576-offboarding | export job (menu round-trips through the import, orders and reservations CSV) and `scripts/remove_stack.py` (dry run by default, needs the export, keeps a record without personal data) | new | not needed | ready, 2 decisions (Q9) |
| docs/575-golive-checklist | marks every item of the existing checklist as auto, planned, hand or Maxi | doc tests | - | ready |
| research/637-watermark-feasibility | simulated line: a classic spread-spectrum mark survives A-law, band and noise (18/18, 0 false positives, 1.4-5.4 ms per s); PerTh lost; AudioSeal not testable here (Hugging Face blocked) | 10 new | - | ready, 1 decision (Q10) |
| docs/status-upkeep-2026-11-15 | weekly sweep: only 1.2 KB qualified (everything else younger than a month) | doc tests | - | ready; row decisions in build/status-row-decisions.md (Q11) |

Every full suite run here: the sandbox baseline (1 failed db_role login, 13 audioop import errors), nothing new.
Not built: #530 needs no prompt change (fixed by #684); one eval for "pickup on but closed by hours" is open
(Q12). The menu test corpus (step 1 of the redesign) was started and stopped at the usage limit: nothing kept.

## 2. Reviews

- **Ledger #764 (14 PRs, `/code-review max` level):** `review764/REPORT.md`. 5 PRs clean (#745, #749, #755,
  #774, #783). Every confirmed P1 is on a fix branch above. Tick the ledger after filing (commands in the report).
- **#207 batch 2 (15 fix PRs of #546 without a deep review):** `review207/` (area-e/f/g, skeptic-e/f/g). Fixed:
  #731, #726, the two regressions (WIP). Downgraded to P3: #725 24:00, #724 dead-end text, #729 window rows,
  #730 x2, tablet number. No REPORT.md yet for this batch: the next session writes it from the skeptic files.
- **Redesign trigger (docs/21 "Review loop limits") fired for the menu parser:** 12 confirmed findings in five
  function groups, each patched three rounds or more. Per CLAUDE.md, Fable use 1: `review207/fable-consult.md`
  (a conservation gate: an unclaimed token makes the result an ask; corpus PR first). Nothing of it is built.

## 3. Decisions (closed questions, recommendation first)

1. **Menu parser redesign (Fable consult):** A build the conservation gate in the consult's order (corpus, gate, cleanup), recommended · B keep patching per finding · C a Fable design session first (use 2).
2. **#751 second person's allergy (D1 of REPORT):** A coverage check per named allergen, recommended · B keep #788 and measure (#787) · C prompt rule only.
3. **#757 stranded guest list (D2):** B a standing per-date block computed at render, recommended · A card stays until ticked off · C list on waiting cards and until the date passes.
4. **#757 past tables (D3):** A list tables from now minus 60 min, recommended · B from now · C whole day.
5. **Ledger lines:** file the P2/P3 drafts of REPORT section 4 as written? A yes, recommended · B into #207 only.
6. **"Ich möchte die Pizza reklamieren"** now goes to the team instead of the remedy offer: A keep, recommended · B restore the offer.
7. **Phone normalisation:** A keep the sourced list, recommended · B libphonenumber (new dependency).
8. **Self-test card on the real tablet:** A no, rollback only, recommended · B yes (`is_test` column, migration).
9. **Offboarding:** a removed customer in off-site backups: A let them expire with the rolling 30 days, recommended · B delete at once. A stack with a second tenant: A remove by hand, recommended · B build a per-tenant delete.
10. **#637:** run AudioSeal and the real SIP leg on the workbench before the first real call? A yes, recommended · B the spread-spectrum result is enough.
11. **STATUS size:** take the firm row set of `build/status-row-decisions.md` (to about 89 KB) and archive the 24 old header lines? A yes, recommended · B decide row by row.
12. **#530:** add the eval "out of zone, pickup on but closed by its hours" and close #530? A yes, recommended · B close #530 as fixed by #684 now.

## 4. Unchanged facts checked

- #775: Docker Hub still has no `andrius/asterisk` 22.12 tag (checked 18:40); the pin stays.
