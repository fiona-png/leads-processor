"""Pydantic models for parsed leads, web research output, and Airtable records."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal[
    "Sales",
    "Marketing",
    "Sales Ops",
    "BD",
    "Customer Success",
    "Sales Engineering",
    "General Management",
    "Other Category",
]

Seniority = Literal["VP", "Head", "Director", "Chief", "SVP"]

LeadSourceType = Literal[
    "Company",
    "Existing Client",
    "VC",
    "Candidate or Friend",
    "Advisors",
    "Takeover",
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
    "Bootstrapped",
    "Unknown",
]


class ParsedEmail(BaseModel):
    """Fields extracted from the forwarded-lead email itself."""

    model_config = ConfigDict(extra="forbid")

    client: str = Field(..., description="External hiring company. Never a Cole Group entity.")
    role: Role
    seniority: Seniority
    lead_recipient: str = Field(
        ..., description="First name lowercase of the Cole team member the lead was sent TO."
    )
    lead_date: date = Field(..., description="Date the original lead email was sent (not forwarded).")
    lead_source_individual: str | None = Field(
        None,
        description="External person who referred the lead. Never a Cole team member.",
    )
    lead_source_company: str | None = Field(
        None, description="External referring company (VC firm, candidate's employer, etc.)."
    )
    lead_source_type: LeadSourceType
    lead_notes: str = Field(..., description="1-2 sentence human-readable summary.")


class CompanyResearch(BaseModel):
    """Fields filled in by Claude using web_search on the client company."""

    model_config = ConfigDict(extra="forbid")

    biz_type: BizType
    biz_arr: float | None = Field(None, description="ARR in $M. null if unknown.")
    company_hq: str | None = Field(None, description="City name only, e.g. 'San Francisco'.")
    series: Series
    search_type: SearchType
    investors: list[str] = Field(default_factory=list)
    website: str | None = None


class Lead(BaseModel):
    """Combined output of the single Claude parse+research call."""

    model_config = ConfigDict(extra="forbid")

    parsed: ParsedEmail
    research: CompanyResearch


class SearchRecord(BaseModel):
    """Shape of a Search row in Airtable. Field names mirror the column names.

    The `gmail_message_id` is stored on the Search row and is the idempotency key:
    before creating a new Search we check whether one already exists with the same
    message ID.
    """

    model_config = ConfigDict(extra="forbid")

    search_name: str
    client_record_id: str
    lead_recipient_record_id: str
    investor_record_ids: list[str] = Field(default_factory=list)
    lead_source_company_record_id: str | None = None
    lead_source_individual: str | None = None
    lead_source_type: LeadSourceType
    lead_date: date
    lead_notes: str
    role: Role
    seniority: Seniority
    biz_type: BizType
    biz_arr: float | None = None
    company_hq: str | None = None
    series: Series
    search_type: SearchType
    website: str | None = None
    gmail_message_id: str
    status: Literal["Qualified"] = "Qualified"
    outcome: Literal["Open"] = "Open"
    open_flag: bool = True


class RawEmail(BaseModel):
    """Minimal Gmail-message shape used downstream. `gmail.py` builds these."""

    model_config = ConfigDict(extra="forbid")

    message_id: str
    thread_id: str
    subject: str
    from_addr: str
    to_addr: str
    received_at: date
    body_text: str
