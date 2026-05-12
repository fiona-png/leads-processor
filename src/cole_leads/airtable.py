"""Typed Airtable client. STUB — implement in a later session.

Notes for the implementer:
  - Use exact-match lookups via filterByFormula like
    `LOWER({Client Name})=LOWER("Acme")`, never substring matches.
  - Idempotency: before creating a Search, look up by `Gmail Message ID` field
    (must be added on the Searches table) and skip if found.
  - The "Closes" view (config.AIRTABLE_CLOSES_VIEW) on the Searches table is
    used to detect prior closed engagements with the client — if any exist,
    override `lead_source_type` to "Existing Client".
"""

from __future__ import annotations

from .models import SearchRecord


def find_or_create_client(name: str) -> str:
    raise NotImplementedError


def find_or_create_investor(name: str, *, client_record_id: str) -> str:
    raise NotImplementedError


def find_client_by_exact_name(name: str) -> str | None:
    raise NotImplementedError


def client_has_closed_searches(client_record_id: str) -> bool:
    raise NotImplementedError


def search_exists_for_message(gmail_message_id: str) -> bool:
    raise NotImplementedError


def create_search(record: SearchRecord) -> str:
    raise NotImplementedError
