"""Tests for the LLM call wrapper. Uses a stub Anthropic client — no network."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from cole_leads.filters import ForwardedHeaders
from cole_leads.llm import EMIT_LEAD_TOOL, build_user_message, extract_lead


@dataclass
class _StubBlock:
    type: str
    name: str | None = None
    input: Any = None
    text: str | None = None


@dataclass
class _StubResponse:
    content: list[_StubBlock]
    stop_reason: str = "tool_use"


class _StubMessages:
    def __init__(self, response: _StubResponse, calls: list[dict]):
        self._response = response
        self._calls = calls

    def create(self, **kwargs):
        self._calls.append(kwargs)
        return self._response


class _StubAnthropic:
    def __init__(self, response: _StubResponse):
        self.calls: list[dict] = []
        self.messages = _StubMessages(response, self.calls)


_VALID_INPUT = {
    "parsed": {
        "client": "Acme Robotics",
        "role": "Sales",
        "seniority": "Chief",
        "lead_recipient": "matt",
        "lead_date": "2026-05-06",
        "lead_source_individual": "Jane Investor",
        "lead_source_company": "Accel",
        "lead_source_type": "VC",
        "lead_notes": "Series B SF robotics; first CRO.",
    },
    "research": {
        "biz_type": "Enterprise",
        "biz_arr": 12.0,
        "company_hq": "San Francisco",
        "series": "B",
        "search_type": "Core",
        "investors": ["Accel"],
        "website": "https://acme.example",
    },
}


def test_extract_lead_from_tool_use_block():
    response = _StubResponse(
        content=[
            _StubBlock(type="text", text="thinking..."),
            _StubBlock(type="tool_use", name="emit_lead", input=_VALID_INPUT),
        ]
    )
    client = _StubAnthropic(response)
    lead = extract_lead(
        body_text="dummy",
        outer_subject="Fwd: Intro",
        outer_from="matt@colellc.com",
        outer_to="leads@colellc.com",
        outer_received_at="2026-05-06",
        inner=ForwardedHeaders(
            from_="Jane <jane@accel.com>",
            date="Tue, May 6, 2026",
            subject="Intro",
            to="Matt <matt@colellc.com>",
        ),
        client=client,
    )
    assert lead.parsed.client == "Acme Robotics"
    assert lead.research.biz_arr == 12.0
    assert lead.research.investors == ["Accel"]

    # Verify the call was constructed correctly.
    call = client.calls[0]
    assert any(t == EMIT_LEAD_TOOL or t.get("name") == "emit_lead" for t in call["tools"])
    assert any(t.get("type") == "web_search_20250305" for t in call["tools"])
    assert "Cole Group" in call["system"]


def test_extract_lead_raises_when_no_tool_use():
    response = _StubResponse(
        content=[_StubBlock(type="text", text="I'm not sure what to do.")],
        stop_reason="end_turn",
    )
    client = _StubAnthropic(response)
    with pytest.raises(ValueError, match="emit_lead"):
        extract_lead(
            body_text="x",
            outer_subject="x",
            outer_from="x",
            outer_to="x",
            outer_received_at="x",
            inner=ForwardedHeaders(None, None, None, None),
            client=client,
        )


def test_extract_lead_handles_string_input():
    """Some SDKs deliver tool input as a JSON string; handle that path too."""
    import json

    response = _StubResponse(
        content=[_StubBlock(type="tool_use", name="emit_lead", input=json.dumps(_VALID_INPUT))]
    )
    client = _StubAnthropic(response)
    lead = extract_lead(
        body_text="x",
        outer_subject="x",
        outer_from="x",
        outer_to="x",
        outer_received_at="x",
        inner=ForwardedHeaders(None, None, None, None),
        client=client,
    )
    assert lead.parsed.client == "Acme Robotics"


def test_build_user_message_includes_inner_headers():
    msg = build_user_message(
        "body text",
        outer_subject="Fwd: X",
        outer_from="matt@colellc.com",
        outer_to="leads@colellc.com",
        outer_received_at="2026-05-06",
        inner=ForwardedHeaders(
            from_="Jane <jane@accel.com>",
            date="Tue, May 6, 2026",
            subject="Intro",
            to="Matt <matt@colellc.com>",
        ),
    )
    assert "Jane <jane@accel.com>" in msg
    assert "leads@colellc.com" in msg
    assert "body text" in msg


def test_images_sent_as_image_blocks():
    from unittest.mock import MagicMock

    from cole_leads.filters import ForwardedHeaders
    from cole_leads.llm import extract_lead
    from cole_leads.models import EmailImage

    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("stop")
    try:
        extract_lead(
            body_text="",
            outer_subject="Fiona will you attach",
            outer_from="matt@colegroup.com",
            outer_to="leads@colegroup.com",
            outer_received_at="2026-09-30",
            inner=ForwardedHeaders(None, None, None, None),
            client=client,
            images=[EmailImage(media_type="image/jpeg", data_b64="AAAA")],
        )
    except RuntimeError:
        pass
    content = client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[-1]["type"] == "text"
