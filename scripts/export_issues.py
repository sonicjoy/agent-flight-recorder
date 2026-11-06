"""Re-export this repo's issues to the triager's fixtures (issue_triager/fixtures/issues.json).

Read-only (uses `gh issue list`). Run with: uv run python scripts/export_issues.py
"""

from issue_triager.export import main

if __name__ == "__main__":
    main()
