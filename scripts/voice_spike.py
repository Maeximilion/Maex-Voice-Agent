"""Entry of the kit for the spike T-10.1 step 1 (docs/20 §10).

    python3 -m scripts.voice_spike [--dry-run] [--keep] [--down] [--run-dir DIR]

The kit is `api/telephony/spike.py`: it drives the PBX containers, and only
`telephony/` knows the PBX (docs/11 §telephony). Options and exit codes are
described there.

On a branch other than `main` the run records its headline numbers in
`docs/metrics/voice_history.csv` and redraws the README images; on `main` they
wait in `evals/reports/` (`scripts/readme_assets.py pending`).
"""

import sys
import time

from api.telephony.spike import CONFIG_DIR, main
from scripts.readme_assets import record_kit_run

if __name__ == "__main__":
    started = time.time()
    code = main()
    record_kit_run("spike", sys.argv[1:], CONFIG_DIR / "run", started)
    sys.exit(code)
