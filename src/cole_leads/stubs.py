"""In-memory stub clients for `python -m cole_leads.pipeline --fixture ...`.

These are NOT used in production and NOT used in the pytest suite (tests use
unittest.mock). Their only purpose is to let an operator run the full pipeline
end-to-end against a fixture email without touching Anthropic, Gmail, or
Airtable. They print/log what would have happened.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from .filters import ForwardedHeaders
from .logging import get_logger
from .models import CompanyResearch, Lead, ParsedEmail, RawEmail, SearchRecord

logger = get_logger(__name__)


class StubGmailClient:
    """Gmail surface for fixture runs. All methods are no-ops with logging."""

    def fetch_unprocessed_leads(self, max_results: int = 50) -> list[RawEmail]:
        return []

    def mark_processed(self, message_id: str) -> None:
        logger.info("stub_gmail_mark_processed", extra={"message_id": message_id})

    def mark_failed(self, message_id: str, error: str) -> None:
        logger.info("stub_gmail_mark_failed", extra={"message_id": message_id, "error": error})


class StubAirtableClient:
    """Airtable surface for fixture runs. Returns deterministic placeholder IDs."""

    def search_exists_for_message_id(self, message_id: str) -> bool:
        return False

    def find_client_by_name(self, name: str) -> str | None:
        return None

    def upsert_client(self, name: str, research: CompanyResearch) -> str:
        return f"recSTUB_CLIENT_{_slug(name)}"

    def find_or_create_investor(self, name: str) -> str:
        return f"recSTUB_INV_{_slug(name)}"

    def link_investors_to_client(self, client_id: str, investor_ids: list[str]) -> None:
        logger.info(
            "stub_airtable_link_investors",
            extra={"client_id": client_id, "investors": investor_ids},
        )

    def has_closed_searches_for_client(self, client_name: str) -> bool:
        return False

    def create_search(self, record: SearchRecord) -> str:
        return "recSTUB_SEARCH"


# ---------------------------------------------------------------------------
# Stub LLM: hardcoded per-fixture canned responses
# ---------------------------------------------------------------------------


def _vpm_lead() -> Lead:
    return Lead(
        parsed=ParsedEmail(
            client="Helios Energy",
            role="Marketing",
            seniority="VP",
            lead_recipient="matt",
            lead_date=date(2026, 5, 11),
            lead_source_individual="Maria Operator",
            lead_source_company="Brexbros Ventures",
            lead_source_type="VC",
            lead_notes=(
                "Series A Boulder utility-grid SaaS, ~$4M ARR. CEO Eddie Wong "
                "wants to hire their first VP Marketing."
            ),
        ),
        research=CompanyResearch(
            biz_type="Enterprise",
            biz_arr=4.0,
            company_hq="Boulder",
            series="A",
            search_type="Core",
            investors=["Brexbros Ventures"],
            website="https://helios.energy",
        ),
    )


_CANNED: dict[str, Any] = {
    "forwarded_vpm_lead": _vpm_lead,
}


class StubLLMClient:
    """LLM stand-in for fixture runs. Looks up a canned Lead by fixture filename."""

    def __init__(self, *, fixture_path: Path) -> None:
        self._stem = fixture_path.stem

    def parse_and_research(self, raw_email: RawEmail, inner: ForwardedHeaders) -> Lead:
        builder = _CANNED.get(self._stem)
        if builder is None:
            raise ValueError(
                f"No canned LLM response registered for fixture '{self._stem}'. "
                f"Add one to cole_leads.stubs._CANNED."
            )
        return builder()


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s)[:32]
