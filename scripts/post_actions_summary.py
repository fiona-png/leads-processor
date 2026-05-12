"""Emit a one-section markdown summary of a RunSummary JSON for $GITHUB_STEP_SUMMARY.

Called from .github/workflows/run.yml after the pipeline step. Writes
to stdout — the workflow appends our stdout to $GITHUB_STEP_SUMMARY.

Robust to a missing or unparseable input file (e.g. when the pipeline
crashed before printing): emits a section that says so.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("### Cole leads processor\n\nNo summary file path given.")
        return 0

    path = Path(argv[1])
    if not path.exists() or path.stat().st_size == 0:
        print("### Cole leads processor")
        print()
        print("Pipeline did not emit a summary — check the Run pipeline step logs.")
        return 0

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print("### Cole leads processor")
        print()
        print(f"Could not parse summary JSON: `{e}`")
        return 0

    counts = {
        k: data.get(k, 0)
        for k in ("total", "created", "skipped_duplicate", "skipped_filter", "failed")
    }
    print("### Cole leads processor")
    print()
    print(f"- **Total**: {counts['total']}")
    print(f"- **Created**: {counts['created']}")
    print(f"- **Skipped (duplicate)**: {counts['skipped_duplicate']}")
    print(f"- **Skipped (filter)**: {counts['skipped_filter']}")
    print(f"- **Failed**: {counts['failed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
