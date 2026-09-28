"""Tests for the Airtable REST client. No network — httpx.MockTransport everywhere."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest

from cole_leads.airtable import (
    BASE_URL,
    CLIENT_BIZ_ARR,
    CLIENT_BIZ_TYPE,
    CLIENT_INVESTORS,
    CLIENT_NAME,
    CLIENT_WEBSITE,
    INVESTOR_NAME,
    MAX_RETRIES,
    SEARCH_BIZ_ARR,
    SEARCH_BIZ_TYPE,
    SEARCH_CLIENT,
    SEARCH_COMPANY_HQ,
    SEARCH_GMAIL_MESSAGE_ID,
    SEARCH_LEAD_DATE,
    SEARCH_LEAD_NOTES,
    SEARCH_LEAD_RECIPIENT,
    SEARCH_LEAD_SOURCE,
    SEARCH_LEAD_SOURCE_INDIVIDUAL,
    SEARCH_LEAD_SOURCE_TYPE,
    SEARCH_NAME,
    SEARCH_OPEN,
    SEARCH_OUTCOME,
    SEARCH_ROLE,
    SEARCH_SEARCH_TYPE,
    SEARCH_SENIORITY,
    SEARCH_SERIES,
    SEARCH_STATUS,
    AirtableClient,
    AirtableError,
)
from cole_leads.config import (
    AIRTABLE_CLIENTS_TABLE,
    AIRTABLE_CLOSES_VIEW,
    AIRTABLE_INVESTORS_TABLE,
    AIRTABLE_SEARCHES_TABLE,
)
from cole_leads.models import CompanyResearch, SearchRecord

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler) -> AirtableClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": "Bearer test-pat"},
        transport=transport,
    )
    return AirtableClient(http=http)


def make_recording_handler(
    responses: list[httpx.Response],
) -> tuple[Handler, list[httpx.Request]]:
    """Build a handler that walks `responses` in order, recording each request."""
    calls: list[httpx.Request] = []
    iter_responses = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        try:
            return next(iter_responses)
        except StopIteration as e:
            raise AssertionError(f"Unexpected extra request: {request.method} {request.url}") from e

    return handler, calls


# ----- Builders ---------------------------------------------------------------


def _research(**overrides: Any) -> CompanyResearch:
    base: dict[str, Any] = dict(
        biz_type="Enterprise",
        biz_arr=12.0,
        company_hq="San Francisco",
        series="B",
        search_type="Core",
        investors=["Accel"],
        website="https://acme.example",
    )
    base.update(overrides)
    return CompanyResearch(**base)


def _search_record(**overrides: Any) -> SearchRecord:
    base: dict[str, Any] = dict(
        search_name="Acme Robotics CRO",
        client_record_id="recCLIENT",
        lead_recipient_record_id="recMATT",
        lead_source_type="VC",
        lead_date=date(2026, 5, 6),
        lead_notes="Series B SF robotics; first CRO.",
        role="Sales",
        seniority="Chief",
        biz_type="Enterprise",
        series="B",
        search_type="Core",
        gmail_message_id="abc123",
        lead_source_individual="Jane Investor",
        lead_source_company_record_id="recACCEL",
        biz_arr=12.0,
        company_hq="San Francisco",
        website="https://acme.example",
    )
    base.update(overrides)
    return SearchRecord(**base)


# ============================================================================
# search_exists_for_message_id
# ============================================================================


class TestSearchExistsForMessageId:
    def test_true_when_records_nonempty(self):
        def h(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"records": [{"id": "recX"}]})

        with make_client(h) as at:
            assert at.search_exists_for_message_id("abc123") is True

    def test_false_when_records_empty(self):
        def h(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"records": []})

        with make_client(h) as at:
            assert at.search_exists_for_message_id("abc123") is False

    def test_request_filters_by_message_id_and_hits_searches_table(self):
        seen: dict[str, Any] = {}

        def h(request: httpx.Request) -> httpx.Response:
            seen["url"] = request.url
            return httpx.Response(200, json={"records": []})

        with make_client(h) as at:
            at.search_exists_for_message_id("msg-123")

        url = seen["url"]
        assert AIRTABLE_SEARCHES_TABLE in str(url)
        formula = url.params["filterByFormula"]
        assert SEARCH_GMAIL_MESSAGE_ID in formula
        assert "msg-123" in formula
        assert url.params["maxRecords"] == "1"


# ============================================================================
# find_client_by_name
# ============================================================================


class TestFindClientByName:
    def test_returns_id_on_hit(self):
        def h(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"records": [{"id": "recCLIENT1"}]})

        with make_client(h) as at:
            assert at.find_client_by_name("Acme") == "recCLIENT1"

    def test_returns_none_on_miss(self):
        def h(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"records": []})

        with make_client(h) as at:
            assert at.find_client_by_name("Acme") is None

    def test_formula_lowers_both_sides_and_hits_clients_table(self):
        seen: dict[str, Any] = {}

        def h(request: httpx.Request) -> httpx.Response:
            seen["url"] = request.url
            return httpx.Response(200, json={"records": []})

        with make_client(h) as at:
            at.find_client_by_name("Acme Robotics")

        url = seen["url"]
        assert AIRTABLE_CLIENTS_TABLE in str(url)
        formula = url.params["filterByFormula"]
        assert formula == f'LOWER({{{CLIENT_NAME}}})=LOWER("Acme Robotics")'
        # Must NOT use SEARCH (which is what made Cole -> Coleman match in n8n).
        assert "SEARCH(" not in formula
        assert "FIND(" not in formula

    def test_formula_escapes_embedded_quotes(self):
        seen: dict[str, Any] = {}

        def h(request: httpx.Request) -> httpx.Response:
            seen["url"] = request.url
            return httpx.Response(200, json={"records": []})

        with make_client(h) as at:
            at.find_client_by_name('Foo "Bar" Inc')

        formula = seen["url"].params["filterByFormula"]
        assert r"\"Bar\"" in formula


# ============================================================================
# create_client
# ============================================================================


class TestCreateClient:
    def test_posts_correct_body(self):
        seen: dict[str, Any] = {}

        def h(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            seen["json"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "recNEW", "fields": {}})

        with make_client(h) as at:
            new_id = at.create_client("Acme Robotics", _research())

        assert new_id == "recNEW"
        assert seen["method"] == "POST"
        assert AIRTABLE_CLIENTS_TABLE in seen["path"]
        body = seen["json"]
        assert body["typecast"] is True
        assert body["fields"][CLIENT_NAME] == "Acme Robotics"
        assert body["fields"][CLIENT_BIZ_TYPE] == ["Enterprise"]
        assert body["fields"][CLIENT_BIZ_ARR] == 12.0
        assert body["fields"][CLIENT_WEBSITE] == "https://acme.example"

    def test_omits_optional_nulls(self):
        seen: dict[str, Any] = {}

        def h(request: httpx.Request) -> httpx.Response:
            seen["json"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "recNEW", "fields": {}})

        research = _research(biz_arr=None, website=None)
        with make_client(h) as at:
            at.create_client("Acme", research)

        fields = seen["json"]["fields"]
        assert CLIENT_BIZ_ARR not in fields
        assert CLIENT_WEBSITE not in fields


# ============================================================================
# upsert_client
# ============================================================================


class TestUpsertClient:
    def test_returns_existing_id_without_creating(self):
        responses = [httpx.Response(200, json={"records": [{"id": "recEXISTS"}]})]
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            cid = at.upsert_client("Acme", _research())

        assert cid == "recEXISTS"
        assert len(calls) == 1
        assert calls[0].method == "GET"

    def test_creates_on_miss(self):
        responses = [
            httpx.Response(200, json={"records": []}),
            httpx.Response(200, json={"id": "recNEW", "fields": {}}),
        ]
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            cid = at.upsert_client("Acme", _research())

        assert cid == "recNEW"
        assert [c.method for c in calls] == ["GET", "POST"]


# ============================================================================
# find_or_create_investor
# ============================================================================


class TestFindOrCreateInvestor:
    def test_returns_existing(self):
        responses = [httpx.Response(200, json={"records": [{"id": "recACCEL"}]})]
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            inv = at.find_or_create_investor("Accel")

        assert inv == "recACCEL"
        assert len(calls) == 1

    def test_creates_on_miss_with_typecast(self):
        responses = [
            httpx.Response(200, json={"records": []}),
            httpx.Response(200, json={"id": "recACCEL", "fields": {}}),
        ]
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            inv = at.find_or_create_investor("Accel")

        assert inv == "recACCEL"
        assert calls[1].method == "POST"
        body = json.loads(calls[1].content)
        assert body["typecast"] is True
        assert body["fields"] == {INVESTOR_NAME: "Accel"}
        assert AIRTABLE_INVESTORS_TABLE in calls[1].url.path


# ============================================================================
# link_investors_to_client
# ============================================================================


class TestLinkInvestorsToClient:
    def test_merges_with_existing(self):
        responses = [
            # GET current client record returns one existing investor.
            httpx.Response(
                200,
                json={
                    "id": "recCLIENT",
                    "fields": {CLIENT_INVESTORS: ["recEXISTING"]},
                },
            ),
            # PATCH succeeds.
            httpx.Response(200, json={"id": "recCLIENT", "fields": {}}),
        ]
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            at.link_investors_to_client("recCLIENT", ["recNEW1", "recNEW2"])

        assert [c.method for c in calls] == ["GET", "PATCH"]
        body = json.loads(calls[1].content)
        merged = body["fields"][CLIENT_INVESTORS]
        assert merged == ["recEXISTING", "recNEW1", "recNEW2"]
        assert body["typecast"] is True

    def test_dedupes_against_existing(self):
        responses = [
            httpx.Response(
                200,
                json={
                    "id": "recCLIENT",
                    "fields": {CLIENT_INVESTORS: ["recA", "recB"]},
                },
            ),
            httpx.Response(200, json={"id": "recCLIENT", "fields": {}}),
        ]
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            at.link_investors_to_client("recCLIENT", ["recB", "recC"])

        body = json.loads(calls[1].content)
        assert body["fields"][CLIENT_INVESTORS] == ["recA", "recB", "recC"]

    def test_skips_patch_when_nothing_new(self):
        responses = [
            httpx.Response(
                200,
                json={
                    "id": "recCLIENT",
                    "fields": {CLIENT_INVESTORS: ["recA", "recB"]},
                },
            ),
        ]
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            at.link_investors_to_client("recCLIENT", ["recA", "recB"])

        assert [c.method for c in calls] == ["GET"]  # no PATCH issued

    def test_noop_when_empty_input(self):
        responses: list[httpx.Response] = []
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            at.link_investors_to_client("recCLIENT", [])

        assert calls == []  # no requests at all


# ============================================================================
# has_closed_searches_for_client
# ============================================================================


class TestHasClosedSearchesForClient:
    def test_uses_closes_view_and_anchored_find(self):
        seen: dict[str, Any] = {}

        def h(request: httpx.Request) -> httpx.Response:
            seen["url"] = request.url
            return httpx.Response(200, json={"records": []})

        with make_client(h) as at:
            at.has_closed_searches_for_client("Acme")

        url = seen["url"]
        assert AIRTABLE_SEARCHES_TABLE in str(url)
        assert url.params["view"] == AIRTABLE_CLOSES_VIEW
        formula = url.params["filterByFormula"]
        assert formula == f'FIND(LOWER("Acme"), LOWER({{{SEARCH_NAME}}}))=1'
        # Anchored at position 1 — "Coleman" must NOT match "Cole".
        assert formula.endswith("=1")

    def test_true_when_nonempty(self):
        def h(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"records": [{"id": "recOLD"}]})

        with make_client(h) as at:
            assert at.has_closed_searches_for_client("Acme") is True

    def test_false_when_empty(self):
        def h(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"records": []})

        with make_client(h) as at:
            assert at.has_closed_searches_for_client("Acme") is False


# ============================================================================
# create_search
# ============================================================================


class TestCreateSearch:
    def test_full_body(self):
        seen: dict[str, Any] = {}

        def h(request: httpx.Request) -> httpx.Response:
            seen["json"] = json.loads(request.content)
            seen["path"] = request.url.path
            return httpx.Response(200, json={"id": "recSEARCH", "fields": {}})

        with make_client(h) as at:
            sid = at.create_search(_search_record())

        assert sid == "recSEARCH"
        assert AIRTABLE_SEARCHES_TABLE in seen["path"]
        body = seen["json"]
        assert body["typecast"] is True
        f = body["fields"]
        assert f[SEARCH_NAME] == "Acme Robotics CRO"
        assert f[SEARCH_STATUS] == "Qualified"
        assert f[SEARCH_CLIENT] == ["recCLIENT"]
        assert f[SEARCH_LEAD_DATE] == "2026-05-06"
        assert f[SEARCH_OUTCOME] == "Open"
        assert f[SEARCH_BIZ_TYPE] == ["Enterprise"]
        assert f[SEARCH_LEAD_SOURCE_TYPE] == "VC"
        assert f[SEARCH_OPEN] is True
        assert f[SEARCH_GMAIL_MESSAGE_ID] == "abc123"
        # Optionals that were provided
        assert f[SEARCH_LEAD_SOURCE] == ["recACCEL"]
        assert f[SEARCH_LEAD_SOURCE_INDIVIDUAL] == ["Jane Investor"]
        assert f[SEARCH_LEAD_RECIPIENT] == ["recMATT"]
        assert f[SEARCH_SENIORITY] == "Chief"
        assert f[SEARCH_ROLE] == ["Sales"]
        assert f[SEARCH_BIZ_ARR] == 12.0
        assert f[SEARCH_COMPANY_HQ] == "San Francisco"
        assert f[SEARCH_SERIES] == "B"
        assert f[SEARCH_SEARCH_TYPE] == "Core"
        assert f[SEARCH_LEAD_NOTES] == "Series B SF robotics; first CRO."

    def test_omits_none_fields_entirely(self):
        seen: dict[str, Any] = {}

        def h(request: httpx.Request) -> httpx.Response:
            seen["json"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "recSEARCH", "fields": {}})

        record = _search_record(
            lead_source_company_record_id=None,
            lead_source_individual=None,
            biz_arr=None,
            company_hq=None,
            website=None,
        )
        with make_client(h) as at:
            at.create_search(record)

        f = seen["json"]["fields"]
        # Critically: we send NO key at all, not `null`.
        assert SEARCH_LEAD_SOURCE not in f
        assert SEARCH_LEAD_SOURCE_INDIVIDUAL not in f
        assert SEARCH_BIZ_ARR not in f
        assert SEARCH_COMPANY_HQ not in f
        # Required fields still present.
        assert f[SEARCH_NAME] == "Acme Robotics CRO"
        assert f[SEARCH_GMAIL_MESSAGE_ID] == "abc123"


# ============================================================================
# _request retry behavior
# ============================================================================


class TestRequestRetry:
    def test_retries_on_429_then_succeeds(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr("cole_leads.airtable.time.sleep", lambda s: sleeps.append(s))

        responses = [
            httpx.Response(429, json={"error": "RATE_LIMITED"}),
            httpx.Response(429, json={"error": "RATE_LIMITED"}),
            httpx.Response(200, json={"records": []}),
        ]
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            ok = at.search_exists_for_message_id("abc")

        assert ok is False
        assert len(calls) == 3
        # Sleeps before attempts 2 and 3: 0.5, 1.0.
        assert sleeps == [0.5, 1.0]

    def test_retries_on_503(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr("cole_leads.airtable.time.sleep", lambda s: sleeps.append(s))

        responses = [
            httpx.Response(503, text="upstream down"),
            httpx.Response(200, json={"records": [{"id": "rec1"}]}),
        ]
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            assert at.search_exists_for_message_id("abc") is True
        assert len(calls) == 2
        assert sleeps == [0.5]

    def test_does_not_retry_on_400(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("cole_leads.airtable.time.sleep", lambda s: slept.append(s))

        responses = [httpx.Response(400, json={"error": "INVALID_FORMULA"})]
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            with pytest.raises(AirtableError, match="400"):
                at.search_exists_for_message_id("abc")
        assert len(calls) == 1
        assert slept == []

    def test_does_not_retry_on_404(self, monkeypatch):
        monkeypatch.setattr("cole_leads.airtable.time.sleep", lambda _s: None)

        responses = [httpx.Response(404, text="not found")]
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            with pytest.raises(AirtableError, match="404"):
                at.find_client_by_name("Acme")
        assert len(calls) == 1

    def test_gives_up_after_max_retries(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr("cole_leads.airtable.time.sleep", lambda s: sleeps.append(s))

        responses = [httpx.Response(429, text="rate limited")] * MAX_RETRIES
        handler, calls = make_recording_handler(responses)

        with make_client(handler) as at:
            with pytest.raises(AirtableError, match="after 5 attempts"):
                at.search_exists_for_message_id("abc")

        assert len(calls) == MAX_RETRIES
        # One sleep between each of the first 4 attempts; no sleep after the last.
        assert sleeps == [0.5, 1.0, 2.0, 4.0]


# ============================================================================
# delete_search / delete_client (integration-test cleanup)
# ============================================================================


class TestDelete:
    def test_delete_search_issues_delete_to_searches_table(self):
        seen: dict[str, Any] = {}

        def h(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            return httpx.Response(200, json={"deleted": True, "id": "recSEARCH"})

        with make_client(h) as at:
            at.delete_search("recSEARCH")

        assert seen["method"] == "DELETE"
        assert AIRTABLE_SEARCHES_TABLE in seen["path"]
        assert seen["path"].endswith("/recSEARCH")

    def test_delete_client_issues_delete_to_clients_table(self):
        seen: dict[str, Any] = {}

        def h(request: httpx.Request) -> httpx.Response:
            seen["method"] = request.method
            seen["path"] = request.url.path
            return httpx.Response(200, json={"deleted": True, "id": "recCLIENT"})

        with make_client(h) as at:
            at.delete_client("recCLIENT")

        assert seen["method"] == "DELETE"
        assert AIRTABLE_CLIENTS_TABLE in seen["path"]
        assert seen["path"].endswith("/recCLIENT")
