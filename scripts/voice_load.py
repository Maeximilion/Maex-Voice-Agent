"""Entry of the load test of one server of D3 (#841, docs/13 "Load test").

    python3 -m scripts.voice_load [--max-stacks 6] [--calls 3] [--turns 4] [--keep] [--down]

The kit is `api/telephony/load.py`: it drives the telephone containers, and
only `telephony/` knows them (docs/11 §telephony). Options and exit codes are
described there.
"""

import sys

from api.telephony.load import main

if __name__ == "__main__":
    sys.exit(main())
