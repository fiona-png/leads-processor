"""One-shot prod runner used for the n8n -> Python cutover.

Runs the full pipeline against real Gmail + Airtable + Anthropic, exactly once,
with a conservative default of `--max 1` so an oops won't process the whole
inbox. Prints the `RunSummary` as pretty JSON. Exit code 1 if any lead failed.

Usage:
    python scripts/run_once.py --dry-run --fixture tests/fixtures/forwarded_vpm_lead.txt
    python scripts/run_once.py --max 1
    python scripts/run_once.py --max 5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make `src/` importable when running as a script.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cole_leads.airtable import AirtableClient  # noqa: E402
from cole_leads.gmail import GmailClient  # noqa: E402
from cole_leads.llm import LLMClient  # noqa: E402
from cole_leads.models import ProcessResult, RunSummary  # noqa: E402
from cole_leads.pipeline import _load_fixture_email, process_one_lead, run  # noqa: E402


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_once.py",
        description="Run the Cole leads processor against real services, once.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip Airtable writes and Gmail label changes; LLM still runs.",
    )
    p.add_argument(
        "--max",
        type=int,
        default=1,
        help="Maximum leads to process this run. Default 1 (safety).",
    )
    p.add_argument(
        "--fixture",
        type=str,
        default=None,
        help="Optional fixture email path. If set, skips Gmail entirely.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    airtable = AirtableClient()
    llm = LLMClient()

    if args.fixture:
        # Single-lead path. Gmail is stubbed because the email came from disk;
        # there's no real message to label.
        from cole_leads.stubs import StubGmailClient

        email = _load_fixture_email(Path(args.fixture))
        result: ProcessResult = process_one_lead(
            email,
            gmail=StubGmailClient(),  # type: ignore[arg-type]
            airtable=airtable,
            llm=llm,
            dry_run=args.dry_run,
        )
        summary = RunSummary(results=[result])
    else:
        gmail = GmailClient()
        summary = run(
            gmail=gmail,
            airtable=airtable,
            llm=llm,
            max_leads=args.max,
            dry_run=args.dry_run,
        )

    print(json.dumps(summary.model_dump(mode="json"), indent=2, default=str))
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
