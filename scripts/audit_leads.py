"""Audit lead rows and fill the "Claude Lead Check" field.

    python scripts/audit_leads.py                 # bot-created leads, last 120 days
    python scripts/audit_leads.py --days 365      # wider window
    python scripts/audit_leads.py --all-leads     # every lead in the window, not just bot-created
    python scripts/audit_leads.py --dry-run       # print what would change, write nothing

Only the Claude Lead Check field is ever written. Rows whose note starts with
"REVIEWED" are left alone.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from cole_leads.airtable import AirtableClient
from cole_leads.audit import audit_rows, default_window


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--days", type=int, default=120, help="Audit leads dated within N days (0 = all)."
    )
    p.add_argument("--all-leads", action="store_true", help="Include leads not created by the bot.")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)

    today = date.today()
    since = default_window(today, a.days) if a.days else None
    with AirtableClient() as at:
        index = at.load_relationship_index()
        rows, named = at.load_audit_rows(since=since, bot_only=not a.all_leads, index=index)
        notes, summary = audit_rows(rows, index=index, all_named=named, today=today)
        if a.dry_run:
            for rid, text in notes.items():
                print(f"--- {rid}\n{text}")
        else:
            at.write_claude_checks(notes)

    result = {
        "audited": summary.audited,
        "ok": summary.ok,
        "flagged": summary.flagged,
        "written": 0 if a.dry_run else summary.written,
        "would_write": summary.written if a.dry_run else None,
        "skipped_reviewed": summary.skipped_reviewed,
        "issues": dict(summary.by_issue.most_common()),
    }
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
