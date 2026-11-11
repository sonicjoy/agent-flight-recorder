"""python -m flight_recorder.hook: what Claude Code runs for each hook event.

No CLI framework: a new process starts for every event, so it stays light.
"""

import sys

from flight_recorder.hook.client import run

run(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
