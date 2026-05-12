"""Pydantic model validation tests."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from cole_leads.models import CompanyResearch, Lead, ParsedEmail, SearchRecord


def _parsed(**overrides) -> ParsedEmail:
    base: dict = dict(
        client="Acme Robotics",
        role="Sales",
        seniority="Chief",
        lead_recipient="matt",
        lead_date=date(2026, 5, 6),
        lead_source_individual="Jane Investor",
        lead_source_company="Accel",
        lead_source_type="VC",
        lead_notes="Series B SF robotics co hiring first CRO.",
    )
    base.update(overrides)
    return ParsedEmail(**base)


def _research(**overrides) -> CompanyResearch:
    base: dict = dict(
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


class TestParsedEmail:
    def test_valid(self):
        p = _parsed()
        assert p.client == "Acme Robotics"
        assert p.lead_date == date(2026, 5, 6)

    def test_role_must_be_in_enum(self):
        with pytest.raises(ValidationError):
            _parsed(role="CEO")  # not in the role enum

    def test_seniority_must_be_in_enum(self):
        with pytest.raises(ValidationError):
            _parsed(seniority="C-Suite")

    def test_lead_source_individual_optional(self):
        p = _parsed(lead_source_individual=None, lead_source_company=None)
        assert p.lead_source_individual is None

    def test_extra_field_forbidden(self):
        with pytest.raises(ValidationError):
            ParsedEmail(
                client="X",
                role="Sales",
                seniority="VP",
                lead_recipient="matt",
                lead_date=date(2026, 5, 6),
                lead_source_type="Company",
                lead_notes="...",
                bogus_extra="nope",
            )


class TestCompanyResearch:
    def test_valid(self):
        r = _research()
        assert r.biz_arr == 12.0
        assert r.investors == ["Accel"]

    def test_arr_can_be_none(self):
        r = _research(biz_arr=None)
        assert r.biz_arr is None

    def test_series_must_be_in_enum(self):
        with pytest.raises(ValidationError):
            _research(series="Series Q")


class TestLead:
    def test_compose(self):
        lead = Lead(parsed=_parsed(), research=_research())
        assert lead.parsed.client == "Acme Robotics"
        assert lead.research.series == "B"


class TestSearchRecord:
    def test_defaults(self):
        r = SearchRecord(
            search_name="Acme Robotics CRO",
            client_record_id="recCLIENT",
            lead_recipient_record_id="recMATT",
            lead_source_type="VC",
            lead_date=date(2026, 5, 6),
            lead_notes="...",
            role="Sales",
            seniority="Chief",
            biz_type="Enterprise",
            series="B",
            search_type="Core",
            gmail_message_id="abc123",
        )
        assert r.status == "Qualified"
        assert r.outcome == "Open"
        assert r.open_flag is True
        assert r.investor_record_ids == []
