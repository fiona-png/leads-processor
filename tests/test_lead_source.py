"""Unit tests for the relationship-aware Lead Source resolver."""

from __future__ import annotations

from datetime import date

from cole_leads.lead_source import (
    ClientRow,
    InvestorRow,
    RelationshipIndex,
    SearchRow,
    looks_like_fund,
    resolve_lead_source,
)

SEQ = ClientRow(id="recSEQ", name="Sequoia Capital", website="sequoiacap.com")
ACME = ClientRow(id="recACME", name="Acme", website="acme.com")
ACME_DUP = ClientRow(id="recACME2", name="Acme Inc")
HAUS = ClientRow(id="recHAUS", name="Haus", website="haus.io")
NEWCO = ClientRow(id="recNEW", name="NewCo", website="newco.ai")


def _vc_history(person: str = "Paul Cho", n: int = 4) -> list[SearchRow]:
    return [
        SearchRow(
            id=f"recV{i}",
            client_ids=(f"recP{i}",),
            lead_date=date(2025, 1, i + 1),
            lead_source_individuals=(person,),
            lead_source_client_ids=("recSEQ",),
            lead_source_vc_ids=("invSEQ",),
            lead_source_type="VC",
        )
        for i in range(n)
    ]


def _won(client_id: str, when: date = date(2024, 1, 1)) -> SearchRow:
    return SearchRow(
        id=f"recW{client_id}",
        client_ids=(client_id,),
        status="Closed",
        outcome="Won",
        lead_date=when,
    )


def _index(searches: list[SearchRow]) -> RelationshipIndex:
    return RelationshipIndex(
        clients=[SEQ, ACME, ACME_DUP, HAUS, NEWCO],
        investors=[InvestorRow(id="invSEQ", name="Sequoia Capital")],
        searches=searches,
    )


def _resolve(idx: RelationshipIndex, **kw):
    base = dict(
        llm_type="Candidate or Friend",
        llm_individual=None,
        llm_company=None,
        referrer_email=None,
        hiring_client_id="recNEW",
        hiring_client_name="NewCo",
        hiring_website="newco.ai",
        lead_date=date(2026, 9, 1),
    )
    base.update(kw)
    return resolve_lead_source(idx, **base)


def test_known_vc_person_is_vc_even_from_gmail():
    r = _resolve(
        _index(_vc_history()), llm_individual="Paul Cho", referrer_email="paulcho@gmail.com"
    )
    assert r.lead_source_type == "VC"
    assert r.lead_source_client_id == "recSEQ"
    assert r.lead_source_vc_investor_id == "invSEQ"


def test_vc_email_domain_is_vc():
    r = _resolve(
        _index(_vc_history(person="Someone Else")),
        llm_individual="New Person",
        referrer_email="New Person <np@sequoiacap.com>",
    )
    assert r.lead_source_type == "VC"


def test_fresh_employer_beats_old_vc_history():
    """Person used to intro from Sequoia, now emails from Haus (a past client)."""
    idx = _index([*_vc_history(), _won("recHAUS")])
    r = _resolve(idx, llm_individual="Paul Cho", referrer_email="paul@haus.io")
    assert r.lead_source_type == "Existing Client"
    assert r.lead_source_client_id == "recHAUS"


def test_referrer_company_past_client_is_existing_client():
    r = _resolve(_index([_won("recHAUS")]), llm_individual="Gillian", llm_company="Haus")
    assert r.lead_source_type == "Existing Client"


def test_duplicate_client_row_counts_but_is_flagged():
    r = _resolve(
        _index([_won("recACME")]),
        llm_type="Company",
        hiring_client_id="recACME2",
        hiring_client_name="Acme Inc",
        hiring_website=None,
    )
    assert r.lead_source_type == "Existing Client"
    assert any("similarly named" in n for n in r.review_notes)


def test_model_existing_client_without_history_is_kept_and_flagged():
    r = _resolve(_index([]), llm_type="Existing Client")
    assert r.lead_source_type == "Existing Client"
    assert r.needs_review


def test_friend_at_fund_is_flagged():
    r = _resolve(_index([]), llm_individual="Pat", llm_company="Alpine Investors")
    assert r.lead_source_type == "Candidate or Friend"
    assert any("investor" in n for n in r.review_notes)


def test_looks_like_fund():
    assert looks_like_fund("Costanoa VC")
    assert looks_like_fund("PSG Equity")
    assert not looks_like_fund("Acme Robotics")


def test_vc_intro_to_existing_client_is_existing_client():
    """Simile: Index intro, but Simile already hired Cole -> Existing Client + note."""
    idx = _index([*_vc_history(), _won("recNEW", date(2026, 8, 1))])
    r = _resolve(idx, llm_individual="Paul Cho", referrer_email="paul@sequoiacap.com")
    assert r.lead_source_type == "Existing Client"
    assert r.lead_source_client_id == "recNEW"
    assert any("Also a VC intro" in n for n in r.review_notes)


def test_latest_prior_search_matches_same_name_client_rows():
    prior = SearchRow(
        id="recHOM",
        name="Acme HOM",
        client_ids=("recACME",),
        status="Kickoff",
        outcome="Won",
        lead_date=date(2026, 8, 1),
        biz_arr=20,
    )
    idx = _index([prior])
    got = idx.latest_prior_search("recACME", "Acme", date(2026, 9, 30))
    assert got is not None and got.id == "recHOM"
    assert idx.latest_prior_search("recACME", "Acme", date(2026, 7, 1)) is None
