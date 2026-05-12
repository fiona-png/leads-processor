"""Tests for the Gmail client. No network — MagicMock all the way down."""

from __future__ import annotations

import base64
from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from cole_leads.gmail import (
    FAILED_LABEL_NAME,
    LEADS_QUERY,
    MAX_RETRIES,
    PROCESSED_LABEL_NAME,
    GmailClient,
    GmailError,
    _extract_body_text,
    _strip_html,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64(s: str) -> str:
    """Encode like Gmail does (base64url, no padding)."""
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _http_error(status: int, reason: str = "boom") -> HttpError:
    """Build a real HttpError without hitting the network."""
    resp = MagicMock()
    resp.status = status
    resp.reason = reason
    return HttpError(resp, b'{"error":"x"}')


def _service_with_list(messages: list[dict[str, Any]]) -> MagicMock:
    """Build a discovery service whose `messages.list` returns the given refs."""
    service = MagicMock()
    msgs = service.users.return_value.messages.return_value
    msgs.list.return_value.execute.return_value = {"messages": messages}
    return service


def _attach_get(service: MagicMock, messages_by_id: dict[str, dict[str, Any]]) -> None:
    """Wire up `messages.get(id=...).execute()` to return messages_by_id[id]."""

    def get(*, userId: str, id: str, format: str) -> Any:  # noqa: A002 — matches API
        rv = MagicMock()
        rv.execute.return_value = messages_by_id[id]
        return rv

    service.users.return_value.messages.return_value.get.side_effect = get


def _full_message(
    *,
    msg_id: str = "msg-1",
    subject: str = "Fwd: hi",
    from_addr: str = "matt@colellc.com",
    to_addr: str = "leads@colellc.com",
    date_value: str = "Mon, 11 May 2026 09:14:11 -0400",
    body: str = "hello world",
    mime_type: str = "text/plain",
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "threadId": f"thr-{msg_id}",
        "payload": {
            "mimeType": mime_type,
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": from_addr},
                {"name": "To", "value": to_addr},
                {"name": "Date", "value": date_value},
            ],
            "body": {"data": _b64(body)},
        },
    }


# ---------------------------------------------------------------------------
# Body-text extraction
# ---------------------------------------------------------------------------


class TestExtractBodyText:
    def test_single_text_plain(self):
        payload = {
            "mimeType": "text/plain",
            "body": {"data": _b64("plain text here")},
        }
        assert _extract_body_text(payload) == "plain text here"

    def test_single_text_html_falls_back_to_strip(self):
        payload = {
            "mimeType": "text/html",
            "body": {"data": _b64("<p>Hello <b>world</b></p>")},
        }
        assert _extract_body_text(payload) == "Hello world"

    def test_multipart_alternative_prefers_plain(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("PLAIN BODY")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>HTML BODY</p>")}},
            ],
        }
        assert _extract_body_text(payload) == "PLAIN BODY"

    def test_multipart_alternative_html_only_uses_html(self):
        payload = {
            "mimeType": "multipart/alternative",
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<p>HTML only</p>")}},
            ],
        }
        assert _extract_body_text(payload) == "HTML only"

    def test_multipart_mixed_nested(self):
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "application/pdf", "body": {"data": _b64("PDF-BYTES")}},
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("nested plain")}},
                        {"mimeType": "text/html", "body": {"data": _b64("<p>nested html</p>")}},
                    ],
                },
            ],
        }
        assert _extract_body_text(payload) == "nested plain"

    def test_strip_html_drops_script_and_collapses_whitespace(self):
        out = _strip_html(
            "<html><body>"
            "<script>alert('x')</script>"
            "<p>Hi    there</p>\n\n\n<p>Line 2</p>"
            "</body></html>"
        )
        assert "alert" not in out
        assert "Hi there" in out
        assert "Line 2" in out


# ---------------------------------------------------------------------------
# fetch_unprocessed_leads
# ---------------------------------------------------------------------------


class TestFetchUnprocessedLeads:
    def test_uses_label_based_query(self):
        service = _service_with_list([])
        client = GmailClient(service=service, user_email="me")

        client.fetch_unprocessed_leads(max_results=50)

        msgs = service.users.return_value.messages.return_value
        msgs.list.assert_called_once_with(userId="me", q=LEADS_QUERY, maxResults=50)
        # And the query must be label-based, not unread-based.
        assert "-label:cole-leads/processed" in LEADS_QUERY
        assert "is:unread" not in LEADS_QUERY

    def test_returns_empty_list_when_no_messages(self):
        service = _service_with_list([])
        client = GmailClient(service=service, user_email="me")
        assert client.fetch_unprocessed_leads() == []

    def test_builds_raw_email_from_full_message(self):
        ref = {"id": "abc123"}
        full = _full_message(msg_id="abc123", subject="Fwd: hi", body="Hello!\n")
        service = _service_with_list([ref])
        _attach_get(service, {"abc123": full})

        client = GmailClient(service=service, user_email="me")
        emails = client.fetch_unprocessed_leads()

        assert len(emails) == 1
        e = emails[0]
        assert e.message_id == "abc123"
        assert e.thread_id == "thr-abc123"
        assert e.subject == "Fwd: hi"
        assert e.from_addr == "matt@colellc.com"
        assert e.to_addr == "leads@colellc.com"
        assert e.body_text == "Hello!\n"
        assert e.received_at == date(2026, 5, 11)

    def test_max_results_param_is_forwarded(self):
        service = _service_with_list([])
        client = GmailClient(service=service, user_email="me")

        client.fetch_unprocessed_leads(max_results=10)
        service.users.return_value.messages.return_value.list.assert_called_once_with(
            userId="me", q=LEADS_QUERY, maxResults=10
        )


# ---------------------------------------------------------------------------
# Labels: mark_processed / mark_failed
# ---------------------------------------------------------------------------


def _labels_service(label_list: list[dict[str, str]]) -> MagicMock:
    """Build a service that returns the given labels on labels.list()."""
    service = MagicMock()
    service.users.return_value.labels.return_value.list.return_value.execute.return_value = {
        "labels": label_list
    }
    service.users.return_value.messages.return_value.modify.return_value.execute.return_value = {
        "id": "msg-1"
    }
    return service


class TestMarkProcessed:
    def test_uses_correct_label_id(self):
        service = _labels_service([{"id": "LBL_PROCESSED", "name": PROCESSED_LABEL_NAME}])
        client = GmailClient(service=service, user_email="me")

        client.mark_processed("msg-1")

        modify = service.users.return_value.messages.return_value.modify
        modify.assert_called_once_with(
            userId="me", id="msg-1", body={"addLabelIds": ["LBL_PROCESSED"]}
        )

    def test_caches_label_id_across_calls(self):
        service = _labels_service([{"id": "LBL_PROCESSED", "name": PROCESSED_LABEL_NAME}])
        client = GmailClient(service=service, user_email="me")

        client.mark_processed("msg-1")
        client.mark_processed("msg-2")

        labels_list = service.users.return_value.labels.return_value.list
        assert labels_list.call_count == 1


class TestMarkFailed:
    def test_creates_failed_label_on_first_use(self):
        service = _labels_service([])  # no labels exist
        service.users.return_value.labels.return_value.create.return_value.execute.return_value = {
            "id": "LBL_FAILED_NEW"
        }
        client = GmailClient(service=service, user_email="me")

        client.mark_failed("msg-1", "boom")

        create = service.users.return_value.labels.return_value.create
        create.assert_called_once()
        body = create.call_args.kwargs.get("body")
        assert body is not None and body["name"] == FAILED_LABEL_NAME

        modify = service.users.return_value.messages.return_value.modify
        modify.assert_called_once_with(
            userId="me", id="msg-1", body={"addLabelIds": ["LBL_FAILED_NEW"]}
        )

    def test_reuses_existing_failed_label(self):
        service = _labels_service([{"id": "LBL_FAILED_EXISTING", "name": FAILED_LABEL_NAME}])
        client = GmailClient(service=service, user_email="me")

        client.mark_failed("msg-1", "boom")

        # No label create because the label already existed.
        service.users.return_value.labels.return_value.create.assert_not_called()
        modify = service.users.return_value.messages.return_value.modify
        modify.assert_called_once_with(
            userId="me", id="msg-1", body={"addLabelIds": ["LBL_FAILED_EXISTING"]}
        )


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


class TestRetries:
    def test_429_then_success(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr("cole_leads.gmail.time.sleep", lambda s: sleeps.append(s))

        service = MagicMock()
        execute = service.users.return_value.messages.return_value.list.return_value.execute
        execute.side_effect = [_http_error(429), {"messages": []}]

        client = GmailClient(service=service, user_email="me")
        assert client.fetch_unprocessed_leads() == []
        assert execute.call_count == 2
        assert sleeps == [0.5]

    def test_503_retries(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr("cole_leads.gmail.time.sleep", lambda s: sleeps.append(s))

        service = MagicMock()
        execute = service.users.return_value.messages.return_value.list.return_value.execute
        execute.side_effect = [_http_error(503), {"messages": []}]

        client = GmailClient(service=service, user_email="me")
        assert client.fetch_unprocessed_leads() == []
        assert execute.call_count == 2

    def test_400_raises_immediately(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("cole_leads.gmail.time.sleep", lambda s: slept.append(s))

        service = MagicMock()
        execute = service.users.return_value.messages.return_value.list.return_value.execute
        execute.side_effect = _http_error(400, "Bad Request")

        client = GmailClient(service=service, user_email="me")
        with pytest.raises(GmailError, match="400"):
            client.fetch_unprocessed_leads()
        assert slept == []
        assert execute.call_count == 1

    def test_401_raises_immediately(self, monkeypatch):
        monkeypatch.setattr("cole_leads.gmail.time.sleep", lambda _s: None)

        service = MagicMock()
        execute = service.users.return_value.messages.return_value.list.return_value.execute
        execute.side_effect = _http_error(401, "Unauthorized")

        client = GmailClient(service=service, user_email="me")
        with pytest.raises(GmailError, match="401"):
            client.fetch_unprocessed_leads()

    def test_gives_up_after_max_retries(self, monkeypatch):
        sleeps: list[float] = []
        monkeypatch.setattr("cole_leads.gmail.time.sleep", lambda s: sleeps.append(s))

        service = MagicMock()
        execute = service.users.return_value.messages.return_value.list.return_value.execute
        execute.side_effect = [_http_error(429)] * MAX_RETRIES

        client = GmailClient(service=service, user_email="me")
        with pytest.raises(GmailError, match=f"after {MAX_RETRIES} attempts"):
            client.fetch_unprocessed_leads()
        assert execute.call_count == MAX_RETRIES
        assert sleeps == [0.5, 1.0, 2.0, 4.0]
