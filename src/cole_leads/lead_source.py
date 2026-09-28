"""Relationship-aware Lead Source resolution.

The LLM only sees the email, so it has no idea that Paul Cho is Sequoia's
talent partner or that Productboard has hired Cole three times. This module
answers those questions from Cole's own Airtable history and overrides the
LLM's guess.

Rules (Fiona's lead-audit definitions):
  * VC — the referrer works at a venture firm. VC intros are VC only; the fact
    that Cole once ran a search *for* the VC firm does not make it Existing Client.
  * Existing Client — the hiring company, or the referrer's company, had a
    search Won / Closed / Abandoned / Canceled before the Lead Date.
  * Company — the hiring company reached out itself.
  * Candidate or Friend — an individual not acting for any of the above.
  * If more than one type applies (e.g. VC intro to an existing client), the
    field is single-select today, so we pick the primary type and flag the row
    for Fiona's review with a note.

Precedence: VC > Existing Client > Company > referrer's history > LLM guess.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlparse

from .filters import domain_of

# Statuses that on their own mean "Cole ran a search for this client".
_ENGAGED_STATUSES = frozenset({"Closed", "Abandoned", "Canceled"})

# Personal / free-mail domains never identify an organization.
GENERIC_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "hey.com",
        "fastmail.com",
        "msn.com",
        "comcast.net",
    }
)

# Words dropped when comparing firm names, so "Sequoia Capital" == "Sequoia"
# and "Menlo Ventures" == "Menlo". Only used for exact normalized equality,
# never substring matching.
_FIRM_SUFFIXES = frozenset(
    {
        "capital",
        "ventures",
        "venture",
        "partners",
        "partner",
        "vc",
        "management",
        "fund",
        "funds",
        "investments",
        "investors",
        "equity",
        "group",
        "llc",
        "llp",
        "lp",
        "inc",
        "the",
        "co",
        "company",
        "corp",
        "corporation",
        "holdings",
        "hq",
        "technologies",
        "technology",
        "labs",
        "ai",
        "io",
    }
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm_name(s: str | None) -> str:
    """Normalize a firm/company name for equality checks."""
    if not s:
        return ""
    s = s.lower().replace("&", " and ")
    # "Eon.io" -> "eon io", "a16z" stays "a16z"
    tokens = [t for t in _NON_ALNUM.split(s) if t]
    kept = [t for t in tokens if t not in _FIRM_SUFFIXES]
    return " ".join(kept or tokens)


_FUND_WORDS = frozenset(
    {"ventures", "venture", "capital", "vc", "investors", "equity", "partners", "fund"}
)


def looks_like_fund(name: str | None) -> bool:
    """'Alpine Investors', 'Costanoa VC', 'PSG Equity' -> True."""
    if not name:
        return False
    return any(t in _FUND_WORDS for t in _NON_ALNUM.split(name.strip().lower()))


def norm_person(s: str | None) -> str:
    if not s:
        return ""
    return " ".join(t for t in _NON_ALNUM.split(s.lower()) if t)


def website_domain(url: str | None) -> str:
    """'https://www.sequoiacap.com/about' -> 'sequoiacap.com'."""
    if not url:
        return ""
    u = url.strip().lower()
    if "://" not in u:
        u = "http://" + u
    host = urlparse(u).hostname or ""
    return host[4:] if host.startswith("www.") else host


def email_org_domain(email: str | None) -> str:
    d = domain_of(email or "")
    if not d or d in GENERIC_EMAIL_DOMAINS:
        return ""
    return d


def _domain_keys(d: str) -> list[str]:
    """'mail.sequoiacap.com' -> ['mail.sequoiacap.com', 'sequoiacap.com']."""
    if not d:
        return []
    parts = d.split(".")
    keys = [d]
    if len(parts) > 2:
        keys.append(".".join(parts[-2:]))
    return keys


# ---------------------------------------------------------------------------
# Rows loaded from Airtable
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientRow:
    id: str
    name: str
    website: str | None = None


@dataclass(frozen=True)
class InvestorRow:
    id: str
    name: str
    created: str = ""  # ISO timestamp; oldest record wins when names collide


@dataclass(frozen=True)
class SearchRow:
    id: str
    client_ids: tuple[str, ...] = ()
    status: str | None = None
    outcome: str | None = None
    lead_date: date | None = None
    outcome_date: date | None = None
    kickoff_date: date | None = None
    close_date: date | None = None
    lead_source_individuals: tuple[str, ...] = ()
    lead_source_client_ids: tuple[str, ...] = ()
    lead_source_vc_ids: tuple[str, ...] = ()
    lead_source_type: str | None = None

    def engaged_on(self) -> date | None:
        """When this search counts as an engagement, or None if it never did."""
        if self.outcome != "Won" and (self.status or "") not in _ENGAGED_STATUSES:
            return None
        return self.outcome_date or self.kickoff_date or self.lead_date or self.close_date


@dataclass(frozen=True)
class ReferrerProfile:
    """What Cole's history says about a named referrer."""

    name: str  # canonical spelling as used in Airtable
    lead_count: int
    dominant_type: str | None
    dominant_type_share: float
    org_client_id: str | None
    vc_investor_id: str | None


@dataclass
class Resolution:
    lead_source_type: str
    lead_source_client_id: str | None
    lead_source_vc_investor_id: str | None
    lead_source_individual: str | None
    review_notes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return bool(self.review_notes)


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


class RelationshipIndex:
    """In-memory view of Clients, Investors and Searches used for resolution."""

    def __init__(
        self,
        *,
        clients: list[ClientRow],
        investors: list[InvestorRow],
        searches: list[SearchRow],
        hq_options: list[str] | None = None,
    ) -> None:
        self.hq_options = sorted(set(hq_options or []))
        self.clients = {c.id: c for c in clients}
        self.investors = {i.id: i for i in investors}

        self._client_by_norm: dict[str, str] = {}
        self._client_by_exact: dict[str, str] = {}
        self._client_by_domain: dict[str, str] = {}
        for c in clients:
            self._client_by_exact.setdefault(c.name.strip().lower(), c.id)
            n = norm_name(c.name)
            if n:
                self._client_by_norm.setdefault(n, c.id)
            d = website_domain(c.website)
            if d:
                self._client_by_domain.setdefault(d, c.id)

        # All investors by exact name. The Investors table holds thousands of
        # names (angels, corporates, "Salesforce"...), so fuzzy matching is only
        # done against firms that have actually sent Cole a VC lead (below).
        self._investor_by_exact: dict[str, str] = {}
        for inv in investors:
            if inv.name.strip():
                self._investor_by_exact.setdefault(inv.name.strip().lower(), inv.id)
        self._vc_investor_by_norm: dict[str, str] = {}
        # Every investor by normalized name, oldest record first.
        self._investor_by_norm_all: dict[str, str] = {}
        for inv in sorted(investors, key=lambda i: i.created or "~"):
            n = norm_name(inv.name)
            if n and len(n) >= 3:
                self._investor_by_norm_all.setdefault(n, inv.id)

        # Client records that history has used as a VC lead source.
        vc_counter: Counter[str] = Counter()
        # Client -> earliest engagement date.
        self._first_engaged: dict[str, date] = {}
        # Person -> list of (type, org_client_ids, vc_ids)
        person_rows: dict[str, list[SearchRow]] = defaultdict(list)
        self._person_spelling: dict[str, Counter[str]] = defaultdict(Counter)

        for s in searches:
            when = s.engaged_on()
            if when is not None:
                for cid in s.client_ids:
                    prev = self._first_engaged.get(cid)
                    if prev is None or when < prev:
                        self._first_engaged[cid] = when
            if s.lead_source_type == "VC":
                for cid in s.lead_source_client_ids:
                    if cid not in s.client_ids:
                        vc_counter[cid] += 1
            for vid in s.lead_source_vc_ids:
                inv = self.investors.get(vid)
                if inv and norm_name(inv.name):
                    self._vc_investor_by_norm.setdefault(norm_name(inv.name), vid)
            for person in s.lead_source_individuals:
                key = norm_person(person)
                if key:
                    person_rows[key].append(s)
                    self._person_spelling[key][person.strip()] += 1

        # Duplicate client rows are common ("AtoB" / "Atob", "Legion" / "Legion
        # Technologies"), so also track engagement per normalized name.
        self._first_engaged_by_norm: dict[str, tuple[date, str]] = {}
        for cid, when in self._first_engaged.items():
            c = self.clients.get(cid)
            n = norm_name(c.name) if c else ""
            if n:
                prev = self._first_engaged_by_norm.get(n)
                if prev is None or when < prev[0]:
                    self._first_engaged_by_norm[n] = (when, cid)

        self._vc_client_ids = {cid for cid, n in vc_counter.items() if n >= 2}
        # A client record used once as a VC source counts if its name is a known VC firm.
        for cid in vc_counter:
            c = self.clients.get(cid)
            if c and self.investor_for_name(c.name):
                self._vc_client_ids.add(cid)

        self._people: dict[str, ReferrerProfile] = {}
        for key, rows in person_rows.items():
            types = Counter((r.lead_source_type or "").strip() for r in rows if r.lead_source_type)
            dominant, dom_n = types.most_common(1)[0] if types else (None, 0)
            # The person's org = most common Lead Source client that isn't the
            # hiring client itself (for Company-type rows those coincide).
            org_counter: Counter[str] = Counter()
            vc_counter_p: Counter[str] = Counter()
            for r in rows:
                for cid in r.lead_source_client_ids:
                    c = self.clients.get(cid)
                    # Skip the hiring client and client records that are just
                    # the person's own name (e.g. a "Michelle Taite" row).
                    if cid in r.client_ids or (c and norm_person(c.name) == key):
                        continue
                    org_counter[cid] += 1
                for vid in r.lead_source_vc_ids:
                    vc_counter_p[vid] += 1
            org = org_counter.most_common(1)[0][0] if org_counter else None
            vc_inv = vc_counter_p.most_common(1)[0][0] if vc_counter_p else None
            if vc_inv is None and org is not None:
                org_row = self.clients.get(org)
                vc_inv = self.investor_for_name(org_row.name) if org_row else None
            spelling = self._person_spelling[key].most_common(1)[0][0]
            self._people[key] = ReferrerProfile(
                name=spelling,
                lead_count=len(rows),
                dominant_type=dominant,
                dominant_type_share=(dom_n / sum(types.values())) if types else 0.0,
                org_client_id=org,
                vc_investor_id=vc_inv,
            )

    # ----- lookups -----------------------------------------------------------

    def client_for(
        self, name: str | None = None, website: str | None = None, *, fuzzy: bool = True
    ) -> str | None:
        """Match a company to a Clients record: website domain, exact name, then
        (if `fuzzy`) normalized name ("Rogo AI" == "Rogo")."""
        d = website_domain(website)
        for k in _domain_keys(d):
            if k in self._client_by_domain:
                return self._client_by_domain[k]
        if name:
            exact = self._client_by_exact.get(name.strip().lower())
            if exact:
                return exact
            if fuzzy:
                n = norm_name(name)
                if n and n in self._client_by_norm:
                    return self._client_by_norm[n]
        return None

    def client_for_email(self, email: str | None) -> str | None:
        for k in _domain_keys(email_org_domain(email)):
            if k in self._client_by_domain:
                return self._client_by_domain[k]
        return None

    def investor_for_name(self, name: str | None, *, any_investor: bool = False) -> str | None:
        """Investor record for a firm that has sent Cole VC leads before
        ("Sequoia" == "Sequoia Capital"). With `any_investor`, also accept an
        exact name match anywhere in the Investors table."""
        n = norm_name(name)
        if n and n in self._vc_investor_by_norm:
            return self._vc_investor_by_norm[n]
        if any_investor and name:
            return self._investor_by_exact.get(name.strip().lower())
        return None

    def investor_match(self, name: str | None) -> str | None:
        """Existing Investors record for a firm name from research:
        exact name first, then normalized ("Sequoia" -> "Sequoia Capital")."""
        if not name or not name.strip():
            return None
        exact = self._investor_by_exact.get(name.strip().lower())
        if exact:
            return exact
        n = norm_name(name)
        if n in self._vc_investor_by_norm:
            return self._vc_investor_by_norm[n]
        return self._investor_by_norm_all.get(n) if n and len(n) >= 3 else None

    def is_vc_client(self, client_id: str | None) -> bool:
        return bool(client_id) and client_id in self._vc_client_ids

    def referrer(self, person: str | None) -> ReferrerProfile | None:
        return self._people.get(norm_person(person)) if person else None

    def was_engaged_before(self, client_id: str | None, lead_date: date) -> bool:
        if not client_id:
            return False
        first = self._first_engaged.get(client_id)
        return first is not None and first < lead_date

    def engaged_sibling_before(
        self, client_id: str | None, name: str | None, lead_date: date
    ) -> str | None:
        """A *different* client record with the same normalized name that was
        engaged before `lead_date` (duplicate rows like "Atob" vs "AtoB")."""
        n = norm_name(name or self.client_name(client_id))
        hit = self._first_engaged_by_norm.get(n) if n else None
        if hit and hit[0] < lead_date and hit[1] != client_id:
            return hit[1]
        return None

    def hint_lines(self, text: str) -> list[str]:
        """Human-readable lines for the LLM about known referrers in `text`."""
        lines = []
        for p in self.known_referrers_in(text):
            org = self.client_name(p.org_client_id) or "unknown org"
            typ = p.dominant_type or "unknown"
            lines.append(
                f"- {p.name}: works at {org}; {p.lead_count} prior leads to Cole, "
                f"usually tagged '{typ}'."
            )
        return lines

    def known_referrers_in(self, text: str, *, limit: int = 5) -> list[ReferrerProfile]:
        """Known referrers whose full name appears in `text` (for LLM hints)."""
        hay = " " + norm_person(text) + " "
        hits = [
            p
            for key, p in self._people.items()
            if " " in key and f" {key} " in hay  # full names only: "paul cho", not "paul"
        ]
        hits.sort(key=lambda p: -p.lead_count)
        return hits[:limit]

    def client_name(self, client_id: str | None) -> str | None:
        c = self.clients.get(client_id or "")
        return c.name if c else None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_lead_source(
    index: RelationshipIndex,
    *,
    llm_type: str,
    llm_individual: str | None,
    llm_company: str | None,
    referrer_email: str | None,
    hiring_client_id: str | None,
    hiring_client_name: str,
    hiring_website: str | None,
    lead_date: date,
) -> Resolution:
    reasons: list[str] = []
    review: list[str] = []

    profile = index.referrer(llm_individual)
    individual = profile.name if profile else llm_individual

    # --- Who does the referrer work for? -----------------------------------
    # Candidate orgs, strongest evidence first: email domain, the model's read
    # of the signature, then the org Cole's history links this person to.
    candidates: list[tuple[str, str]] = []
    email_org = index.client_for_email(referrer_email)
    if email_org:
        candidates.append((email_org, "referrer email domain"))
    if llm_company:
        cid = index.client_for(llm_company)
        if cid:
            candidates.append((cid, f"lead source company '{llm_company}'"))
    # History is the fallback only: people change jobs, so a current email
    # domain or signature beats where they worked on past leads.
    if profile and profile.org_client_id and not candidates:
        candidates.append(
            (profile.org_client_id, f"{profile.name}'s {profile.lead_count} prior leads")
        )
    seen: set[str] = set()
    me = norm_person(llm_individual)
    orgs = [
        (c, why)
        for c, why in candidates
        # a Client row named after the referrer themself isn't an org
        if norm_person(index.client_name(c)) != me and not (c in seen or seen.add(c))
    ]

    vc_orgs = [c for c, _ in orgs if index.is_vc_client(c)]
    org_id = vc_orgs[0] if vc_orgs else (orgs[0][0] if orgs else None)
    for c, why in orgs:
        if c == org_id:
            reasons.append(f"{why} -> {index.client_name(c)}")

    # VC investor record (for the "Lead Source (VC Only)" link)
    vc_inv_id: str | None = None
    if vc_orgs:
        vc_inv_id = index.investor_for_name(index.client_name(vc_orgs[0]))
    if vc_inv_id is None and llm_company:
        vc_inv_id = index.investor_for_name(llm_company)
    # History only decides VC when nothing fresher (email domain, signature)
    # places the referrer at a non-VC company today.
    fresh_non_vc = any(
        not index.is_vc_client(c) for c, why in orgs if not why.endswith("prior leads")
    )
    if vc_inv_id is None and profile and profile.dominant_type == "VC" and not fresh_non_vc:
        vc_inv_id = profile.vc_investor_id

    email_domain = email_org_domain(referrer_email)
    hiring_domain = website_domain(hiring_website)

    history_says_vc = bool(
        profile
        and profile.dominant_type == "VC"
        and profile.dominant_type_share >= 0.6
        and not fresh_non_vc
    )
    referrer_is_vc = bool(
        vc_orgs
        or vc_inv_id
        or history_says_vc
        # The model read a VC firm off the email (people change jobs, so this
        # beats older history when the firm is a known investor).
        or (
            llm_type == "VC"
            and llm_company
            and (
                not profile
                or index.investor_for_name(llm_company, any_investor=True)
                or looks_like_fund(llm_company)
            )
        )
    )
    referrer_at_hiring_co = bool(
        (hiring_client_id and any(c == hiring_client_id for c, _ in orgs))
        or (email_domain and hiring_domain and email_domain.endswith(hiring_domain))
        or (llm_company and norm_name(llm_company) == norm_name(hiring_client_name))
    )

    hiring_existing = index.was_engaged_before(hiring_client_id, lead_date)
    if not hiring_existing:
        sib = index.engaged_sibling_before(hiring_client_id, hiring_client_name, lead_date)
        if sib:
            hiring_existing = True
            review.append(
                "Existing Client based on a similarly named client record "
                f"'{index.client_name(sib)}' - check it's the same company (possible duplicate)."
            )
    existing_referrer_org = next(
        (
            c
            for c, _ in orgs
            if c != hiring_client_id
            and (
                index.was_engaged_before(c, lead_date)
                or index.engaged_sibling_before(c, None, lead_date)
            )
        ),
        None,
    )
    if referrer_is_vc:
        existing_referrer_org = None  # VC intros are VC only

    # --- Decide ------------------------------------------------------------
    if referrer_is_vc:
        final = "VC"
        reasons.append("referrer is at a VC firm")
        if hiring_existing:
            review.append(
                f"Also Existing Client: {hiring_client_name} has a prior Cole search. "
                "Tagged VC (single-select) - tag both once Lead Source Type is multi-select."
            )
    elif hiring_existing or existing_referrer_org:
        final = "Existing Client"
        if hiring_existing:
            reasons.append(f"{hiring_client_name} had a Cole search before {lead_date}")
        if existing_referrer_org:
            org_id = existing_referrer_org
            reasons.append(f"referrer's company {index.client_name(org_id)} is a past client")
    elif llm_type == "Existing Client":
        # The email itself suggests a past relationship but Airtable has no
        # Won/Closed/Abandoned/Canceled search before the lead date. Keep the
        # model's call, but ask a human.
        final = "Existing Client"
        review.append(
            f"Tagged Existing Client from the email, but no Won/Closed/Abandoned/Canceled "
            f"search for {hiring_client_name} before {lead_date} was found - "
            "possibly a duplicate client record."
        )
    elif referrer_at_hiring_co:
        final = "Company"
        reasons.append("referrer works at the hiring company")
    elif (
        profile
        and profile.dominant_type == "Candidate or Friend"
        and profile.dominant_type_share >= 0.6
        and profile.lead_count >= 2
        and llm_type != "VC"
    ):
        final = "Candidate or Friend"
        reasons.append(f"{profile.name}'s prior leads are mostly Candidate or Friend")
    else:
        final = llm_type
        reasons.append("kept the model's classification")

    if final == "Candidate or Friend" and looks_like_fund(llm_company):
        review.append(
            f"Referrer works at '{llm_company}', which looks like an investor - "
            "should this be VC (or VC + Candidate or Friend)?"
        )

    # Never write a type Airtable doesn't have as an option.
    if final not in {"VC", "Existing Client", "Company", "Candidate or Friend"}:
        review.append(f"Model suggested '{final}', which isn't a Lead Source Type option.")
        final = "Candidate or Friend" if llm_individual else "Company"

    # --- Lead Source link (Clients) ----------------------------------------
    lead_source_client = org_id
    if lead_source_client is None and final in {"Company", "Existing Client"}:
        lead_source_client = hiring_client_id

    # --- Sanity flags ------------------------------------------------------
    if index.investor_for_name(hiring_client_name):
        review.append(
            f"Hiring company '{hiring_client_name}' looks like an investor - "
            "check the client wasn't parsed as the referring VC."
        )
    if final != llm_type:
        reasons.append(f"overrode model's '{llm_type}'")

    if final == "VC" and vc_inv_id is None:
        # A firm new to Cole: link it if it's already in the Investors table.
        vc_inv_id = index.investor_for_name(
            llm_company or index.client_name(org_id), any_investor=True
        )

    return Resolution(
        lead_source_type=final,
        lead_source_client_id=lead_source_client,
        lead_source_vc_investor_id=vc_inv_id if final == "VC" else None,
        lead_source_individual=individual,
        review_notes=review,
        reasons=reasons,
    )
