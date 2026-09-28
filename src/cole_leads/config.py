"""Static configuration: env vars, team map, role/seniority abbreviations, Airtable IDs.

Nothing in here makes network calls. Anything secret comes from environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Airtable
# ---------------------------------------------------------------------------

AIRTABLE_BASE_ID = "app8WfXnuvr10M26M"
AIRTABLE_CLIENTS_TABLE = "tbl5musqYq3Whujun"
AIRTABLE_INVESTORS_TABLE = "tbloDEmmXH6cdcfd2"
AIRTABLE_SEARCHES_TABLE = "tblAmCTGKNxtyrMKZ"
# View on the Searches table used to detect prior closed searches for a client.
AIRTABLE_CLOSES_VIEW = "viwOq2S67Uew3t8xz"


# ---------------------------------------------------------------------------
# Team: first-name (lowercase) -> Airtable record ID for Lead Recipient field
# ---------------------------------------------------------------------------

TEAM_MAP: dict[str, str] = {
    "jamie": "recQcggsMzAPNOghx",
    "matt": "recr5w4mfK2muZEx2",
    "gillian": "recpob7Zko8nUbmpf",
    "david": "recXXxcDyYzCWRwkC",
    "chloe": "reckIGJWFj26FAs8Z",
    "natalia": "recq0zqELLTUA76Ch",
    "clay": "rect1jZKqn6FtlWpw",
    "gia": "recCm4ilW1PFkO7sT",
    "bridget": "recoY8Iqul6qcx87v",
    "geoff": "rec9f5jBf3q8kj1vK",
    "fiona": "rectplJuZeBMiCBpy",
    "brandon": "rech0pp0Mt3nsKdjZ",
    "rob": "recWDxkomFcWO6cXd",
    "kelly": "recIKvT37OuAqK5Gz",
}

INTERNAL_DOMAINS: frozenset[str] = frozenset({"colellc.com", "cole.co", "colegroup.com"})


# ---------------------------------------------------------------------------
# Role abbreviation logic for search_name
# ---------------------------------------------------------------------------

# Maps a (seniority, role) pair to the abbreviation appended after the client name.
# Chief is special-cased to the C-suite letter. Head -> "HO{first letter of role}".
# Others -> "{seniority}{first letter of role}".

CHIEF_ABBREV: dict[str, str] = {
    "Sales": "CRO",
    "Marketing": "CMO",
    "Customer Success": "CCO",
    "General Management": "COO",
    "Sales Ops": "COO",
}


def role_abbrev(seniority: str, role: str) -> str:
    """Return the trailing abbreviation for the search name, e.g. 'CRO' or 'VPS'."""
    if seniority == "Chief":
        return CHIEF_ABBREV.get(role, "CXO")
    first_letter = role[0].upper() if role else "X"
    if seniority == "Head":
        return f"HO{first_letter}"
    return f"{seniority}{first_letter}"


def search_name(client: str, seniority: str, role: str, *, title: str | None = None) -> str:
    """`{client} {abbrev}`, the canonical Search record name."""
    if title and "president" in title.lower() and "vice" not in title.lower():
        return f"{client} President".strip()
    return f"{client} {role_abbrev(seniority, role)}".strip()


# ---------------------------------------------------------------------------
# Env-backed settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    airtable_pat: str
    gmail_client_id: str
    gmail_client_secret: str
    gmail_refresh_token: str
    gmail_user: str


def get_settings() -> Settings:
    """Read all required secrets from the environment. Raises if anything is missing."""
    required = {
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        "AIRTABLE_PAT": os.environ.get("AIRTABLE_PAT", ""),
        "GMAIL_CLIENT_ID": os.environ.get("GMAIL_CLIENT_ID", ""),
        "GMAIL_CLIENT_SECRET": os.environ.get("GMAIL_CLIENT_SECRET", ""),
        "GMAIL_REFRESH_TOKEN": os.environ.get("GMAIL_REFRESH_TOKEN", ""),
        "GMAIL_USER": os.environ.get("GMAIL_USER", ""),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise RuntimeError(f"Missing env vars: {', '.join(missing)}")
    return Settings(
        anthropic_api_key=required["ANTHROPIC_API_KEY"],
        airtable_pat=required["AIRTABLE_PAT"],
        gmail_client_id=required["GMAIL_CLIENT_ID"],
        gmail_client_secret=required["GMAIL_CLIENT_SECRET"],
        gmail_refresh_token=required["GMAIL_REFRESH_TOKEN"],
        gmail_user=required["GMAIL_USER"],
    )
