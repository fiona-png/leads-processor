"""End-to-end pipeline: Gmail -> filter -> Claude -> Airtable.

Idempotency contract:
  1. Look up the Gmail message ID on the Searches table. If a Search already
     exists for it, mark the message processed in Gmail and skip.
  2. Apply filters (drop replies, internal-only chatter, etc.). Mark processed
     and skip if filtered.
  3. Parse inner forwarded headers and hand both the body and the headers to
     Claude. One LLM call combines parse + research.
  4. Upsert Client, find-or-create Investors, link them, resolve Lead Source,
     check for prior closed engagements, build a `SearchRecord`, create it.
  5. Mark the message `cole-leads/processed`. On any step-3+ failure, mark
     `cole-leads/failed` instead so the run continues and humans can review.

Eventual consistency: a failure between writing investors and writing the
Search will leave orphan investor links. That's fine — the Search-row write is
the last step and the idempotency key, so a retry produces no duplicates.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from . import filters
from .config import TEAM_MAP
from .logging import get_logger
from .models import (
    ProcessResult,
    RawEmail,
    RunSummary,
    SearchRecord,
)

if TYPE_CHECKING:
    from .airtable import AirtableClient
    from .gmail import GmailClient
    from .llm import LLMClient

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Single-lead processor
# ---------------------------------------------------------------------------


def process_one_lead(
    raw_email: RawEmail,
    *,
    gmail: GmailClient,
    airtable: AirtableClient,
    llm: LLMClient,
    dry_run: bool = False,
) -> ProcessResult:
    """Run the full pipeline on one email. Never raises — all failures are
    captured into the returned `ProcessResult`.

    Calls in `dry_run`:
      * All Airtable READ methods still run (so we can see whether the client/
        searches exist).
      * Airtable WRITES are skipped and logged instead.
      * Gmail label writes (`mark_processed`, `mark_failed`) are skipped.
      * The LLM is called normally — that's the whole point of a dry run.
    """
    msg_id = raw_email.message_id
    logger.info(
        "pipeline_start",
        extra={"message_id": msg_id, "subject": raw_email.subject, "dry_run": dry_run},
    )

    # --- 1. Idempotency check ----------------------------------------------
    if airtable.search_exists_for_message_id(msg_id):
        logger.info("pipeline_skip_duplicate", extra={"message_id": msg_id})
        if not dry_run:
            gmail.mark_processed(msg_id)
        return ProcessResult(message_id=msg_id, status="skipped_duplicate", dry_run=dry_run)

    # --- 2. Filter ---------------------------------------------------------
    if not filters.should_process_email(raw_email):
        logger.info(
            "pipeline_skip_filter",
            extra={"message_id": msg_id, "subject": raw_email.subject},
        )
        if not dry_run:
            gmail.mark_processed(msg_id)
        return ProcessResult(message_id=msg_id, status="skipped_filter", dry_run=dry_run)

    # --- 3. Inner forwarded headers ----------------------------------------
    inner = filters.extract_inner_headers(raw_email)
    logger.info(
        "pipeline_inner_headers",
        extra={
            "message_id": msg_id,
            "inner_from": inner.from_,
            "inner_subject": inner.subject,
        },
    )

    # --- 4. LLM ------------------------------------------------------------
    try:
        lead = llm.parse_and_research(raw_email, inner)
    except Exception as exc:  # noqa: BLE001 — we want to catch and continue
        logger.exception("pipeline_llm_failed", extra={"message_id": msg_id})
        if not dry_run:
            gmail.mark_failed(msg_id, f"llm: {exc}")
        return ProcessResult(
            message_id=msg_id,
            status="failed",
            error=f"llm: {exc}",
            dry_run=dry_run,
        )

    logger.info(
        "pipeline_parsed_lead",
        extra={
            "message_id": msg_id,
            "client": lead.parsed.client,
            "role": lead.parsed.role,
            "seniority": lead.parsed.seniority,
            "lead_source_type": lead.parsed.lead_source_type,
        },
    )

    # The LLM signals "this wasn't actually a lead" by emitting client=null.
    # Skip the row and mark the message processed so we don't keep re-running
    # the LLM on it.
    if lead.parsed.client is None:
        logger.info("pipeline_skip_no_lead", extra={"message_id": msg_id})
        if not dry_run:
            gmail.mark_processed(msg_id)
        return ProcessResult(
            message_id=msg_id,
            status="skipped_no_lead",
            lead=lead,
            dry_run=dry_run,
        )

    # --- 5. Airtable writes ------------------------------------------------
    try:
        # 5a. Client (upsert)
        if dry_run:
            existing_client = airtable.find_client_by_name(lead.parsed.client)
            if existing_client:
                client_id = existing_client
                logger.info(
                    "pipeline_dry_client_exists",
                    extra={"client": lead.parsed.client, "id": client_id},
                )
            else:
                client_id = "rec_DRY_NEW_CLIENT"
                logger.info(
                    "pipeline_dry_would_create_client",
                    extra={
                        "client": lead.parsed.client,
                        "research": lead.research.model_dump(),
                    },
                )
        else:
            client_id = airtable.upsert_client(lead.parsed.client, lead.research)

        # 5b. Investors
        investor_ids: list[str] = []
        for inv_name in lead.research.investors:
            if dry_run:
                inv_id = f"rec_DRY_INV_{len(investor_ids) + 1}"
                logger.info(
                    "pipeline_dry_would_upsert_investor",
                    extra={"investor": inv_name, "placeholder_id": inv_id},
                )
            else:
                inv_id = airtable.find_or_create_investor(inv_name)
            investor_ids.append(inv_id)

        # 5c. Link investors (skip when empty — no need to fetch + PATCH)
        if investor_ids:
            if dry_run:
                logger.info(
                    "pipeline_dry_would_link_investors",
                    extra={"client_id": client_id, "investors": investor_ids},
                )
            else:
                airtable.link_investors_to_client(client_id, investor_ids)

        # 5d. Lead source company resolution
        lead_source_id: str | None = None
        if lead.parsed.lead_source_company:
            lead_source_id = airtable.find_client_by_name(lead.parsed.lead_source_company)
        # If the LLM classified this as "Company" (hiring company is its own
        # source) but we couldn't resolve a separate source company, point the
        # link at the hiring client itself.
        if lead_source_id is None and lead.parsed.lead_source_type == "Company":
            lead_source_id = client_id

        # 5e. Prior-engagement override of lead_source_type
        final_source_type = lead.parsed.lead_source_type
        if airtable.has_closed_searches_for_client(lead.parsed.client):
            final_source_type = "Existing Client"
            logger.info(
                "pipeline_existing_client_override",
                extra={"client": lead.parsed.client},
            )

        # 5f. Build SearchRecord
        recipient_id = TEAM_MAP.get(lead.parsed.lead_recipient.lower())
        if recipient_id is None:
            logger.warning(
                "pipeline_unknown_recipient",
                extra={
                    "lead_recipient": lead.parsed.lead_recipient,
                    "message_id": msg_id,
                },
            )
        record = SearchRecord.from_lead(
            lead,
            client_id=client_id,
            recipient_id=recipient_id,
            lead_source_id=lead_source_id,
            gmail_message_id=msg_id,
            lead_source_type_override=final_source_type,
        )

        # 5g. Create Search (the idempotency anchor)
        if dry_run:
            search_id = "rec_DRY_SEARCH"
            logger.info(
                "pipeline_dry_would_create_search",
                extra={"record": record.model_dump(mode="json")},
            )
        else:
            search_id = airtable.create_search(record)
    except Exception as exc:  # noqa: BLE001
        logger.exception("pipeline_airtable_failed", extra={"message_id": msg_id})
        if not dry_run:
            gmail.mark_failed(msg_id, f"airtable: {exc}")
        return ProcessResult(
            message_id=msg_id,
            status="failed",
            error=f"airtable: {exc}",
            lead=lead,
            dry_run=dry_run,
        )

    # --- 6. Mark processed -------------------------------------------------
    if not dry_run:
        gmail.mark_processed(msg_id)

    logger.info(
        "pipeline_created",
        extra={
            "message_id": msg_id,
            "search_id": search_id,
            "client_id": client_id,
        },
    )
    return ProcessResult(
        message_id=msg_id,
        status="created",
        search_record_id=search_id,
        client_record_id=client_id,
        lead=lead,
        dry_run=dry_run,
    )


# ---------------------------------------------------------------------------
# Full-run aggregator
# ---------------------------------------------------------------------------


def run(
    *,
    gmail: GmailClient,
    airtable: AirtableClient,
    llm: LLMClient,
    max_leads: int = 50,
    dry_run: bool = False,
) -> RunSummary:
    """Fetch unprocessed leads and process them. Never raises on a per-lead
    failure — a single bad email shouldn't poison the whole run."""
    emails = gmail.fetch_unprocessed_leads(max_results=max_leads)
    logger.info("pipeline_run_start", extra={"count": len(emails), "dry_run": dry_run})

    results: list[ProcessResult] = []
    for email in emails:
        try:
            result = process_one_lead(
                email, gmail=gmail, airtable=airtable, llm=llm, dry_run=dry_run
            )
        except Exception as exc:  # noqa: BLE001 — defense in depth
            logger.exception("pipeline_unexpected_error", extra={"message_id": email.message_id})
            result = ProcessResult(
                message_id=email.message_id,
                status="failed",
                error=f"unexpected: {exc}",
                dry_run=dry_run,
            )
        results.append(result)

    summary = RunSummary(results=results)
    # Reserved LogRecord attrs ("created", "message", etc.) cannot appear in
    # `extra={...}` — namespace with `n_*` to dodge the stdlib check.
    logger.info(
        "pipeline_run_summary",
        extra={
            "n_total": summary.total,
            "n_created": summary.created,
            "n_skipped_duplicate": summary.skipped_duplicate,
            "n_skipped_filter": summary.skipped_filter,
            "n_skipped_no_lead": summary.skipped_no_lead,
            "n_failed": summary.failed,
        },
    )
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


@dataclass
class _CliArgs:
    once: bool
    dry_run: bool
    fixture: str | None
    max_leads: int


def _parse_args(argv: list[str]) -> _CliArgs:
    p = argparse.ArgumentParser(
        prog="python -m cole_leads.pipeline",
        description="Run the Cole leads processor pipeline once.",
    )
    p.add_argument("--once", action="store_true", default=True, help=argparse.SUPPRESS)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip all Airtable writes and Gmail label changes; still call the LLM.",
    )
    p.add_argument(
        "--fixture",
        type=str,
        default=None,
        help="Path to a fixture email file. Skips Gmail/Airtable/LLM in favor of stubs.",
    )
    p.add_argument(
        "--max-leads",
        type=int,
        default=50,
        help="Max number of leads to fetch from Gmail (ignored with --fixture).",
    )
    ns = p.parse_args(argv)
    return _CliArgs(
        once=ns.once,
        dry_run=ns.dry_run,
        fixture=ns.fixture,
        max_leads=ns.max_leads,
    )


def _load_fixture_email(path: Path) -> RawEmail:
    """Parse a `Subject:\\n...\\n\\nBody` text fixture into a RawEmail."""
    from .gmail import _parse_date  # reuse the production date parser

    raw = path.read_text()
    head, _, body = raw.partition("\n\n")
    headers: dict[str, str] = {}
    for line in head.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return RawEmail(
        message_id=f"fixture-{path.stem}",
        thread_id=f"thread-{path.stem}",
        subject=headers.get("subject", ""),
        from_addr=headers.get("from", ""),
        to_addr=headers.get("to", ""),
        cc_addr=headers.get("cc") or None,
        received_at=_parse_date(headers.get("date", "")),
        body_text=body,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.fixture:
        return _run_fixture(Path(args.fixture), dry_run=True)

    # Production: real clients from env. Imported lazily so `--fixture` runs
    # without ANTHROPIC_API_KEY / AIRTABLE_PAT / Gmail creds being present.
    from .airtable import AirtableClient
    from .gmail import GmailClient
    from .llm import LLMClient

    airtable = AirtableClient()
    gmail = GmailClient()
    llm = LLMClient()
    summary = run(
        gmail=gmail,
        airtable=airtable,
        llm=llm,
        max_leads=args.max_leads,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary.model_dump(mode="json"), indent=2))
    return 0 if summary.failed == 0 else 1


def _run_fixture(path: Path, *, dry_run: bool) -> int:
    """Stubbed end-to-end run against a fixture. No real network."""
    from .stubs import StubAirtableClient, StubGmailClient, StubLLMClient

    email = _load_fixture_email(path)
    gmail = StubGmailClient()
    airtable = StubAirtableClient()
    llm = StubLLMClient(fixture_path=path)

    result = process_one_lead(email, gmail=gmail, airtable=airtable, llm=llm, dry_run=dry_run)
    print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
    return (
        0
        if result.status
        in ("created", "skipped_duplicate", "skipped_filter", "skipped_no_lead")
        else 1
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
