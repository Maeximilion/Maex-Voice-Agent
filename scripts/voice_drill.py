"""Entry of the outage drills of the AI telephone (T-1.11, docs/20 §10).

    python3 -m scripts.voice_drill [--dry-run] [--keep] [--down] [--run-dir DIR]

The kit is `api/telephony/drill.py`: it drives the telephone containers, and
only `telephony/` knows them (docs/11 §telephony). Options and exit codes are
described there.
"""

import sys

from api.telephony.drill import main

if __name__ == "__main__":
    sys.exit(main())
