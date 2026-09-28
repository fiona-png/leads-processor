"""Tests for deterministic post-processing of the model's output."""

from __future__ import annotations

from datetime import date

import pytest

from cole_leads.derive import (
    finalize_lead,
    normalize_arr,
    normalize_hq,
    roles_from_title,
    search_type_for,
    seniority_from_title,
)
from cole_leads.models import CompanyResearch, Lead, ParsedEmail


def _lead(parsed=None, research=None) -> Lead:
    p = dict(
        client="Acme",
        role_title="VP of Sales",
        role="Sales",
        seniority="VP",
        lead_recipient="matt",
        lead_date=date(2026, 5, 1),
        lead_source_type="VC",
        lead_notes="n",
    )
    r = dict(biz_type="Enterprise", biz_arr=10.0, series="B", investors=[])
    p.update(parsed or {})
    r.update(research or {})
    return Lead(parsed=ParsedEmail(**p), research=CompanyResearch(**r))


@pytest.mark.parametrize(
    "arr,series,expected",
    [
        (4.2, "B", "Core"),  # Replicant: Series B but $4M ARR -> Core
        (15.9, "D", "Core"),  # Cyberhaven-like: late series, small ARR
        (16, "A", "Strategic"),
        (50.9, "C", "Strategic"),
        (51, "B", "Franchise"),
        (30, "Public", "Franchise"),
    ],
)
def test_search_type_uses_arr_not_series(arr, series, expected):
    assert search_type_for(arr, series)[0] == expected


def test_search_type_falls_back_to_series_with_note():
    st, note = search_type_for(None, "C")
    assert st == "Strategic" and note


@pytest.mark.parametrize(
    "title,roles,sen",
    [
        ("CRO", ["Sales"], "Chief"),
        ("VP of Marketing", ["Marketing"], "VP"),
        ("Head of Sales", ["Sales"], "Head"),
        ("COO / VP Sales", ["General Management", "Sales"], "Chief"),
        ("President", ["General Management"], "Chief"),
        ("SVP Customer Success", ["Customer Success"], "SVP"),
        ("Chief Customer Officer", ["Customer Success"], "Chief"),
        ("VP Revenue Operations", ["Sales Ops"], "VP"),
        ("VP Business Development", ["Sales"], "VP"),
        ("Head of Growth", ["Marketing"], "Head"),
    ],
)
def test_title_parsing(title, roles, sen):
    assert roles_from_title(title) == roles
    assert seniority_from_title(title) == sen


def test_title_overrides_wrong_model_seniority():
    """Outset / FileVine / Transit: title says CRO, model said VP."""
    lead, notes = finalize_lead(_lead(parsed={"role_title": "CRO", "seniority": "VP"}))
    assert lead.parsed.seniority == "Chief"
    assert any("Seniority" in n for n in notes)


def test_title_overrides_wrong_model_role():
    """Composio VPM was written as Sales."""
    lead, notes = finalize_lead(_lead(parsed={"role_title": "VP Marketing", "role": "Sales"}))
    assert lead.parsed.role == "Marketing"


def test_multi_role_title_keeps_both():
    lead, _ = finalize_lead(
        _lead(parsed={"role_title": "CMO / VP Sales", "role": "Marketing", "seniority": "Chief"})
    )
    assert [lead.parsed.role, *lead.parsed.additional_roles] == ["Marketing", "Sales"]


def test_arr_in_raw_dollars_converted():
    assert normalize_arr(12_000_000)[0] == 12.0


def test_high_arr_for_series_a_flagged():
    """Nue.io / Litify: Series A with $77M-$100M 'ARR'."""
    _, notes = finalize_lead(_lead(research={"biz_arr": 77, "series": "A"}))
    assert any("unusually high" in n for n in notes)


def test_arr_equal_to_funding_flagged():
    _, notes = finalize_lead(_lead(research={"biz_arr": 78.5, "total_funding_m": 78.57}))
    assert any("funding" in n for n in notes)


def test_hq_normalized_to_existing_option():
    assert normalize_hq("New York City", ["New York"]) == "New York"
    assert normalize_hq("Austin, Texas", ["Austin"]) == "Austin"
    assert normalize_hq("san mateo", ["San Mateo"]) == "San Mateo"


def test_search_type_from_model_ignored():
    lead, _ = finalize_lead(
        _lead(research={"biz_arr": 4.2, "series": "D", "search_type": "Strategic"})
    )
    assert lead.research.search_type == "Core"
