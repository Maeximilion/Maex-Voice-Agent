"""Entry of the kit for the spike T-10.3 (docs/20 §6, §10).

    python3 -m scripts.speech_spike [--dry-run] [--limit N] [--run-dir DIR]

The kit is `api/telephony/speech_spike.py`: it names the speech engines, and
only `telephony/` knows them (docs/11 §telephony). Options and exit codes are
described there.

On a branch other than `main` the run records its headline numbers in
`docs/metrics/voice_history.csv` and redraws the README images; on `main` they
wait in `evals/reports/` (`scripts/readme_assets.py pending`).
"""

import sys
import time

from api.telephony.speech_spike import SPEECH_DIR, main
from scripts.readme_assets import record_kit_run

if __name__ == "__main__":
    started = time.time()
    code = main()
    record_kit_run("speech", sys.argv[1:], SPEECH_DIR / "run", started)
    sys.exit(code)
