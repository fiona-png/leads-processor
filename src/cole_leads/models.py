"""Pydantic models for parsed leads, web research output, and Airtable records."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

# Must match the Airtable select options exactly (Searches > Role / Seniority).
Role = Literal[
    "Sales",
    "Marketing",
    "Sales Ops",
    "Customer Success",
    "General Management",
    "Other Category",
]

Seniority = Literal["Chief", "SVP", "VP", "Head", "Director", "GM"]

LeadSourceType = Literal[
    "Company",
    "Existing Client",
    "VC",
    "Candidate or Friend",
]

BizType = Literal["Enterprise", "Consumer"]

SearchType = Literal["Core", "Strategic", "Franchise"]

Series = Literal[
    "Pre-Seed",
    "Seed",
    "A",
    "B",
    "C",
    "D",
    "E",
    "F",
    "G",
    "Public",
    "Private Equity",
    "Bootstrapped",
    "Unknown",
]

ProcessStatus = Literal[
    "created",
    "skipped_duplicate",
    "skipped_filter",
    "skipped_no_lead",
    "failed",
]


class ParsedEmail(BaseModel):
    """Fields extracted from the forwarded-lead email itself."""

    model_config = ConfigDict(extra="forbid")

    client: str | None = Field(
        default=None,
        description=(
            "External hiring company. Never a Cole Group entity. "
            "null when the email is not actually a lead — the pipeline then "
            "skips it without creating a Search record."
        ),
    )
    role_title: str | None = Field(
        None, description="The job title exactly as written in the email, e.g. 'VP of Sales'."
    )
    role: Role
    additional_roles: list[Role] = Field(
        default_factory=list, description="Other roles if the lead covers several (e.g. CRO/VPM)."
    )
    seniority: Seniority
    lead_recipient: str = Field(
        ..., description="First name lowercase of the Cole team member the lead was sent TO."
    )
    lead_date: date = Field(
        ..., description="Date the original lead email was sent (not forwarded)."
    )
    lead_source_individual: str | None = Field(
        None,
        description="External person who referred the lead. Never a Cole team member.",
    )
    lead_source_company: str | None = Field(
        None, description="External referring company (VC firm, candidate's employer, etc.)."
    )
    lead_source_email: str | None = Field(
        None, description="Email address of the referrer, if shown anywhere in the thread."
    )
    lead_source_type: LeadSourceType
    lead_notes: str = Field(..., description="1-2 sentence human-readable summary.")


class CompanyResearch(BaseModel):
    """Fields filled in by Claude using web_search on the client company."""

    model_config = ConfigDict(extra="forbid")

    biz_type: BizType
    biz_arr: float | None = Field(None, description="ARR/revenue in $M at the lead date.")
    arr_basis: str | None = Field(
        None, description="Where the ARR figure came from and which year it refers to."
    )
    company_hq: str | None = Field(None, description="City name only, e.g. 'San Francisco'.")
    series: Series
    last_round_date: str | None = Field(
        None, description="YYYY-MM of the last round announced BEFORE the lead date."
    )
    total_funding_m: float | None = Field(None, description="Total raised by the lead date, $M.")
    # Derived from ARR by derive.search_type_for, never trusted from the model.
    search_type: SearchType | None = None
    investors: list[str] = Field(default_factory=list)
    research_confidence: Literal["high", "medium", "low"] | None = None
    website: str | None = None


class Lead(BaseModel):
    """Combined output of the single Claude parse+research call."""

    model_config = ConfigDict(extra="forbid")

    parsed: ParsedEmail
    research: CompanyResearch


class SearchRecord(BaseModel):
    """Shape of a Search row in Airtable. Field names mirror the column names.

    `gmail_message_id` is stored on the Search row and is the idempotency key:
    before creating a new Search we check whether one already exists with the
    same message ID.
    """

    model_config = ConfigDict(extra="forbid")

    search_name: str
    client_record_id: str
    lead_recipient_record_id: str | None = None
    lead_source_company_record_id: str | None = None
    lead_source_individual: str | None = None
    lead_source_vc_investor_id: str | None = None
    lead_source_type: LeadSourceType
    needs_review: bool = False
    review_notes: str | None = None
    claude_check: str | None = None  # "Claude Lead Check" - always set by the pipeline
    lead_date: date
    lead_notes: str
    role: Role
    roles: list[Role] = Field(default_factory=list)
    seniority: Seniority
    biz_type: BizType
    biz_arr: float | None = None
    company_hq: str | None = None
    series: Series
    search_type: SearchType | None = None
    website: str | None = None
    gmail_message_id: str
    status: Literal["Qualified"] = "Qualified"
    outcome: Literal["Open"] = "Open"
    open_flag: bool = True

    @classmethod
    def from_lead(
        cls,
        lead: Lead,
        *,
        client_id: str,
        recipient_id: str | None,
        lead_source_id: str | None,
        gmail_message_id: str,
        lead_source_type_override: LeadSourceType | None = None,
        lead_source_individual_override: str | None = None,
        lead_source_vc_investor_id: str | None = None,
        review_notes: list[str] | None = None,
    ) -> SearchRecord:
        """Build a SearchRecord by combining a Lead with resolved Airtable IDs.

        `lead_source_type_override` lets the pipeline force "Existing Client" when
        the client has prior closed searches; otherwise we use the LLM's classification.
        """
        from .config import search_name as build_search_name  # local import: cycle-free

        p, r = lead.parsed, lead.research
        return cls(
            search_name=build_search_name(p.client, p.seniority, p.role, title=p.role_title),
            client_record_id=client_id,
            lead_recipient_record_id=recipient_id,
            lead_source_company_record_id=lead_source_id,
            lead_source_individual=lead_source_individual_override or p.lead_source_individual,
            lead_source_vc_investor_id=lead_source_vc_investor_id,
            lead_source_type=lead_source_type_override or p.lead_source_type,
            needs_review=bool(review_notes),
            review_notes=" | ".join(review_notes) if review_notes else None,
            lead_date=p.lead_date,
            lead_notes=p.lead_notes,
            role=p.role,
            roles=[p.role, *[x for x in p.additional_roles if x != p.role]],
            seniority=p.seniority,
            biz_type=r.biz_type,
            biz_arr=r.biz_arr,
            company_hq=r.company_hq,
            series=r.series,
            search_type=r.search_type,
            website=r.website,
            gmail_message_id=gmail_message_id,
        )


class EmailImage(BaseModel):
    """An image from the email (e.g. a screenshot of a text or LinkedIn post)."""

    model_config = ConfigDict(extra="forbid")

    media_type: str
    data_b64: str


class RawEmail(BaseModel):
    """Minimal Gmail-message shape used downstream. `gmail.py` builds these."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    thread_id: str
    subject: str
    from_addr: str
    to_addr: str
    cc_addr: str | None = None
    received_at: date
    body_text: str
    images: list[EmailImage] = Field(default_factory=list)


class ProcessResult(BaseModel):
    """Outcome of running the pipeline on a single email."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    status: ProcessStatus
    search_record_id: str | None = None
    client_record_id: str | None = None
    error: str | None = None
    lead: Lead | None = None
    dry_run: bool = False


class RunSummary(BaseModel):
    """Aggregate of `ProcessResult`s from a single `run()` pass."""

    model_config = ConfigDict(extra="forbid")

    results: list[ProcessResult] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> int:
        return len(self.results)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def created(self) -> int:
        return sum(1 for r in self.results if r.status == "created")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def skipped_duplicate(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped_duplicate")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def skipped_filter(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped_filter")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def skipped_no_lead(self) -> int:
        return sum(1 for r in self.results if r.status == "skipped_no_lead")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")
