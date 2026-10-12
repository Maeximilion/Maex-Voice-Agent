"""Entry of the kit for the spike T-10.1 step 1 (docs/20 §10).

    python3 -m scripts.voice_spike [--dry-run] [--keep] [--down] [--run-dir DIR]

The kit is `api/telephony/spike.py`: it drives the PBX containers, and only
`telephony/` knows the PBX (docs/11 §telephony). Options and exit codes are
described there.
"""

import sys

from api.telephony.spike import main

if __name__ == "__main__":
    sys.exit(main())
