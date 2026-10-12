"""Entry of the kit for the spike T-10.3 (docs/20 §6, §10).

    python3 -m scripts.speech_spike [--dry-run] [--limit N] [--run-dir DIR]

The kit is `api/telephony/speech_spike.py`: it names the speech engines, and
only `telephony/` knows them (docs/11 §telephony). Options and exit codes are
described there.
"""

import sys

from api.telephony.speech_spike import main

if __name__ == "__main__":
    sys.exit(main())
