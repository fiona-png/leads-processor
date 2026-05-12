"""End-to-end integration test against real Anthropic + Airtable.

NOT run by default — see `pyproject.toml`'s `addopts = "-m 'not integration'"`.
Run explicitly with:

    pytest -m integration

Cost: ~$0.05 of Anthropic credit per invocation, plus three Airtable writes
(client + search + investor) that are deleted in a `finally` block. Should
finish in <30 seconds.

Safety:
  * Client name is mutated to `TestCo Integration <timestamp>` so the test
    can't accidentally write under a real client name.
  * Gmail message_id is timestamp-based, so re-runs never dedupe-skip.
  * Cleanup deletes the Search row AND the Client row. If either delete
    fails, the test emits a CRITICAL log line with the orphan record IDs
    so a human can clean up manually in Airtable.
  * Does NOT touch Gmail labels — there's no real message to mark.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest
from dotenv import load_dotenv

from cole_leads.airtable import AirtableClient, AirtableError
from cole_leads.config import AIRTABLE_BASE_ID, AIRTABLE_SEARCHES_TABLE
from cole_leads.llm import LLMClient
from cole_leads.pipeline import _load_fixture_email, process_one_lead
from cole_leads.stubs import StubGmailClient

logger = logging.getLogger(__name__)

FIXTURE = Path(__file__).parent / "fixtures" / "forwarded_vpm_lead.txt"


def _ensure_env() -> None:
    """Load `.env` from the repo root and confirm the required keys are set."""
    repo_root = Path(__file__).parent.parent
    load_dotenv(repo_root / ".env")
    missing = [k for k in ("ANTHROPIC_API_KEY", "AIRTABLE_PAT") if not os.environ.get(k)]
    if missing:
        pytest.skip(f"Missing env vars: {', '.join(missing)}")


@pytest.mark.integration
def test_one_lead_end_to_end_with_cleanup(capsys):
    _ensure_env()

    timestamp = time.strftime("%Y%m%d-%H%M%S")
    fake_client_name = f"TestCo Integration {timestamp}"
    fake_message_id = f"integration-{timestamp}"

    # Load the fixture and rewrite "Helios Energy" -> fake client name so the
    # LLM emits the fake name into Airtable. This keeps the test record
    # trivially identifiable and orthogonal to any real client.
    email = _load_fixture_email(FIXTURE)
    email = email.model_copy(
        update={
            "message_id": fake_message_id,
            "thread_id": f"thread-{fake_message_id}",
            "body_text": email.body_text.replace("Helios Energy", fake_client_name),
            "subject": email.subject.replace("Helios Energy", fake_client_name),
        }
    )

    # Real clients. Gmail is the only stub because there's no message to label.
    airtable = AirtableClient()
    llm = LLMClient()
    gmail = StubGmailClient()

    result = None
    created_search_id: str | None = None
    created_client_id: str | None = None

    try:
        result = process_one_lead(email, gmail=gmail, airtable=airtable, llm=llm, dry_run=False)

        # Capture IDs immediately so a later assertion failure still leads to
        # cleanup.
        created_search_id = result.search_record_id
        created_client_id = result.client_record_id

        assert result.status == "created", (
            f"unexpected status: {result.status} (error={result.error})"
        )
        assert created_search_id, "expected a Search record ID"
        assert created_client_id, "expected a Client record ID"
        assert result.lead is not None
        # The LLM should have echoed the fake client name back into the parse.
        assert result.lead.parsed.client == fake_client_name, (
            f"LLM returned '{result.lead.parsed.client}' instead of "
            f"'{fake_client_name}' — Anthropic may have hallucinated."
        )

        # Print the Airtable URL so a human can eyeball the row during the
        # few seconds before cleanup. capsys lets pytest surface it on
        # success too when invoked with `-s`.
        url = (
            f"https://airtable.com/{AIRTABLE_BASE_ID}/{AIRTABLE_SEARCHES_TABLE}/{created_search_id}"
        )
        print(f"\n[integration] Created Search: {url}")
        print(f"[integration] Client: {created_client_id}  Search: {created_search_id}")

    finally:
        # Clean up in reverse order. Search first (it links to Client and
        # Investors); deleting it doesn't cascade, so the Client delete is a
        # separate call. We DON'T delete the Investor — `find_or_create_investor`
        # may have linked to a real existing investor row.
        if created_search_id:
            try:
                airtable.delete_search(created_search_id)
            except AirtableError as e:
                logger.critical(
                    "INTEGRATION CLEANUP FAILED: could not delete Search %s. "
                    "Delete it manually in Airtable. Error: %s",
                    created_search_id,
                    e,
                )
        if created_client_id:
            try:
                airtable.delete_client(created_client_id)
            except AirtableError as e:
                logger.critical(
                    "INTEGRATION CLEANUP FAILED: could not delete Client %s. "
                    "Delete it manually in Airtable. Error: %s",
                    created_client_id,
                    e,
                )
