# feat/530-prompt-v4: stopped, already fixed on main

Branch feat/530-prompt-v4 (worktree /home/user/wt-530-prompt-v4): no commits, no file changes.

Finding: on origin/main (69ef3678) the newest prompt is still prompts/system_v3.md, but its out_of_zone line was already fixed in place by commit 04a3cfe8 "fix(prompt): after out_of_zone the model speaks only the say (#684)". Line 32 now reads "`out_of_zone`: nur `say`." instead of "`say`, Abholung anbieten". It is pinned by api/tests/test_prompts_v3.py::test_out_of_zone_offers_pickup_only_through_its_say and covered by eval evals/cases/lieferung_0091_out_of_zone_with_pickup_off_offers_no_pickup.json. Per the brief, no v4 was created. Issue #530 can be closed as fixed by #684 (coordinator's job).

Coverage gap (not built): lieferung_0091 uses `pickup_enabled: false`. No eval covers pickup switched on but closed by the hours (out of zone at 22:00, pickup until 21:00). Optional follow-up: one eval case with the clock at 22:00 and never_said pickup sentences.

Same class of defect in system_v3 (lines that offer a service without an open check), none changed:
- Line 10 "Ohne Tool-Ergebnis: Rückruf anbieten." offers a callback unconditionally (callbacks have no hours, likely fine).
- Lines 14 and 36 call transfer_to_team; guarded by tools_v3 line 65 (available: false then create_callback). Fine.
- Line 23 alternatives come from check_slot. Fine.
- Line 26 skips get_service_status when `service` is already in slots; not an offer, draft_order still checks.
No unguarded service offer remains.

Tests: none run (no change). GPU run not needed for this issue.
