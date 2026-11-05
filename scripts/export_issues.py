"""Re-export this repo's issues to examples/issue_triager/fixtures/issues.json.

Read-only (uses `gh issue list`). Run with: uv run python scripts/export_issues.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))

from issue_triager.export import main

if __name__ == "__main__":
    main()
