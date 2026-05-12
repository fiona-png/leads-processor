"""One-shot: apply `cole-leads/processed` to every existing lead-tagged email.

Use ONCE, before the production cron starts, to set the high-water mark so the
first run doesn't try to reprocess 10k+ historical messages. After this, every
new lead falls through the normal pipeline.

Usage:
    # Dry run — count matches, write nothing.
    python scripts/backfill_processed_label.py --dry-run

    # Real run — apply the label to every match (paginates + 1000-per-batch).
    python scripts/backfill_processed_label.py

Idempotent: the same query is `-label:cole-leads/processed`, so re-running
finds zero new matches once the first pass completes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `src/` importable when invoked as a script.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cole_leads.config import get_settings  # noqa: E402
from cole_leads.gmail import LEADS_QUERY, GmailClient  # noqa: E402


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="backfill_processed_label.py",
        description=(
            "Apply cole-leads/processed to every existing lead email so the "
            "production pipeline starts from a clean high-water mark."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Count matching messages without applying the label.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    settings = get_settings()
    gmail = GmailClient(
        client_id=settings.gmail_client_id,
        client_secret=settings.gmail_client_secret,
        refresh_token=settings.gmail_refresh_token,
        user_email=settings.gmail_user,
    )

    total = gmail.bulk_apply_processed_label(LEADS_QUERY, dry_run=args.dry_run)

    if args.dry_run:
        print(f"[dry-run] {total} messages would be labeled cole-leads/processed.")
    else:
        print(f"Labeled {total} messages cole-leads/processed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
