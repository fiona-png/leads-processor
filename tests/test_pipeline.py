"""Tests for the pipeline orchestrator. Gmail / Airtable / LLM are all stubs."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock

from cole_leads.lead_source import ClientRow, InvestorRow, RelationshipIndex, SearchRow
from cole_leads.models import (
    CompanyResearch,
    Lead,
    ParsedEmail,
    RawEmail,
)
from cole_leads.pipeline import process_one_lead, run

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _raw_email(**overrides: Any) -> RawEmail:
    base = dict(
        message_id="msg-abc",
        thread_id="thr-1",
        subject="Fwd: Intro - Helios Energy VP Marketing",
        from_addr="matt@colellc.com",
        to_addr="leads@colellc.com",
        received_at=date(2026, 5, 11),
        body_text=(
            "Sending to leads@.\n\n"
            "---------- Forwarded message ----------\n"
            "From: Maria <maria@brexbros.vc>\n"
            "Date: Mon, May 11, 2026 at 8:55 AM\n"
            "Subject: Intro - Helios Energy\n"
            "To: Matt <matt@colellc.com>\n\n"
            "Series A Boulder utility-grid SaaS."
        ),
    )
    base.update(overrides)
    return RawEmail(**base)


def _lead(**overrides: Any) -> Lead:
    p_overrides = overrides.pop("parsed", {})
    r_overrides = overrides.pop("research", {})
    parsed = ParsedEmail(
        **{
            "client": "Helios Energy",
            "role": "Marketing",
            "seniority": "VP",
            "lead_recipient": "matt",
            "lead_date": date(2026, 5, 11),
            "lead_source_individual": "Maria Operator",
            "lead_source_company": "Brexbros Ventures",
            "lead_source_type": "VC",
            "lead_notes": "Series A Boulder utility-grid SaaS, hiring first VPM.",
            **p_overrides,
        }
    )
    research = CompanyResearch(
        **{
            "biz_type": "Enterprise",
            "biz_arr": 4.0,
            "company_hq": "Boulder",
            "series": "A",
            "search_type": "Core",
            "investors": ["Brexbros Ventures"],
            "website": "https://helios.energy",
            **r_overrides,
        }
    )
    return Lead(parsed=parsed, research=research)


def _airtable_mock(**defaults: Any) -> MagicMock:
    """A mock with sensible defaults for the happy path."""
    m = MagicMock()
    m.search_exists_for_message_id.return_value = defaults.get("exists", False)
    m.upsert_client.return_value = defaults.get("client_id", "recCLIENT_NEW")
    m.find_or_create_investor.return_value = "recINV_DEFAULT"
    m.find_or_create_investor.side_effect = None
    m.find_client_by_name.return_value = defaults.get("find_client", None)
    m.has_closed_searches_for_client.return_value = defaults.get("has_closes", False)
    m.create_search.return_value = defaults.get("search_id", "recSEARCH_NEW")
    m.load_relationship_index.return_value = defaults.get(
        "index", RelationshipIndex(clients=[], investors=[], searches=[])
    )
    return m


def _gmail_mock() -> MagicMock:
    return MagicMock()


def _llm_mock(lead: Lead | None = None) -> MagicMock:
    m = MagicMock()
    m.parse_and_research.return_value = lead if lead is not None else _lead()
    return m


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_returns_created(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        # Two distinct investors so we exercise the loop.
        airtable.find_or_create_investor.side_effect = ["recACCEL", "recSEQ"]
        llm = _llm_mock(_lead(research={"investors": ["Accel", "Sequoia"]}))

        result = process_one_lead(_raw_email(), gmail=gmail, airtable=airtable, llm=llm)

        assert result.status == "created"
        assert result.search_record_id == "recSEARCH_NEW"
        assert result.client_record_id == "recCLIENT_NEW"
        assert result.error is None
        assert result.lead is not None

    def test_calls_airtable_in_expected_order(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        airtable.find_or_create_investor.side_effect = ["recACCEL", "recSEQ"]
        llm = _llm_mock(_lead(research={"investors": ["Accel", "Sequoia"]}))

        process_one_lead(_raw_email(), gmail=gmail, airtable=airtable, llm=llm)

        # Verify the sequence of calls on the airtable mock.
        method_calls = [c[0] for c in airtable.method_calls]
        # idempotency check, then client, then investors, then link, then lookup,
        # then closes check, then create_search.
        assert method_calls[0] == "search_exists_for_message_id"
        assert method_calls[1] == "load_relationship_index"
        assert "upsert_client" in method_calls
        assert method_calls.count("find_or_create_investor") == 2
        assert "link_investors_to_client" in method_calls
        assert method_calls.index("link_investors_to_client") > method_calls.index(
            "find_or_create_investor"
        )
        assert method_calls[-1] == "create_search"

    def test_marks_processed_in_gmail_on_success(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        llm = _llm_mock()

        process_one_lead(_raw_email(), gmail=gmail, airtable=airtable, llm=llm)

        gmail.mark_processed.assert_called_once_with("msg-abc")
        gmail.mark_failed.assert_not_called()

    def test_search_record_carries_resolved_ids(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock(client_id="recHELIOS")
        airtable.find_or_create_investor.side_effect = ["recBREX"]
        llm = _llm_mock()

        process_one_lead(_raw_email(), gmail=gmail, airtable=airtable, llm=llm)

        # Inspect the SearchRecord passed to create_search.
        sent = airtable.create_search.call_args.args[0]
        assert sent.client_record_id == "recHELIOS"
        assert sent.lead_recipient_record_id is not None  # matt is in TEAM_MAP
        assert sent.gmail_message_id == "msg-abc"
        assert sent.search_name == "Helios Energy VPM"
        assert sent.lead_source_type == "VC"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_skipped_duplicate_when_search_exists(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock(exists=True)
        llm = _llm_mock()

        result = process_one_lead(_raw_email(), gmail=gmail, airtable=airtable, llm=llm)

        assert result.status == "skipped_duplicate"
        assert result.search_record_id is None
        # No LLM call, no Airtable writes.
        llm.parse_and_research.assert_not_called()
        airtable.upsert_client.assert_not_called()
        airtable.create_search.assert_not_called()
        # Gmail mark_processed still called so the message doesn't keep coming back.
        gmail.mark_processed.assert_called_once_with("msg-abc")


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


class TestFilter:
    def test_skipped_when_not_a_forward(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        llm = _llm_mock()

        # A bare "Re:" with no forward marker → drops at the filter.
        raw = _raw_email(subject="Re: lunch tomorrow", body_text="Sounds good!")
        result = process_one_lead(raw, gmail=gmail, airtable=airtable, llm=llm)

        assert result.status == "skipped_filter"
        llm.parse_and_research.assert_not_called()
        airtable.upsert_client.assert_not_called()
        gmail.mark_processed.assert_called_once_with("msg-abc")


# ---------------------------------------------------------------------------
# LLM says "this isn't a lead"
# ---------------------------------------------------------------------------


class TestLLMReturnsNoLead:
    def test_client_none_skips_without_airtable_writes(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        # LLM returns a Lead with client=None — i.e., "this email wasn't actually a lead".
        llm = _llm_mock(_lead(parsed={"client": None}))

        result = process_one_lead(_raw_email(), gmail=gmail, airtable=airtable, llm=llm)

        assert result.status == "skipped_no_lead"
        assert result.search_record_id is None
        assert result.client_record_id is None
        # No Airtable writes at all — we should bail before touching them.
        airtable.upsert_client.assert_not_called()
        airtable.find_or_create_investor.assert_not_called()
        airtable.create_search.assert_not_called()
        # But we still mark the message processed so it doesn't keep coming back.
        gmail.mark_processed.assert_called_once_with("msg-abc")
        gmail.mark_failed.assert_not_called()

    def test_client_none_in_dry_run_does_not_label_gmail(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        llm = _llm_mock(_lead(parsed={"client": None}))

        result = process_one_lead(
            _raw_email(), gmail=gmail, airtable=airtable, llm=llm, dry_run=True
        )

        assert result.status == "skipped_no_lead"
        assert result.dry_run is True
        gmail.mark_processed.assert_not_called()
        airtable.upsert_client.assert_not_called()

    def test_run_summary_counts_skipped_no_lead(self):
        emails = [_raw_email(message_id="nope-1"), _raw_email(message_id="ok-1")]
        gmail = _gmail_mock()
        gmail.fetch_unprocessed_leads.return_value = emails
        airtable = _airtable_mock()
        llm = MagicMock()
        llm.parse_and_research.side_effect = [
            _lead(parsed={"client": None}),  # first one isn't really a lead
            _lead(),  # second is fine
        ]

        summary = run(gmail=gmail, airtable=airtable, llm=llm, max_leads=10)

        assert summary.total == 2
        assert summary.skipped_no_lead == 1
        assert summary.created == 1
        assert summary.failed == 0


# ---------------------------------------------------------------------------
# LLM failure
# ---------------------------------------------------------------------------


class TestLLMFailure:
    def test_marks_failed_and_returns_status_failed(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        llm = MagicMock()
        llm.parse_and_research.side_effect = RuntimeError("anthropic 500")

        result = process_one_lead(_raw_email(), gmail=gmail, airtable=airtable, llm=llm)

        assert result.status == "failed"
        assert "anthropic 500" in result.error
        airtable.upsert_client.assert_not_called()
        gmail.mark_failed.assert_called_once()
        gmail.mark_processed.assert_not_called()


# ---------------------------------------------------------------------------
# Airtable failure
# ---------------------------------------------------------------------------


class TestAirtableFailure:
    def test_create_search_failure_marks_failed(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        airtable.create_search.side_effect = RuntimeError("airtable 500")
        llm = _llm_mock()

        result = process_one_lead(_raw_email(), gmail=gmail, airtable=airtable, llm=llm)

        assert result.status == "failed"
        assert "airtable 500" in result.error
        gmail.mark_failed.assert_called_once()
        gmail.mark_processed.assert_not_called()

    def test_mid_flight_failure_does_not_rollback_earlier_writes(self):
        """Documented behavior: investor/client writes that succeeded before a
        failed create_search ARE NOT rolled back. The Search row is the
        idempotency anchor, so a retry safely re-converges."""
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        airtable.find_or_create_investor.side_effect = ["recINV1"]
        airtable.create_search.side_effect = RuntimeError("boom on create_search")
        llm = _llm_mock(_lead(research={"investors": ["Accel"]}))

        result = process_one_lead(_raw_email(), gmail=gmail, airtable=airtable, llm=llm)

        assert result.status == "failed"
        # Confirm earlier writes happened and were not undone.
        airtable.upsert_client.assert_called_once()
        airtable.find_or_create_investor.assert_called_once_with("Accel")
        airtable.link_investors_to_client.assert_called_once()


# ---------------------------------------------------------------------------
# Investor handling
# ---------------------------------------------------------------------------


class TestInvestorHandling:
    def test_empty_investor_list_skips_link(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        llm = _llm_mock(_lead(research={"investors": []}))

        process_one_lead(_raw_email(), gmail=gmail, airtable=airtable, llm=llm)

        airtable.find_or_create_investor.assert_not_called()
        airtable.link_investors_to_client.assert_not_called()


# ---------------------------------------------------------------------------
# Lead source resolution
# ---------------------------------------------------------------------------


def _index(**kw: Any) -> RelationshipIndex:
    return RelationshipIndex(
        clients=kw.get("clients", []),
        investors=kw.get("investors", []),
        searches=kw.get("searches", []),
    )


_BREXBROS = ClientRow(id="recBREXBROS", name="Brexbros Ventures", website="brexbros.vc")
_BREXBROS_INV = InvestorRow(id="invBREXBROS", name="Brexbros Ventures")


class TestLeadSourceResolution:
    def test_resolved_to_existing_client_record(self):
        airtable = _airtable_mock(
            client_id="recHELIOS",
            index=_index(clients=[_BREXBROS], investors=[_BREXBROS_INV]),
        )

        process_one_lead(_raw_email(), gmail=_gmail_mock(), airtable=airtable, llm=_llm_mock())

        sent = airtable.create_search.call_args.args[0]
        assert sent.lead_source_company_record_id == "recBREXBROS"
        assert sent.lead_source_vc_investor_id == "invBREXBROS"
        assert sent.lead_source_type == "VC"

    def test_fallback_to_self_when_company_type_and_no_source_record(self):
        airtable = _airtable_mock(client_id="recHELIOS")
        llm = _llm_mock(_lead(parsed={"lead_source_type": "Company", "lead_source_company": None}))

        process_one_lead(_raw_email(), gmail=_gmail_mock(), airtable=airtable, llm=llm)

        sent = airtable.create_search.call_args.args[0]
        assert sent.lead_source_company_record_id == "recHELIOS"

    def test_no_lead_source_company_no_link(self):
        airtable = _airtable_mock()
        llm = _llm_mock(
            _lead(parsed={"lead_source_company": None, "lead_source_type": "Candidate or Friend"})
        )

        process_one_lead(_raw_email(), gmail=_gmail_mock(), airtable=airtable, llm=llm)

        sent = airtable.create_search.call_args.args[0]
        assert sent.lead_source_company_record_id is None
        assert sent.lead_source_type == "Candidate or Friend"

    def test_known_vc_referrer_overrides_candidate_or_friend(self):
        """The Paul Cho case: model says friend, Cole's history says Sequoia / VC."""
        seq = ClientRow(id="recSEQ", name="Sequoia Capital", website="sequoiacap.com")
        history = [
            SearchRow(
                id=f"recS{i}",
                client_ids=(f"recPORT{i}",),
                lead_date=date(2025, 1, i + 1),
                lead_source_individuals=("Paul Cho",),
                lead_source_client_ids=("recSEQ",),
                lead_source_vc_ids=("invSEQ",) if i == 0 else (),
                lead_source_type="VC",
            )
            for i in range(5)
        ]
        airtable = _airtable_mock(
            index=_index(
                clients=[seq],
                investors=[InvestorRow(id="invSEQ", name="Sequoia")],
                searches=history,
            )
        )
        llm = _llm_mock(
            _lead(
                parsed={
                    "lead_source_individual": "paul cho",
                    "lead_source_company": None,
                    "lead_source_type": "Candidate or Friend",
                }
            )
        )
        email = _raw_email(body_text="Paul Cho from Sequoia wanted to intro Helios Energy.")

        process_one_lead(email, gmail=_gmail_mock(), airtable=airtable, llm=llm)

        sent = airtable.create_search.call_args.args[0]
        assert sent.lead_source_type == "VC"
        assert sent.lead_source_company_record_id == "recSEQ"
        assert sent.lead_source_vc_investor_id == "invSEQ"
        assert sent.lead_source_individual == "Paul Cho"  # canonical spelling
        hints = llm.parse_and_research.call_args.kwargs["relationship_hints"]
        assert hints and "Paul Cho" in hints[0] and "Sequoia Capital" in hints[0]


# ---------------------------------------------------------------------------
# Existing-client override
# ---------------------------------------------------------------------------


class TestExistingClientOverride:
    @staticmethod
    def _history(**kw: Any) -> RelationshipIndex:
        helios = ClientRow(id="recHELIOS", name="Helios Energy", website="https://helios.energy")
        prior = SearchRow(
            id="recOLD",
            client_ids=("recHELIOS",),
            status=kw.get("status", "Closed"),
            outcome=kw.get("outcome", "Won"),
            lead_date=kw.get("when", date(2024, 3, 1)),
        )
        return _index(
            clients=[helios, *kw.get("extra_clients", [])],
            investors=kw.get("investors", []),
            searches=[prior],
        )

    def test_prior_closes_force_existing_client(self):
        airtable = _airtable_mock(index=self._history())
        llm = _llm_mock(_lead(parsed={"lead_source_type": "Company", "lead_source_company": None}))

        process_one_lead(_raw_email(), gmail=_gmail_mock(), airtable=airtable, llm=llm)

        sent = airtable.create_search.call_args.args[0]
        assert sent.lead_source_type == "Existing Client"
        assert sent.client_record_id == "recHELIOS"  # matched by website, no new client
        airtable.upsert_client.assert_not_called()

    def test_vc_intro_to_existing_client_stays_vc_and_flags_review(self):
        airtable = _airtable_mock(
            index=self._history(extra_clients=[_BREXBROS], investors=[_BREXBROS_INV])
        )

        process_one_lead(_raw_email(), gmail=_gmail_mock(), airtable=airtable, llm=_llm_mock())

        sent = airtable.create_search.call_args.args[0]
        assert sent.lead_source_type == "VC"
        assert sent.needs_review is True
        assert "Existing Client" in (sent.review_notes or "")

    def test_engagement_after_lead_date_does_not_count(self):
        airtable = _airtable_mock(index=self._history(when=date(2027, 1, 1)))
        llm = _llm_mock(_lead(parsed={"lead_source_type": "Company", "lead_source_company": None}))
        process_one_lead(_raw_email(), gmail=_gmail_mock(), airtable=airtable, llm=llm)
        assert airtable.create_search.call_args.args[0].lead_source_type == "Company"

    def test_passed_lead_is_not_an_engagement(self):
        airtable = _airtable_mock(index=self._history(status="Pass", outcome="Pass"))
        llm = _llm_mock(_lead(parsed={"lead_source_type": "Company", "lead_source_company": None}))
        process_one_lead(_raw_email(), gmail=_gmail_mock(), airtable=airtable, llm=llm)
        assert airtable.create_search.call_args.args[0].lead_source_type == "Company"

    def test_no_prior_closes_uses_llm_classification(self):
        airtable = _airtable_mock()
        process_one_lead(_raw_email(), gmail=_gmail_mock(), airtable=airtable, llm=_llm_mock())
        assert airtable.create_search.call_args.args[0].lead_source_type == "VC"


# ---------------------------------------------------------------------------
# Lead recipient resolution
# ---------------------------------------------------------------------------


class TestRecipientResolution:
    def test_known_name_resolved(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        llm = _llm_mock(_lead(parsed={"lead_recipient": "matt"}))

        process_one_lead(_raw_email(), gmail=gmail, airtable=airtable, llm=llm)

        sent = airtable.create_search.call_args.args[0]
        assert sent.lead_recipient_record_id is not None
        # The exact value is the team-map ID; we just check it's resolved.

    def test_unknown_name_omitted_not_crashed(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        llm = _llm_mock(_lead(parsed={"lead_recipient": "nobody-known"}))

        result = process_one_lead(_raw_email(), gmail=gmail, airtable=airtable, llm=llm)

        assert result.status == "created"
        sent = airtable.create_search.call_args.args[0]
        assert sent.lead_recipient_record_id is None


# ---------------------------------------------------------------------------
# Dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_skips_writes_and_gmail_label(self):
        gmail = _gmail_mock()
        airtable = _airtable_mock()
        llm = _llm_mock()

        result = process_one_lead(
            _raw_email(), gmail=gmail, airtable=airtable, llm=llm, dry_run=True
        )

        # LLM called regardless (the whole point of dry-run).
        llm.parse_and_research.assert_called_once()
        # Reads happen, writes don't.
        airtable.search_exists_for_message_id.assert_called_once()
        airtable.upsert_client.assert_not_called()
        airtable.find_or_create_investor.assert_not_called()
        airtable.link_investors_to_client.assert_not_called()
        airtable.create_search.assert_not_called()
        gmail.mark_processed.assert_not_called()
        gmail.mark_failed.assert_not_called()
        # Status is "created" so the operator can see what would land.
        assert result.status == "created"
        assert result.dry_run is True


# ---------------------------------------------------------------------------
# run() aggregator
# ---------------------------------------------------------------------------


class TestRun:
    def test_aggregates_across_mixed_outcomes(self):
        # Three emails: one will be a duplicate, one a forward (created), one filtered (Re:).
        emails = [
            _raw_email(message_id="dup-1"),
            _raw_email(message_id="ok-1"),
            _raw_email(
                message_id="filt-1",
                subject="Re: lunch tomorrow",
                body_text="Sounds good!",
            ),
        ]
        gmail = _gmail_mock()
        gmail.fetch_unprocessed_leads.return_value = emails

        # Idempotency for dup-1 only.
        airtable = _airtable_mock()
        airtable.search_exists_for_message_id.side_effect = lambda mid: mid == "dup-1"
        llm = _llm_mock()

        summary = run(gmail=gmail, airtable=airtable, llm=llm, max_leads=10)

        assert summary.total == 3
        assert summary.created == 1
        assert summary.skipped_duplicate == 1
        assert summary.skipped_filter == 1
        assert summary.failed == 0

    def test_continues_after_per_lead_failure(self):
        good = _raw_email(message_id="ok-1")
        bad = _raw_email(message_id="bad-1")
        gmail = _gmail_mock()
        gmail.fetch_unprocessed_leads.return_value = [bad, good]

        airtable = _airtable_mock()
        llm = MagicMock()
        # First call raises, second succeeds.
        llm.parse_and_research.side_effect = [RuntimeError("first one bombed"), _lead()]

        summary = run(gmail=gmail, airtable=airtable, llm=llm, max_leads=10)

        assert summary.total == 2
        assert summary.failed == 1
        assert summary.created == 1
        # The good one still went through Airtable.
        assert airtable.create_search.call_count == 1


# ---------------------------------------------------------------------------
# Fixture loader (used by --fixture CLI)
# ---------------------------------------------------------------------------


class TestFixtureLoader:
    def test_loads_subject_from_to_body(self, tmp_path):
        from cole_leads.pipeline import _load_fixture_email

        fixture = tmp_path / "fx.txt"
        fixture.write_text(
            "Subject: Fwd: hi there\n"
            "From: matt@colellc.com\n"
            "To: leads@colellc.com\n"
            "Date: Mon, 11 May 2026 09:14:11 -0400\n"
            "\n"
            "body text here"
        )
        email = _load_fixture_email(fixture)

        assert email.subject == "Fwd: hi there"
        assert email.from_addr == "matt@colellc.com"
        assert email.to_addr == "leads@colellc.com"
        assert email.body_text.strip() == "body text here"
        assert email.received_at == date(2026, 5, 11)
        assert email.message_id == "fixture-fx"


# ---------------------------------------------------------------------------
# Ensure we don't accidentally lose pytest collection if a top-level import drifts
# ---------------------------------------------------------------------------


def test_pipeline_module_exports_expected_surface():
    from cole_leads import pipeline

    assert hasattr(pipeline, "process_one_lead")
    assert hasattr(pipeline, "run")
    assert hasattr(pipeline, "main")


class TestClaudeLeadCheck:
    def test_always_filled_ok_when_clean(self):
        airtable = _airtable_mock()
        process_one_lead(_raw_email(), gmail=_gmail_mock(), airtable=airtable, llm=_llm_mock())
        sent = airtable.create_search.call_args.args[0]
        assert sent.claude_check and (
            sent.claude_check.startswith("OK - checked on entry") or ":" in sent.claude_check
        )

    def test_review_notes_go_into_claude_check(self):
        airtable = _airtable_mock()
        llm = _llm_mock(_lead(parsed={"role_title": "CRO", "seniority": "VP"}))
        process_one_lead(_raw_email(), gmail=_gmail_mock(), airtable=airtable, llm=llm)
        sent = airtable.create_search.call_args.args[0]
        assert "Seniority" in sent.claude_check
