"""Deterministic post-processing of the model's output.

The model is good at reading emails and searching the web, but it was also
being trusted with things that are either rules (Search Type), controlled
vocabularies (Role, Seniority, HQ) or easy to sanity-check (ARR units, stage
vs. ARR). Those live here so they're testable and can't drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Controlled vocabularies (must match Airtable select options exactly)
# ---------------------------------------------------------------------------

ROLES = (
    "Sales",
    "Marketing",
    "Sales Ops",
    "Customer Success",
    "General Management",
    "Other Category",
)
SENIORITIES = ("Chief", "SVP", "VP", "Head", "Director", "GM")

# ---------------------------------------------------------------------------
# Search Type (Fiona's rule): ARR at Lead Date, $M.
#   < 16 Core, < 51 Strategic, else Franchise. Public is always Franchise.
# Same cut-offs as the Airtable "Stage" formula (Early / Mid / Late).
# ---------------------------------------------------------------------------

CORE_MAX = 16
STRATEGIC_MAX = 51

_SERIES_FALLBACK = {
    "Pre-Seed": "Core",
    "Seed": "Core",
    "A": "Core",
    "B": "Core",
    "C": "Strategic",
    "D": "Strategic",
    "E": "Franchise",
    "F": "Franchise",
    "G": "Franchise",
    "Public": "Franchise",
}


def search_type_for(arr_m: float | None, series: str | None) -> tuple[str | None, str | None]:
    """Return (search_type, review_note). Uses ARR; falls back to series only
    when ARR is unknown, and says so."""
    if series == "Public":
        return "Franchise", None
    if arr_m is not None:
        if arr_m < CORE_MAX:
            return "Core", None
        if arr_m < STRATEGIC_MAX:
            return "Strategic", None
        return "Franchise", None
    guess = _SERIES_FALLBACK.get(series or "")
    if guess:
        return guess, f"No ARR found - Search Type '{guess}' guessed from Series {series}."
    return None, "No ARR or Series found - Search Type left blank."


# ---------------------------------------------------------------------------
# Role / seniority from the job title
# ---------------------------------------------------------------------------

# Each rule: (case-sensitive acronyms, case-insensitive words, value).
# Order matters: most specific first.
_ROLE_RULES: list[tuple[str, str, str]] = [
    (
        r"\bVPRO\b|\bRevOps\b",
        r"\brev(enue)?\s*op(eration)?s\b|\bsales\s*op(eration)?s\b|\bgtm\s*op(eration)?s\b",
        "Sales Ops",
    ),
    (
        r"\bCCO\b|\bVPCS\b|\bHOCS\b|\bCS\b",
        r"\bcustomer\s*(success|experience|support|care)\b|\bchief\s*customer\b|\bpost[- ]sales\b",
        "Customer Success",
    ),
    (
        r"\bCMO\b|\bPMM\b|\bVPM\b|\bHOM\b|\bSVPM\b",
        r"\bmarketing\b|\bgrowth\b|\bbrand\b|\bdemand\s*gen",
        "Marketing",
    ),
    (
        r"\bCRO\b|\bBD\b|\bGTM\b|\bVPS\b|\bHOS\b|\bSVPS\b|\bHOR\b",
        r"\bchief\s*revenue\b|\bsales\b|\brev(enue)?\b|\bbusiness\s*development\b|\bpartnerships?\b|\bcommercial\b|\bgo[- ]to[- ]market\b|\bsolutions?\s*engineering\b",
        "Sales",
    ),
    (
        r"\bCOO\b|\bGM\b|\bCEO\b",
        r"\bpresident\b|\bchief\s*operating\b|\bgeneral\s*manager\b|\bchief\s*executive\b|\bmanaging\s*director\b",
        "General Management",
    ),
]

_SENIORITY_RULES: list[tuple[str, str, str]] = [
    (r"\bC[A-Z]O\b", r"\bchief\b|\bpresident\b", "Chief"),
    (
        r"\bSVP|\bEVP|\bGVP",
        r"\bsenior\s*vice\s*president\b|\bexecutive\s*vice\s*president\b|\bgroup\s*vice\s*president\b",
        "SVP",
    ),
    (r"\bVP", r"\bvice\s*president\b", "VP"),
    (r"\bHO[A-Z]{1,2}\b", r"\bhead\s*of\b", "Head"),
    (r"\bGM\b", r"\bgeneral\s*manager\b", "GM"),
    (r"(?!)", r"\bdirector\b", "Director"),
]


def _match(acr: str, words: str, text: str) -> bool:
    return bool(re.search(acr, text) or re.search(words, text, re.IGNORECASE))


def roles_from_title(title: str | None) -> list[str]:
    """All roles a title mentions, e.g. 'CRO / VP Marketing' -> [Sales, Marketing]."""
    if not title:
        return []
    found: list[str] = []
    for part in re.split(r"\s*(?:/|\bor\b|,|&|\+)\s*", title):
        for acr, words, role in _ROLE_RULES:
            if _match(acr, words, part):
                if role not in found:
                    found.append(role)
                break
    return found


def seniority_from_title(title: str | None) -> str | None:
    """Most senior level the title mentions."""
    if not title:
        return None
    for acr, words, sen in _SENIORITY_RULES:
        if _match(acr, words, title):
            return sen
    return None


@dataclass
class RoleCheck:
    roles: list[str]
    seniority: str
    notes: list[str] = field(default_factory=list)


def reconcile_role(model_roles: list[str], model_seniority: str, title: str | None) -> RoleCheck:
    """Keep the model's call unless the literal title clearly says otherwise."""
    notes: list[str] = []
    roles = [r for r in model_roles if r in ROLES] or ["Other Category"]
    sen = model_seniority if model_seniority in SENIORITIES else "VP"

    t_roles = roles_from_title(title)
    t_sen = seniority_from_title(title)
    if t_roles and not set(t_roles) & set(roles):
        notes.append(
            f"Role: model said {'/'.join(roles)} but title '{title}' reads as "
            f"{'/'.join(t_roles)} - used the title."
        )
        roles = t_roles
    elif t_roles and len(t_roles) > len(roles):
        roles = t_roles  # e.g. "CRO / VP Marketing" -> both
    if t_sen and t_sen != sen:
        notes.append(
            f"Seniority: model said {sen} but title '{title}' reads as {t_sen} - used the title."
        )
        sen = t_sen
    return RoleCheck(roles=roles, seniority=sen, notes=notes)


# ---------------------------------------------------------------------------
# HQ: match an existing Airtable option instead of spawning near-duplicates
# ---------------------------------------------------------------------------

_HQ_ALIASES = {
    "new york city": "New York",
    "nyc": "New York",
    "new york, ny": "New York",
    "manhattan": "New York",
    "sf": "San Francisco",
    "san francisco bay area": "San Francisco",
    "washington dc": "Washington, DC",
    "washington d.c.": "Washington, DC",
    "tel aviv-yafo": "Tel Aviv",
}


def normalize_hq(city: str | None, options: list[str] | None = None) -> str | None:
    if not city:
        return None
    c = city.strip()
    # "Austin, Texas" / "Paris, France" -> city part
    base = c.split(",")[0].strip() if "," in c and c.lower() not in _HQ_ALIASES else c
    alias = _HQ_ALIASES.get(c.lower()) or _HQ_ALIASES.get(base.lower())
    if alias:
        return alias
    if options:
        by_lower = {o.strip().lower(): o.strip() for o in options}
        if base.lower() in by_lower:
            return by_lower[base.lower()]
    return base


# ---------------------------------------------------------------------------
# ARR / stage sanity checks
# ---------------------------------------------------------------------------


def normalize_arr(arr: float | None) -> tuple[float | None, list[str]]:
    """Airtable stores Biz ARR in $M. Catch the model returning raw dollars."""
    notes: list[str] = []
    if arr is None:
        return None, notes
    if arr >= 100_000:  # 12_000_000 -> 12
        notes.append(f"ARR looked like raw dollars ({arr:,.0f}); converted to ${arr / 1e6:.1f}M.")
        arr = arr / 1_000_000
    if arr < 0:
        return None, ["Negative ARR returned - cleared."]
    return round(arr, 1), notes


_EARLY_SERIES = {"Pre-Seed", "Seed", "A"}


def sanity_notes(
    *,
    arr_m: float | None,
    series: str | None,
    total_funding_m: float | None,
    confidence: str | None,
) -> list[str]:
    """Flag combinations that are usually a research mistake."""
    notes: list[str] = []
    if arr_m is not None and series in _EARLY_SERIES and arr_m >= 40:
        notes.append(
            f"ARR ${arr_m:g}M is unusually high for Series {series} - check it isn't "
            "funding raised, valuation or today's revenue instead of revenue at the lead date."
        )
    if arr_m is not None and series == "Public" and arr_m < 50:
        notes.append(f"Public company with ARR ${arr_m:g}M - check ARR/series.")
    if (
        arr_m is not None
        and total_funding_m
        and abs(arr_m - total_funding_m) / max(total_funding_m, 1) < 0.05
    ):
        notes.append(
            f"ARR ${arr_m:g}M matches total funding raised - probably funding, not revenue."
        )
    if confidence == "low":
        notes.append("Model marked its ARR/series research as low confidence.")
    return notes


# ---------------------------------------------------------------------------
# One entry point used by the pipeline
# ---------------------------------------------------------------------------


def finalize_lead(lead, *, hq_options: list[str] | None = None):  # -> (Lead, list[str])
    """Apply every deterministic rule to the model's output.

    Returns a new Lead plus review notes for anything the rules changed or
    couldn't settle.
    """
    notes: list[str] = []
    p = lead.parsed.model_copy()
    r = lead.research.model_copy()

    # Role / seniority vs the literal title
    rc = reconcile_role([p.role, *p.additional_roles], p.seniority, p.role_title)
    notes += rc.notes
    p.role = rc.roles[0]
    p.additional_roles = rc.roles[1:]
    p.seniority = rc.seniority

    # ARR units, Search Type from ARR, sanity flags
    r.biz_arr, arr_notes = normalize_arr(r.biz_arr)
    notes += arr_notes
    r.search_type, st_note = search_type_for(r.biz_arr, r.series)
    if st_note:
        notes.append(st_note)
    notes += sanity_notes(
        arr_m=r.biz_arr,
        series=r.series,
        total_funding_m=r.total_funding_m,
        confidence=r.research_confidence,
    )
    if r.series == "Unknown":
        notes.append("Series unknown - please fill in.")

    # HQ to an existing option
    r.company_hq = normalize_hq(r.company_hq, hq_options)

    # Investors: de-dupe the model's own list
    seen: set[str] = set()
    invs = []
    for name in r.investors:
        k = name.strip().lower()
        if k and k not in seen:
            seen.add(k)
            invs.append(name.strip())
    r.investors = invs[:8]

    return lead.model_copy(update={"parsed": p, "research": r}), notes


# ---------------------------------------------------------------------------
# Reuse what Cole already knows about a returning client
# ---------------------------------------------------------------------------

PRIOR_MAX_AGE_DAYS = 548  # ~18 months


def apply_prior_search(lead, prior, notes: list[str]):  # -> (Lead, list[str])
    """Fill gaps from the client's most recent earlier Search (human-vetted):
    ARR and Series when research came back empty, then recompute Search Type.
    Flags big disagreements instead of silently choosing."""
    if prior is None or prior.lead_date is None:
        return lead, notes
    age = (lead.parsed.lead_date - prior.lead_date).days
    if age < 0 or age > PRIOR_MAX_AGE_DAYS:
        return lead, notes
    r = lead.research.model_copy()
    label = f"'{prior.name or 'earlier search'}' ({prior.lead_date})"
    changed = False
    if r.biz_arr is None and prior.biz_arr is not None:
        r.biz_arr = float(prior.biz_arr)
        changed = True
        notes = [n for n in notes if not n.startswith("No ARR found")]
        notes.append(f"ARR ${r.biz_arr:g}M taken from {label}.")
    elif r.biz_arr is not None and prior.biz_arr and age <= 183:
        ratio = max(r.biz_arr, prior.biz_arr) / max(min(r.biz_arr, prior.biz_arr), 0.1)
        if ratio > 2:
            notes.append(
                f"ARR ${r.biz_arr:g}M differs a lot from ${prior.biz_arr:g}M on {label} - check."
            )
    if (r.series in (None, "Unknown")) and prior.series:
        r.series = prior.series
        changed = True
        notes = [n for n in notes if not n.startswith("Series unknown")]
        notes.append(f"Series {prior.series} taken from {label}.")
    if changed:
        r.search_type, st_note = search_type_for(r.biz_arr, r.series)
        notes = [n for n in notes if not n.startswith("No ARR")]
        if st_note:
            notes.append(st_note)
    return lead.model_copy(update={"research": r}), notes
