# research/637-watermark-feasibility

Branch research/637-watermark-feasibility, commit 3c6753eb (not pushed).

Files: scripts/watermark_line.py (new), api/tests/test_scripts_watermark_line.py (new, 10 tests),
scripts/README.md, docs/20_VOICE_LAYER.md (section 10 handover for #637 and a section 11 log row),
pyproject.toml (mypy missing-import list: torch, audioseal, perth, wavmark),
.github/workflows/ci.yml (the new test joins the numpy step).

Speech: flite through ffmpeg, English (Piper voices sit on Hugging Face, which is blocked here).
18 sentences, 83 s.

Results on the modelled line (repo speech_bench line):
- Spread spectrum (classic method I wrote, no package): found in 18 of 18 at every stage. Minimum z:
  16.2 after A-law, 14.1 with noise, 13.9 with 3 % packet loss, 12.6 with 100 ppm drift,
  7.6 after GSM, 8.3 after Opus. Threshold 6. False positives: 0 of 900 (max 5.65).
  Costs 1.4 to 5.4 ms per second of audio. Mark sits 26.6 dB below the speech; nobody listened to it.
  At -25 dB 61 % are found after GSM.
- PerTh 1.0.1: lost. Scores 0.60/0.87 after A-law, 0.41/0.62 with noise, 0.10/0.47 after GSM, on
  only 2 sentences. Needs about 1 s per second of audio on this loaded CPU.
- AudioSeal 0.2.1 and WavMark 0.0.3: their weights are on Hugging Face, so they could not load.

Tests: 10 new tests plus the bench, ear-floor and docs tests: 456 passed. To run, audioop-lts went
on a scratch PYTHONPATH (this machine has Python 3.13). The speech_bench tests fail to collect here
without that path, and that was already so before this change. ruff is clean. mypy reports 5 errors
that are already on main and come from numpy being installed here; none is in the new files.

Not run: the full suite (the coordinator said to stop), the real SIP leg, and AudioSeal.

Open for Maxi: run AudioSeal and the real leg on the workbench (docs/20 section 10)?
A (recommended): yes, before the first real call.
B: take spread spectrum as the mark and test whether it can be heard.
