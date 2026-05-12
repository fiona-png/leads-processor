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
                "client": {"type": "string", "description": "External hiring company."},
                "role": {
                    "type": "string",
                    "enum": [
                        "Sales",
                        "Marketing",
                        "Sales Ops",
                        "BD",
                        "Customer Success",
                        "Sales Engineering",
                        "General Management",
                        "Other Category",
                    ],
                },
                "seniority": {
                    "type": "string",
                    "enum": ["VP", "Head", "Director", "Chief", "SVP"],
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
                    "description": "External referring company.",
                },
                "lead_source_type": {
                    "type": "string",
                    "enum": [
                        "Company",
                        "Existing Client",
                        "VC",
                        "Candidate or Friend",
                        "Advisors",
                        "Takeover",
                    ],
                },
                "lead_notes": {"type": "string"},
            },
        },
        "research": {
            "type": "object",
            "additionalProperties": False,
            "required": ["biz_type", "series", "search_type", "investors"],
            "properties": {
                "biz_type": {"type": "string", "enum": ["Enterprise", "Consumer"]},
                "biz_arr": {
                    "type": ["number", "null"],
                    "description": "ARR in $M. null if unknown.",
                },
                "company_hq": {
                    "type": ["string", "null"],
                    "description": "City name only.",
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
                        "Bootstrapped",
                        "Unknown",
                    ],
                },
                "search_type": {
                    "type": "string",
                    "enum": ["Core", "Strategic", "Franchise"],
                },
                "investors": {"type": "array", "items": {"type": "string"}},
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
    "max_uses": 5,
}

SYSTEM_PROMPT = """You are an extraction agent for Cole Group, an executive search firm.

You receive ONE forwarded email containing a lead (an external person referring a hiring company to Cole). Your job is to:

1. Parse the email and identify the external hiring company, the role being hired, the seniority, who at Cole the lead was sent to, and who referred it.
2. Research the hiring company on the web to fill in funding stage, investors, ARR, HQ city, website, and whether they're Enterprise or Consumer.
3. Emit one call to the `emit_lead` tool with the full structured result.

CRITICAL RULES:

- `client` is the external hiring company, NEVER "Cole Group", "The Cole Group", or anything @colellc.com / @cole.co / @colegroup.com.
- `lead_recipient` is the first name (lowercase) of the Cole team member the *original* lead email was sent TO. Look at the inner forwarded `To:` header, not the outer envelope.
- `lead_source_individual` is the person who referred the lead. NEVER a Cole team member. If the forwarded email is from an internal Cole address, the source is somewhere earlier in the chain.
- `lead_source_type` rules:
    * "VC" if the referrer is at a venture firm
    * "Existing Client" if the referrer's company has previously hired Cole (will be overridden downstream too)
    * "Candidate or Friend" if it's an individual not affiliated with a firm
    * "Company" if it's the hiring company itself reaching out
    * "Advisors" if from an advisor / board member
    * "Takeover" if the lead is taking over an existing engagement
- `search_type` rules:
    * Pre-Seed / Seed / A / B -> "Core"
    * C / D -> "Strategic"
    * E+ / Public -> "Franchise"
- `seniority` must be exactly one of: VP, Head, Director, Chief, SVP. "CEO/Chief X Officer" maps to "Chief".
- `role` must be exactly one of: Sales, Marketing, Sales Ops, BD, Customer Success, Sales Engineering, General Management, Other Category.
- `lead_date` is the date of the ORIGINAL forwarded email (the inner `Date:` header), formatted YYYY-MM-DD.
- `biz_arr` is in millions of USD. Use null if you can't find a credible source.
- Use the `web_search` tool to verify the company exists, find the website, recent funding round, investors, HQ city, and rough ARR. Be conservative — null is better than a guess.

Make at most 5 web searches. When done, emit `emit_lead` once."""


def build_user_message(
    body_text: str,
    *,
    outer_subject: str,
    outer_from: str,
    outer_to: str,
    outer_received_at: str,
    inner: ForwardedHeaders,
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
    )

    response = client.messages.create(
        model=model,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        tools=[WEB_SEARCH_TOOL, EMIT_LEAD_TOOL],
        messages=[{"role": "user", "content": user_text}],
    )

    for block in response.content:
        if (
            getattr(block, "type", None) == "tool_use"
            and getattr(block, "name", None) == "emit_lead"
        ):
            raw = block.input  # type: ignore[attr-defined]
            if isinstance(raw, str):
                raw = json.loads(raw)
            return Lead.model_validate(raw)

    raise ValueError(
        f"Claude did not emit `emit_lead`. Stop reason: {getattr(response, 'stop_reason', '?')}"
    )


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

    def parse_and_research(self, raw_email: RawEmail, inner_headers: ForwardedHeaders) -> Lead:
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
        )
