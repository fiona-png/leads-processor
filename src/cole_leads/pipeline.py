"""End-to-end pipeline for one lead. STUB — implement in a later session.

Idempotency contract:
  1. Read message.
  2. Short-circuit if `airtable.search_exists_for_message(message_id)` is true.
  3. Filter (forward/reply rules).
  4. Parse forwarded headers.
  5. Single Claude call (llm.extract_lead) -> Lead.
  6. Resolve client, investors, lead recipient, lead source company.
  7. If client has prior closes, override `lead_source_type` to "Existing Client".
  8. Build SearchRecord (includes gmail_message_id).
  9. Create Search.
 10. Label message as processed in Gmail.
"""

from __future__ import annotations

from .models import RawEmail


def process(email: RawEmail) -> str | None:
    """Process one email. Returns the Airtable Search record ID, or None if skipped."""
    raise NotImplementedError
