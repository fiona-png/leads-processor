"""Gmail fetch + label. STUB — implement in a later session."""

from __future__ import annotations

from collections.abc import Iterator

from .models import RawEmail


def fetch_unread_leads(*, user: str) -> Iterator[RawEmail]:
    """Yield unread messages in the leads@ inbox, oldest first.

    To implement: use google-api-python-client with an OAuth-refresh-token flow,
    query `is:unread to:leads@cole(group|llc).com OR to:leads@cole.co -subject:Re:`,
    and yield `RawEmail` objects.
    """
    raise NotImplementedError("gmail.fetch_unread_leads is not yet implemented")


def mark_processed(message_id: str) -> None:
    """Apply a 'cole-leads/processed' label to the given message.

    To implement after the Airtable write succeeds, so a crash mid-pipeline
    leaves the message unread and reprocessable.
    """
    raise NotImplementedError("gmail.mark_processed is not yet implemented")
