"""Typed Airtable client wrapping the REST API directly.

Design rules:
  * Exact-match lookups (`LOWER({field})=LOWER("…")`) — never substring SEARCH.
    The exception is `has_closed_searches_for_client`, which uses anchored
    `FIND(...)=1` because the Search field is `"<Client> <Role abbrev>"`.
  * Request bodies are Python dicts; httpx JSON-encodes them. We never build
    JSON strings by hand — the n8n version's f-string templates broke on any
    quote or newline in a field value.
  * Single `_request` helper retries on 429 and 5xx with exponential backoff
    (max 5 tries). 4xx other than 429 fail immediately — those are bugs, not
    transient.
  * Every method accepts an optional `httpx.Client`; tests inject one wired to
    `httpx.MockTransport`.
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

import httpx

from .config import (
    AIRTABLE_BASE_ID,
    AIRTABLE_CLIENTS_TABLE,
    AIRTABLE_CLOSES_VIEW,
    AIRTABLE_INVESTORS_TABLE,
    AIRTABLE_SEARCHES_TABLE,
    get_settings,
)
from .lead_source import ClientRow, InvestorRow, RelationshipIndex, SearchRow
from .models import CompanyResearch, SearchRecord

# ---------------------------------------------------------------------------
# Field names — all strings live here so we don't repeat magic values inline.
# ---------------------------------------------------------------------------

# Searches table
SEARCH_NAME = "Search"
SEARCH_STATUS = "Status Expanded"
SEARCH_LEAD_SOURCE = "Lead Source"  # link -> Clients
SEARCH_LEAD_SOURCE_INDIVIDUAL = "Lead Source Individual"
SEARCH_LEAD_RECIPIENT = "Lead Recipient"  # link -> team
SEARCH_LEAD_DATE = "Lead Date"
SEARCH_CLIENT = "Client"  # link -> Clients
SEARCH_OUTCOME = "Outcome"
SEARCH_BIZ_TYPE = "Biz Type"
SEARCH_SENIORITY = "Seniority"
SEARCH_ROLE = "Role"
SEARCH_BIZ_ARR = "Biz ARR"
SEARCH_COMPANY_HQ = "Company HQ (Location)"
SEARCH_SERIES = "Series"
SEARCH_SEARCH_TYPE = "Search Type (Core, Strategic, Franchise)"
SEARCH_LEAD_SOURCE_TYPE = "Lead Source Type"
SEARCH_LEAD_NOTES = "Lead Notes"
SEARCH_OPEN = "Open?"
SEARCH_GMAIL_MESSAGE_ID = "Gmail Message ID"
SEARCH_LEAD_SOURCE_VC = "Lead Source (VC Only)"  # link -> Investors
SEARCH_REVIEW_FLAG = "Leads For Fiona's Review"
SEARCH_REVIEW_NOTES = "Leads Review Notes"
SEARCH_OUTCOME_DATE = "Outcome Date"
SEARCH_KICKOFF = "Kickoff"
SEARCH_CLOSE_DATE = "Close Date"

# Clients table
CLIENT_NAME = "Client Name"
CLIENT_BIZ_TYPE = "Biz Type"
CLIENT_BIZ_ARR = "Biz ARR"
CLIENT_WEBSITE = "Website"
CLIENT_INVESTORS = "Investors"  # multi-link -> Investors

# Investors table
INVESTOR_NAME = "Name"


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

BASE_URL = "https://api.airtable.com/v0"
MAX_RETRIES = 5
RETRY_BASE_SECONDS = 0.5  # exponential: 0.5, 1, 2, 4, 8


def _parse_date(v: Any) -> date | None:
    if not v or not isinstance(v, str):
        return None
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        return None


def _as_tuple(v: Any) -> tuple[str, ...]:
    if not v:
        return ()
    if isinstance(v, str):
        return (v,)
    return tuple(x for x in v if isinstance(x, str))


class AirtableError(RuntimeError):
    """Raised on non-retryable Airtable failures or after retries are exhausted."""


def _escape_formula_literal(s: str) -> str:
    """Escape a value for inclusion inside a double-quoted Airtable formula literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


class AirtableClient:
    """Direct wrapper over the Airtable REST API.

    Construct with no args for production (PAT read from env). Tests pass
    `http=httpx.Client(transport=MockTransport(...))` to skip the network.
    """

    def __init__(
        self,
        *,
        pat: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        if http is None:
            if pat is None:
                pat = get_settings().airtable_pat
            http = httpx.Client(
                base_url=BASE_URL,
                headers={"Authorization": f"Bearer {pat}"},
                timeout=30.0,
            )
            self._owns_http = True
        else:
            self._owns_http = False
        self._http = http

    # Context-manager support so callers can `with AirtableClient() as at:` ...
    def __enter__(self) -> AirtableClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # ----- low-level request with retry --------------------------------------

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last_status: int | None = None
        last_text: str = ""
        for attempt in range(MAX_RETRIES):
            response = self._http.request(method, path, **kwargs)
            if response.status_code < 400:
                return response
            last_status = response.status_code
            last_text = response.text
            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if not retryable:
                # 4xx other than 429 — application bug, fail loudly.
                raise AirtableError(
                    f"{method} {path} failed: {response.status_code} {response.text}"
                )
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(RETRY_BASE_SECONDS * (2**attempt))
        raise AirtableError(
            f"{method} {path} failed after {MAX_RETRIES} attempts: {last_status} {last_text}"
        )

    # ----- Searches: idempotency check ---------------------------------------

    def search_exists_for_message_id(self, message_id: str) -> bool:
        """True if a Search row already exists with this Gmail message ID."""
        formula = f'{{{SEARCH_GMAIL_MESSAGE_ID}}}="{_escape_formula_literal(message_id)}"'
        resp = self._request(
            "GET",
            f"/{AIRTABLE_BASE_ID}/{AIRTABLE_SEARCHES_TABLE}",
            params={
                "filterByFormula": formula,
                "maxRecords": 1,
            },
        )
        return bool(resp.json().get("records"))

    # ----- Clients -----------------------------------------------------------

    def find_client_by_name(self, name: str) -> str | None:
        """Exact case-insensitive match. Returns record ID or None."""
        formula = f'LOWER({{{CLIENT_NAME}}})=LOWER("{_escape_formula_literal(name)}")'
        resp = self._request(
            "GET",
            f"/{AIRTABLE_BASE_ID}/{AIRTABLE_CLIENTS_TABLE}",
            params={"filterByFormula": formula, "maxRecords": 1},
        )
        records = resp.json().get("records", [])
        return records[0]["id"] if records else None

    def create_client(self, name: str, research: CompanyResearch) -> str:
        """Create a row in Clients. `typecast=True` so linked-record fields by
        name (Biz Type as a single-select-ish array) resolve correctly."""
        fields: dict[str, Any] = {CLIENT_NAME: name}
        if research.biz_type is not None:
            fields[CLIENT_BIZ_TYPE] = [research.biz_type]
        if research.biz_arr is not None:
            fields[CLIENT_BIZ_ARR] = research.biz_arr
        if research.website is not None:
            fields[CLIENT_WEBSITE] = research.website
        resp = self._request(
            "POST",
            f"/{AIRTABLE_BASE_ID}/{AIRTABLE_CLIENTS_TABLE}",
            json={"fields": fields, "typecast": True},
        )
        return resp.json()["id"]

    def upsert_client(self, name: str, research: CompanyResearch) -> str:
        existing = self.find_client_by_name(name)
        if existing is not None:
            return existing
        return self.create_client(name, research)

    # ----- Investors ---------------------------------------------------------

    def find_or_create_investor(self, name: str) -> str:
        formula = f'LOWER({{{INVESTOR_NAME}}})=LOWER("{_escape_formula_literal(name)}")'
        resp = self._request(
            "GET",
            f"/{AIRTABLE_BASE_ID}/{AIRTABLE_INVESTORS_TABLE}",
            params={"filterByFormula": formula, "maxRecords": 1},
        )
        records = resp.json().get("records", [])
        if records:
            return records[0]["id"]
        created = self._request(
            "POST",
            f"/{AIRTABLE_BASE_ID}/{AIRTABLE_INVESTORS_TABLE}",
            json={"fields": {INVESTOR_NAME: name}, "typecast": True},
        )
        return created.json()["id"]

    def link_investors_to_client(self, client_id: str, investor_ids: list[str]) -> None:
        """Merge `investor_ids` into the client's existing Investors list.

        We read first so we never clobber investors a human added manually.
        """
        if not investor_ids:
            return
        existing_resp = self._request(
            "GET",
            f"/{AIRTABLE_BASE_ID}/{AIRTABLE_CLIENTS_TABLE}/{client_id}",
        )
        existing = existing_resp.json().get("fields", {}).get(CLIENT_INVESTORS, []) or []

        seen: set[str] = set()
        merged: list[str] = []
        for inv in [*existing, *investor_ids]:
            if inv not in seen:
                seen.add(inv)
                merged.append(inv)

        if merged == existing:
            return  # nothing new to add — skip the PATCH

        self._request(
            "PATCH",
            f"/{AIRTABLE_BASE_ID}/{AIRTABLE_CLIENTS_TABLE}/{client_id}",
            json={"fields": {CLIENT_INVESTORS: merged}, "typecast": True},
        )

    # ----- Existing-client detection (overrides lead_source_type) ------------

    def has_closed_searches_for_client(self, client_name: str) -> bool:
        """True if the 'closes all time' view has any Search row whose name
        starts with this client name.

        Uses anchored `FIND(...)=1` rather than `SEARCH(...)` so "Cole" doesn't
        match "Coleman" — same fix as the Client lookup, adapted to the
        `<Client> <Role abbrev>` shape of the Search field.
        """
        literal = _escape_formula_literal(client_name)
        formula = f'FIND(LOWER("{literal}"), LOWER({{{SEARCH_NAME}}}))=1'
        resp = self._request(
            "GET",
            f"/{AIRTABLE_BASE_ID}/{AIRTABLE_SEARCHES_TABLE}",
            params={
                "view": AIRTABLE_CLOSES_VIEW,
                "filterByFormula": formula,
                "maxRecords": 1,
            },
        )
        return bool(resp.json().get("records"))

    # ----- Relationship index (for Lead Source resolution) -------------------

    def _list_all(self, table: str, fields: list[str]) -> list[dict[str, Any]]:
        """Page through every record in `table`, returning only `fields`."""
        out: list[dict[str, Any]] = []
        offset: str | None = None
        while True:
            params: list[tuple[str, str | int]] = [("pageSize", 100)]
            params += [("fields[]", f) for f in fields]
            if offset:
                params.append(("offset", offset))
            body = self._request("GET", f"/{AIRTABLE_BASE_ID}/{table}", params=params).json()
            out.extend(body.get("records", []))
            offset = body.get("offset")
            if not offset:
                return out

    def load_relationship_index(self) -> RelationshipIndex:
        """Load Clients, Investors and Searches history into memory.

        ~one request per 100 rows; called once per run, and only when there
        is at least one lead to process.
        """
        clients = [
            ClientRow(
                id=r["id"],
                name=r.get("fields", {}).get(CLIENT_NAME, "") or "",
                website=r.get("fields", {}).get(CLIENT_WEBSITE),
            )
            for r in self._list_all(AIRTABLE_CLIENTS_TABLE, [CLIENT_NAME, CLIENT_WEBSITE])
        ]
        investors = [
            InvestorRow(id=r["id"], name=r.get("fields", {}).get(INVESTOR_NAME, "") or "")
            for r in self._list_all(AIRTABLE_INVESTORS_TABLE, [INVESTOR_NAME])
        ]
        search_fields = [
            SEARCH_CLIENT,
            SEARCH_STATUS,
            SEARCH_OUTCOME,
            SEARCH_LEAD_DATE,
            SEARCH_OUTCOME_DATE,
            SEARCH_KICKOFF,
            SEARCH_CLOSE_DATE,
            SEARCH_LEAD_SOURCE_INDIVIDUAL,
            SEARCH_LEAD_SOURCE,
            SEARCH_LEAD_SOURCE_VC,
            SEARCH_LEAD_SOURCE_TYPE,
        ]
        searches = []
        for r in self._list_all(AIRTABLE_SEARCHES_TABLE, search_fields):
            f = r.get("fields", {})
            searches.append(
                SearchRow(
                    id=r["id"],
                    client_ids=_as_tuple(f.get(SEARCH_CLIENT)),
                    status=f.get(SEARCH_STATUS),
                    outcome=f.get(SEARCH_OUTCOME),
                    lead_date=_parse_date(f.get(SEARCH_LEAD_DATE)),
                    outcome_date=_parse_date(f.get(SEARCH_OUTCOME_DATE)),
                    kickoff_date=_parse_date(f.get(SEARCH_KICKOFF)),
                    close_date=_parse_date(f.get(SEARCH_CLOSE_DATE)),
                    lead_source_individuals=_as_tuple(f.get(SEARCH_LEAD_SOURCE_INDIVIDUAL)),
                    lead_source_client_ids=_as_tuple(f.get(SEARCH_LEAD_SOURCE)),
                    lead_source_vc_ids=_as_tuple(f.get(SEARCH_LEAD_SOURCE_VC)),
                    lead_source_type=(f.get(SEARCH_LEAD_SOURCE_TYPE) or "").strip() or None,
                )
            )
        return RelationshipIndex(clients=clients, investors=investors, searches=searches)

    # ----- Searches: create --------------------------------------------------

    def create_search(self, record: SearchRecord) -> str:
        """Create a row in Searches from a `SearchRecord`. None-valued fields
        are omitted entirely (never sent as `null`)."""
        fields: dict[str, Any] = {
            SEARCH_NAME: record.search_name,
            SEARCH_STATUS: record.status,
            SEARCH_CLIENT: [record.client_record_id],
            SEARCH_LEAD_DATE: record.lead_date.isoformat(),
            SEARCH_OUTCOME: record.outcome,
            SEARCH_BIZ_TYPE: [record.biz_type],
            SEARCH_LEAD_SOURCE_TYPE: record.lead_source_type,
            SEARCH_OPEN: record.open_flag,
            SEARCH_GMAIL_MESSAGE_ID: record.gmail_message_id,
        }
        if record.lead_source_company_record_id is not None:
            fields[SEARCH_LEAD_SOURCE] = [record.lead_source_company_record_id]
        if record.lead_source_vc_investor_id is not None:
            fields[SEARCH_LEAD_SOURCE_VC] = [record.lead_source_vc_investor_id]
        if record.lead_source_individual is not None:
            # Multi-select in Airtable; typecast adds a new option if needed.
            fields[SEARCH_LEAD_SOURCE_INDIVIDUAL] = [record.lead_source_individual]
        if record.needs_review:
            fields[SEARCH_REVIEW_FLAG] = True
            fields[SEARCH_REVIEW_NOTES] = record.review_notes
        if record.lead_recipient_record_id is not None:
            fields[SEARCH_LEAD_RECIPIENT] = [record.lead_recipient_record_id]
        if record.seniority is not None:
            fields[SEARCH_SENIORITY] = record.seniority
        if record.role is not None:
            fields[SEARCH_ROLE] = [record.role]
        if record.biz_arr is not None:
            fields[SEARCH_BIZ_ARR] = record.biz_arr
        if record.company_hq is not None:
            fields[SEARCH_COMPANY_HQ] = record.company_hq
        if record.series is not None:
            fields[SEARCH_SERIES] = record.series
        if record.search_type is not None:
            fields[SEARCH_SEARCH_TYPE] = record.search_type
        if record.lead_notes is not None:
            fields[SEARCH_LEAD_NOTES] = record.lead_notes

        resp = self._request(
            "POST",
            f"/{AIRTABLE_BASE_ID}/{AIRTABLE_SEARCHES_TABLE}",
            json={"fields": fields, "typecast": True},
        )
        return resp.json()["id"]

    # ----- Cleanup (integration tests only) ----------------------------------

    def delete_search(self, record_id: str) -> None:
        """Hard-delete a Search row. Only the integration test calls this —
        the production pipeline never deletes."""
        self._request(
            "DELETE",
            f"/{AIRTABLE_BASE_ID}/{AIRTABLE_SEARCHES_TABLE}/{record_id}",
        )

    def delete_client(self, record_id: str) -> None:
        """Hard-delete a Client row. Only the integration test calls this."""
        self._request(
            "DELETE",
            f"/{AIRTABLE_BASE_ID}/{AIRTABLE_CLIENTS_TABLE}/{record_id}",
        )
