# Fable consult: menu parsing, main 69ef3678 (read, nothing run)

**Diagnosis.** One shape throughout: a parser lists an open word class (joiners, corrections, hesitations, units) and lets everything unlisted fall through to a committing branch: free note, number-stripping name search, separate positions, a 40+2 join.

**Change: one conservation gate that inverts the default.** Detect only closed classes; the final reading must claim each such token exactly once, an unclaimed one makes the result an ask (ladder). Closed classes: numbers and counts; the hit dish's option names, dish names and aliases (database); allergy and health words; a short whitelist of ignorable filler. One new `domain/menu` module, called from `dispatch._search_menu`:

- N: a number with article, marker or prefix is only ever a card number and must be a hit's; a count is the one dish quantity beside the dish, else ask.
- O: a note never holds an option of its dish outside direct "ohne/kein" government, nor a dish name or card number.
- A: every allergy or health mention is the wish's ingredient or denied in its own clause; else open allergy.
- P: a piece that is neither hit, number, wish nor whitelisted filler keeps the sentence whole and asks.

The gate only converts commit to ask, so it cannot create a wrong booking; its cost, over-asking, is measured by the corpus. It closes all nine confirmed P1. Left as lexer rules: the 40-zwei join, the two-removal echo, not_found instead of an ask.

**Order.**
1. Corpus PR: the 56 red probes and the "Checked and clean" lists as table rows, plus `itertools.product` of dish form x residue (including a nonsense word) x second element, asserted by N/O/A/P. Red rows `xfail(strict=True)`.
2. Gate PR: flips them; clean rows stay clear; evals on the head.
3. One PR per function group: delete patches the gate made dead.
4. Optionally move the parsers onto the gate's lexer.

**Model versus code.** The model forwards the raw sentence and speaks the `say`; it never segments. Gap: the model sets `quantity` itself; return the claimed quantity and let the `draft_order` guard compare.

**Do not.**
- Rewrite 4,600 `xhigh` lines into a shared tokenizer in one step.
- Add another word list, a fix per example, or a dependency (hypothesis).
- Move disambiguation to the model, or count the read-back as the net.
- Loosen the gate before Maxi has seen the over-ask numbers.
