"""Gmail client.

Fetches unprocessed leads, parses them into `RawEmail`, and applies
`cole-leads/processed` / `cole-leads/failed` labels so we never reprocess a
message and can hand-review failures.

Why label-based, not unread-based: anyone reading the inbox would mark messages
read and we'd skip them forever. The `cole-leads/processed` label is owned by
this script.
"""

from __future__ import annotations

import base64
import html
import re
import time
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .logging import get_logger
from .models import RawEmail

PROCESSED_LABEL_NAME = "cole-leads/processed"
FAILED_LABEL_NAME = "cole-leads/failed"

LEADS_QUERY = (
    "(to:leads@colegroup.com OR to:leads@cole.co OR to:leads@colellc.com) "
    f"-label:{PROCESSED_LABEL_NAME}"
)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

MAX_RETRIES = 5
RETRY_BASE_SECONDS = 0.5

logger = get_logger(__name__)


class GmailError(RuntimeError):
    """Raised on non-retryable Gmail failures or after retries are exhausted."""


def _strip_html(s: str) -> str:
    """Cheap HTML-to-text: drop tags, collapse whitespace, unescape entities.

    We only need this as a fallback when an email has no text/plain part. Real
    HTML parsing would be overkill — these are all forwarded human emails.
    """
    # Drop <script>/<style> blocks entirely.
    s = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", " ", s, flags=re.DOTALL | re.IGNORECASE)
    # Strip remaining tags.
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    # Collapse runs of whitespace but keep paragraph breaks.
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _decode_body_data(data: str) -> str:
    """Gmail returns body bytes as base64url (no padding sometimes); decode safely."""
    if not data:
        return ""
    padding = "=" * (-len(data) % 4)
    raw = base64.urlsafe_b64decode(data + padding)
    return raw.decode("utf-8", errors="replace")


def _extract_body_text(payload: dict[str, Any]) -> str:
    """Walk a Gmail message payload tree and return the best body text.

    Preference: first text/plain part anywhere in the tree. If none exists,
    strip HTML from the first text/html part. Empty string if nothing usable.
    """

    def _walk(parts: list[dict[str, Any]], mime: str) -> str | None:
        for part in parts:
            if part.get("mimeType") == mime and part.get("body", {}).get("data"):
                return _decode_body_data(part["body"]["data"])
            if part.get("parts"):
                nested = _walk(part["parts"], mime)
                if nested is not None:
                    return nested
        return None

    # Single-part message.
    if "parts" not in payload:
        mime = payload.get("mimeType", "")
        data = payload.get("body", {}).get("data", "")
        decoded = _decode_body_data(data)
        if mime == "text/html":
            return _strip_html(decoded)
        return decoded

    parts = payload["parts"]
    plain = _walk(parts, "text/plain")
    if plain is not None:
        return plain
    html_body = _walk(parts, "text/html")
    if html_body is not None:
        return _strip_html(html_body)
    return ""


def _header(headers: list[dict[str, str]], name: str) -> str:
    """Case-insensitive header lookup."""
    target = name.lower()
    for h in headers:
        if h.get("name", "").lower() == target:
            return h.get("value", "")
    return ""


def _parse_date(value: str) -> date:
    """Parse an RFC-5322 Date header; fall back to today on parse failure."""
    if not value:
        return date.today()
    try:
        dt = parsedate_to_datetime(value)
        if dt is None:
            return date.today()
        return dt.date()
    except (TypeError, ValueError):
        return date.today()


def _build_raw_email(message: dict[str, Any]) -> RawEmail:
    payload = message.get("payload", {})
    headers = payload.get("headers", [])
    received = _header(headers, "Date")
    return RawEmail(
        message_id=message["id"],
        thread_id=message.get("threadId", ""),
        subject=_header(headers, "Subject"),
        from_addr=_header(headers, "From"),
        to_addr=_header(headers, "To"),
        cc_addr=_header(headers, "Cc") or None,
        received_at=_parse_date(received),
        body_text=_extract_body_text(payload),
    )


class GmailClient:
    """Wrap a `gmail` v1 discovery service.

    Construct with no args for production (OAuth creds from env). Tests pass
    `service=<MagicMock>` to bypass the real SDK.
    """

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        user_email: str | None = None,
        service: Any = None,
    ) -> None:
        if service is None:
            service = _build_service(
                client_id=client_id,
                client_secret=client_secret,
                refresh_token=refresh_token,
            )
        self._service = service
        self._user = user_email or "me"
        self._processed_label_id: str | None = None
        self._failed_label_id: str | None = None

    # ----- public API -------------------------------------------------------

    def fetch_unprocessed_leads(self, max_results: int = 50) -> list[RawEmail]:
        """Return unprocessed lead emails, newest first.

        Filters out anything already labeled `cole-leads/processed`. Limit
        defaults to 50; CI runs every 15 min so backlog should be small.
        """
        listing = self._exec(
            self._service.users()
            .messages()
            .list(
                userId=self._user,
                q=LEADS_QUERY,
                maxResults=max_results,
            )
        )
        message_refs = listing.get("messages", []) or []
        logger.info(
            "gmail_listed",
            extra={"count": len(message_refs), "max_results": max_results},
        )

        emails: list[RawEmail] = []
        for ref in message_refs:
            msg = self._exec(
                self._service.users().messages().get(userId=self._user, id=ref["id"], format="full")
            )
            try:
                emails.append(_build_raw_email(msg))
            except Exception as e:  # noqa: BLE001 — best-effort parsing
                logger.warning(
                    "gmail_parse_failed",
                    extra={"message_id": ref["id"], "error": str(e)},
                )
                continue
        return emails

    def mark_processed(self, message_id: str) -> None:
        label_id = self._get_or_create_label(PROCESSED_LABEL_NAME, "_processed_label_id")
        self._exec(
            self._service.users()
            .messages()
            .modify(
                userId=self._user,
                id=message_id,
                body={"addLabelIds": [label_id]},
            )
        )
        logger.info("gmail_marked_processed", extra={"message_id": message_id})

    def mark_failed(self, message_id: str, error: str) -> None:
        label_id = self._get_or_create_label(FAILED_LABEL_NAME, "_failed_label_id")
        self._exec(
            self._service.users()
            .messages()
            .modify(
                userId=self._user,
                id=message_id,
                body={"addLabelIds": [label_id]},
            )
        )
        logger.warning("gmail_marked_failed", extra={"message_id": message_id, "error": error})

    # ----- helpers ----------------------------------------------------------

    def _get_or_create_label(self, name: str, attr: str) -> str:
        cached: str | None = getattr(self, attr)
        if cached is not None:
            return cached
        labels = self._exec(self._service.users().labels().list(userId=self._user))
        for lbl in labels.get("labels", []):
            if lbl.get("name") == name:
                setattr(self, attr, lbl["id"])
                return lbl["id"]
        # Not present — create it. (The processed label is supposed to exist
        # already, but creating on miss is harmless and helps the failed-label path.)
        created = self._exec(
            self._service.users()
            .labels()
            .create(
                userId=self._user,
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
        )
        setattr(self, attr, created["id"])
        return created["id"]

    def _exec(self, request: Any) -> Any:
        """Execute a discovery-resource request with retry on 429 and 5xx.

        4xx other than 429 raise immediately — those are auth or programmer
        errors, retrying just amplifies the problem.
        """
        last_exc: HttpError | None = None
        for attempt in range(MAX_RETRIES):
            try:
                return request.execute()
            except HttpError as e:
                status = _http_error_status(e)
                last_exc = e
                retryable = status == 429 or (status is not None and 500 <= status < 600)
                if not retryable:
                    raise GmailError(f"Gmail call failed: {status} {e}") from e
                if attempt == MAX_RETRIES - 1:
                    break
                time.sleep(RETRY_BASE_SECONDS * (2**attempt))
        raise GmailError(f"Gmail call failed after {MAX_RETRIES} attempts: {last_exc}")


def _http_error_status(err: HttpError) -> int | None:
    """googleapiclient HttpError exposes status via .resp.status (int)."""
    resp = getattr(err, "resp", None)
    if resp is None:
        return None
    status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _build_service(
    *,
    client_id: str | None,
    client_secret: str | None,
    refresh_token: str | None,
) -> Any:
    """Construct a credentials-bearing Gmail service.

    Imported lazily so tests that inject `service=` don't pay the cost of
    importing google.oauth2 every run.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    from .config import get_settings

    if client_id is None or client_secret is None or refresh_token is None:
        settings = get_settings()
        client_id = client_id or settings.gmail_client_id
        client_secret = client_secret or settings.gmail_client_secret
        refresh_token = refresh_token or settings.gmail_refresh_token

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=GMAIL_SCOPES,
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


__all__ = [
    "FAILED_LABEL_NAME",
    "GMAIL_SCOPES",
    "GmailClient",
    "GmailError",
    "LEADS_QUERY",
    "PROCESSED_LABEL_NAME",
]
