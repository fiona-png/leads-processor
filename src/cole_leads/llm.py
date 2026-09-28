"""Single Claude call that parses the forwarded lead AND researches the client.

Design:
  - One call per lead. The original n8n flow made two — one to parse, one to
    research — which doubled cost and latency.
  - Output structure is enforced via tool_use: Claude must call the `emit_lead`
    tool whose JSON schema mirrors `models.Lead`.
  - Real-time web facts come from Anthropic's native `web_search_20250305` tool.
"""

from __future__ import annotations

import json
from typing import Any

from anthropic import Anthropic

from .derive import ROLES, SENIORITIES
from .filters import ForwardedHeaders
from .models import Lead, RawEmail

# Anthropic model: latest Sonnet is a fine default for parse+research.
DEFAULT_MODEL = "claude-sonnet-4-6"

# The JSON Schema Claude must conform to when calling `emit_lead`. Mirrors models.Lead.
_LEAD_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["parsed", "research"],
    "properties": {
        "parsed": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "client",
                "role",
                "seniority",
                "lead_recipient",
                "lead_date",
                "lead_source_type",
                "lead_notes",
            ],
            "properties": {
                "client": {
                    "type": ["string", "null"],
                    "description": (
                        "External hiring company. Set to null when the email "
                        "is not actually an executive-search lead — the "
                        "pipeline will then skip it without writing anything."
                    ),
                },
                "role_title": {
                    "type": ["string", "null"],
                    "description": "The job title exactly as written in the email, e.g. 'VP of Sales' or 'CRO / VP Sales'.",
                },
                "role": {
                    "type": "string",
                    "enum": list(ROLES),
                    "description": "Primary function of the role. See the ROLE rules in the system prompt.",
                },
                "additional_roles": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(ROLES)},
                    "description": "Other functions if the lead covers more than one role (e.g. 'CRO or VP Marketing').",
                },
                "seniority": {
                    "type": "string",
                    "enum": list(SENIORITIES),
                },
                "lead_recipient": {
                    "type": "string",
                    "description": "First name lowercase of Cole team member the lead was sent TO.",
                },
                "lead_date": {
                    "type": "string",
                    "description": "YYYY-MM-DD of the *original* (forwarded) email, not the forward itself.",
                },
                "lead_source_individual": {
                    "type": ["string", "null"],
                    "description": "External person who referred. Never a Cole team member.",
                },
                "lead_source_company": {
                    "type": ["string", "null"],
                    "description": "Organization the referrer works at (e.g. their VC firm), even if they wrote from a personal address.",
                },
                "lead_source_email": {
                    "type": ["string", "null"],
                    "description": "Email address of the referrer, exactly as shown in the thread. null if not shown.",
                },
                "lead_source_type": {
                    "type": "string",
                    "enum": [
                        "Company",
                        "Existing Client",
                        "VC",
                        "Candidate or Friend",
                    ],
                },
                "lead_notes": {"type": "string"},
            },
        },
        "research": {
            "type": "object",
            "additionalProperties": False,
            "required": ["biz_type", "series", "investors", "research_confidence"],
            "properties": {
                "biz_type": {"type": "string", "enum": ["Enterprise", "Consumer"]},
                "biz_arr": {
                    "type": ["number", "null"],
                    "description": "Annual revenue / ARR in MILLIONS of USD for the year of the lead date (12 means $12M). null if no credible figure.",
                },
                "arr_basis": {
                    "type": ["string", "null"],
                    "description": "Source and year of the ARR figure, e.g. 'Sacra est. 2025 ARR' or 'FY2025 10-K revenue'.",
                },
                "company_hq": {
                    "type": ["string", "null"],
                    "description": "HQ city only (e.g. 'San Francisco', 'New York', 'London'). No state or country.",
                },
                "series": {
                    "type": "string",
                    "enum": [
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
                    ],
                    "description": "Latest priced round announced ON OR BEFORE the lead date.",
                },
                "last_round_date": {
                    "type": ["string", "null"],
                    "description": "YYYY-MM of that round.",
                },
                "total_funding_m": {
                    "type": ["number", "null"],
                    "description": "Total equity raised by the lead date, $M.",
                },
                "investors": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Institutional investors that had invested by the lead date - firm names only, most notable first, max 8. No angels.",
                },
                "research_confidence": {
                    "type": "string",
                    "enum": ["high", "medium", "low"],
                    "description": "How sure you are of ARR and series AS OF THE LEAD DATE.",
                },
                "website": {"type": ["string", "null"]},
            },
        },
    },
}

EMIT_LEAD_TOOL: dict[str, Any] = {
    "name": "emit_lead",
    "description": "Emit the parsed lead and the researched company facts as a single structured object.",
    "input_schema": _LEAD_TOOL_SCHEMA,
}

WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 6,
}

SYSTEM_PROMPT = """You are an extraction agent for Cole Group, an executive search firm that hires go-to-market leaders (sales, marketing, customer success) for venture-backed and public tech companies.

You receive ONE email containing a lead (someone telling Cole that a company is hiring). Your job is to:

1. Parse the email: the hiring company, the role, who at Cole received it, and who referred it.
2. Research the hiring company AS IT WAS ON THE LEAD DATE: funding stage, investors, revenue, HQ city, website, Enterprise vs Consumer.
3. Call the `emit_lead` tool once with the full structured result.

PARSING RULES

- `client` is the external hiring company, NEVER "Cole Group", "The Cole Group", or anything @colellc.com / @cole.co / @colegroup.com. Use the company's common name (e.g. "Rippling", not "People Center, Inc.").
- If the email isn't actually an executive-search lead (internal chatter, a calendar reminder, a newsletter, a candidate asking for advice), set `client` to null.
- `lead_recipient` is the first name (lowercase) of the Cole team member the *original* lead email was sent TO (inner forwarded `To:` header, not the outer envelope).
- `lead_date` is the date of the ORIGINAL email (inner `Date:` header), YYYY-MM-DD.
- `lead_source_individual` is the person who referred the lead - NEVER a Cole team member.
- `lead_source_company` is the organization the referrer works at - check signatures, email domains and titles (e.g. "Talent Partner, Sequoia"). Fill it even if they wrote from a personal address.
- `lead_source_email` is the referrer's email address if it appears anywhere in the thread.
- `lead_source_type` is your best guess; it is re-checked against Cole's Airtable history downstream:
    * "VC" if the referrer works at a venture / growth / PE firm (talent partners, platform teams, partners)
    * "Existing Client" if the hiring company or the referrer's company has hired Cole before
    * "Company" if the hiring company itself reached out (founder, exec or recruiter at that company)
    * "Candidate or Friend" only for an individual not acting for any of the above
- If a "Known Cole relationships" section is provided, treat it as ground truth about who those people are.

ROLE RULES (`role_title`, `role`, `additional_roles`, `seniority`)

- Copy the title as written into `role_title`.
- `role` is the function:
    * Sales: CRO, Chief Revenue Officer, VP/Head of Sales, revenue leader, GTM leader, business development, partnerships, sales engineering / solutions
    * Marketing: CMO, VP/Head of Marketing, growth, demand gen, product marketing (PMM), brand, communications
    * Customer Success: Chief Customer Officer, VP/Head of Customer Success, customer experience, support, post-sales
    * Sales Ops: revenue operations, sales operations, GTM operations
    * General Management: President, COO, GM, CEO
    * Other Category: anything else (product, engineering, finance, people...)
- If the lead covers more than one role ("CRO or VP Marketing", "VPS/VPM"), put the main one in `role` and the rest in `additional_roles`.
- `seniority`: Chief for any C-level title or President; SVP for SVP/EVP/GVP; VP for Vice President; Head for "Head of"/"leader"; Director; GM for General Manager. Pick the level of the TITLE, not the person.

RESEARCH RULES - everything is AS OF THE LEAD DATE, not today

- Leads can be months old and companies raise and grow quickly. Use news and sources dated on or before the lead date. If the latest round you find was announced AFTER the lead date, use the round before it.
- `series`: the latest priced equity round announced on or before the lead date. "Public" only if listed by the lead date; "Private Equity" if it had been taken private or is PE-owned (e.g. a Thoma Bravo / Vista / STG buyout). Use "Unknown" rather than guessing.
- `biz_arr`: annual recurring revenue (or annual revenue for non-SaaS / public companies) for the lead-date year, in MILLIONS of USD. NEVER use funding raised, valuation, or headcount-based guesses, and never use a figure from a later year. For public companies use trailing-twelve-month revenue at the lead date. Prefer reported numbers (company announcements, filings, Sacra, The Information, Forbes Cloud 100) over data-aggregator estimates. Put the source and year in `arr_basis`. null is better than a guess.
- `total_funding_m`: total equity raised by the lead date, in $M.
- `investors`: institutional investors (VC / growth / PE / corporate venture) that had invested by the lead date, most notable first, max 8. Use the firm's standard name ("Sequoia Capital", "Andreessen Horowitz", "Index Ventures"). No individual angels.
- `company_hq`: HQ city only.
- `research_confidence`: "low" if you couldn't find dated sources for series or revenue.
- Do NOT decide Core / Strategic / Franchise - that is computed from ARR downstream.

Make at most 6 web searches. When done, emit `emit_lead` once."""


def build_user_message(
    body_text: str,
    *,
    outer_subject: str,
    outer_from: str,
    outer_to: str,
    outer_received_at: str,
    inner: ForwardedHeaders,
    relationship_hints: list[str] | None = None,
) -> str:
    """Compose the user-message text shown to Claude.

    Both the raw body and the pre-extracted inner headers are passed in so Claude
    doesn't have to guess where the original message ends.
    """
    parts = [
        "# Outer email (the forward as it arrived in leads@)",
        f"Subject: {outer_subject}",
        f"From: {outer_from}",
        f"To: {outer_to}",
        f"Received: {outer_received_at}",
        "",
        "# Inner forwarded headers (pre-extracted; trust these over the body)",
        f"From: {inner.from_ or '(not found)'}",
        f"Date: {inner.date or '(not found)'}",
        f"Subject: {inner.subject or '(not found)'}",
        f"To: {inner.to or '(not found)'}",
        "",
        "# Full body",
        body_text,
    ]
    lead_date_hint = inner.date or outer_received_at
    parts += [
        "",
        f"# Research everything as of the lead date: {lead_date_hint}",
    ]
    if relationship_hints:
        parts += [
            "",
            "# Known Cole relationships (from Cole's Airtable history)",
            *relationship_hints,
        ]
    return "\n".join(parts)


def extract_lead(
    *,
    body_text: str,
    outer_subject: str,
    outer_from: str,
    outer_to: str,
    outer_received_at: str,
    inner: ForwardedHeaders,
    client: Anthropic,
    model: str = DEFAULT_MODEL,
    relationship_hints: list[str] | None = None,
    extra_tools: list[dict[str, Any]] | None = None,
    extra_request: dict[str, Any] | None = None,
) -> Lead:
    """Run the parse+research call and return a validated `Lead`.

    Raises if Claude doesn't emit a well-formed `emit_lead` tool call.
    """
    user_text = build_user_message(
        body_text,
        outer_subject=outer_subject,
        outer_from=outer_from,
        outer_to=outer_to,
        outer_received_at=outer_received_at,
        inner=inner,
        relationship_hints=relationship_hints,
    )

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
    tools = [WEB_SEARCH_TOOL, EMIT_LEAD_TOOL, *(extra_tools or [])]
    extra: dict[str, Any] = dict(extra_request or {})

    response = None
    for attempt in range(4):
        kwargs: dict[str, Any] = dict(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )
        kwargs.update(extra)
        if attempt == 3:
            # Last try: research is done, force the structured answer.
            kwargs["tool_choice"] = {"type": "tool", "name": "emit_lead"}
        response = client.messages.create(**kwargs)

        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == "emit_lead"
            ):
                raw = block.input  # type: ignore[attr-defined]
                if isinstance(raw, str):
                    raw = json.loads(raw)
                return Lead.model_validate(raw)

        stop = getattr(response, "stop_reason", None)
        # Server-side web search can pause a long turn; continue it. Otherwise
        # nudge the model to emit its answer.
        messages = [*messages, {"role": "assistant", "content": response.content}]
        if stop != "pause_turn":
            messages.append(
                {"role": "user", "content": "Now call emit_lead with your best answer."}
            )

    raise ValueError(
        f"Claude did not emit `emit_lead`. Stop reason: {getattr(response, 'stop_reason', '?')}"
    )


ROLO_PROMPT_ADDENDUM = """

ROLO (Cole's own company database) is connected as MCP tools. Check it FIRST:
- Find the company with company_search, then call get_revenue_details for the lead-date year - use that year's revenue for `biz_arr` (convert to $M) and say "Rolo <year>" in `arr_basis`.
- Rolo's stage, investors and funding fields are CURRENT, not as of the lead date: only use them if the last round is dated on or before the lead date; otherwise confirm the earlier round on the web."""


def rolo_request_options() -> dict[str, Any] | None:
    """If ROLO_MCP_URL (+ ROLO_MCP_TOKEN) is set, let the model query Rolo via
    Anthropic's MCP connector. Off by default so nothing changes until the
    secrets are added to the GitHub Action."""
    import os

    url = os.environ.get("ROLO_MCP_URL")
    if not url:
        return None
    server: dict[str, Any] = {"type": "url", "url": url, "name": "rolo"}
    token = os.environ.get("ROLO_MCP_TOKEN")
    if token:
        server["authorization_token"] = token
    return {
        "extra_headers": {"anthropic-beta": "mcp-client-2025-04-04"},
        "extra_body": {"mcp_servers": [server]},
        "system": SYSTEM_PROMPT + ROLO_PROMPT_ADDENDUM,
    }


class LLMClient:
    """Thin DI wrapper around `extract_lead` for the pipeline.

    Production: construct with no args; reads `ANTHROPIC_API_KEY` from env.
    Tests: pass `anthropic=<stub>` to bypass the real SDK.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        anthropic: Anthropic | None = None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        if anthropic is None:
            if api_key is None:
                from .config import (
                    get_settings,  # local import keeps config side effects out of imports
                )

                api_key = get_settings().anthropic_api_key
            anthropic = Anthropic(api_key=api_key)
        self._client = anthropic
        self._model = model
        self._extra_request = rolo_request_options()

    def parse_and_research(
        self,
        raw_email: RawEmail,
        inner_headers: ForwardedHeaders,
        relationship_hints: list[str] | None = None,
    ) -> Lead:
        """Run one Claude call to parse the forward and research the client company."""
        return extract_lead(
            body_text=raw_email.body_text,
            outer_subject=raw_email.subject,
            outer_from=raw_email.from_addr,
            outer_to=raw_email.to_addr,
            outer_received_at=raw_email.received_at.isoformat(),
            inner=inner_headers,
            client=self._client,
            model=self._model,
            relationship_hints=relationship_hints,
            extra_request=self._extra_request,
        )
