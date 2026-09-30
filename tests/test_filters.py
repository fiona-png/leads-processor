"""Filter and forwarded-header parsing tests."""

from __future__ import annotations

import pytest

from cole_leads.filters import (
    domain_of,
    has_role_keyword,
    is_forward,
    is_internal,
    is_reply_not_forward,
    parse_forwarded_headers,
    should_process,
)


class TestDomain:
    def test_angles(self):
        assert domain_of("Jane <jane@accel.com>") == "accel.com"

    def test_bare(self):
        assert domain_of("jane@accel.com") == "accel.com"

    def test_empty(self):
        assert domain_of("no email here") == ""

    def test_is_internal_true(self):
        assert is_internal("matt@colellc.com") is True
        assert is_internal("Foo <bar@cole.co>") is True
        assert is_internal("baz@colegroup.com") is True

    def test_is_internal_false(self):
        assert is_internal("jane@accel.com") is False
        # near-miss domains must not match
        assert is_internal("jane@cole.com") is False


class TestForwardDetection:
    def test_fwd_subject(self):
        assert is_forward("Fwd: hello", "") is True
        assert is_forward("FW: hello", "") is True
        assert is_forward("Fw: hello", "") is True

    def test_marker_in_body(self):
        body = "fyi\n\n---------- Forwarded message ----------\nFrom: x"
        assert is_forward("hello", body) is True

    def test_outlook_marker(self):
        body = "Begin forwarded message:\nFrom: x"
        assert is_forward("hello", body) is True

    def test_no_signal(self):
        assert is_forward("hello", "just a chat") is False


class TestReplyDetection:
    def test_re_alone(self):
        assert is_reply_not_forward("Re: lunch") is True

    def test_fwd_re_is_not_reply(self):
        assert is_reply_not_forward("Fwd: Re: intro") is False

    def test_not_a_reply(self):
        assert is_reply_not_forward("intro") is False


class TestHasRoleKeyword:
    @pytest.mark.parametrize(
        "subject",
        [
            "Acme CMO opportunity",
            "vp sales search at Stripe portfolio co",  # the trailing-space "vp " token
            "VP Marketing role",
            "Head of Engineering at Globex",
            "Chief Revenue Officer wanted",
            "Director of Sales",
            "airops lead",
            "Pre-lead via former colleague/GC (Generus)",
            "Intro: Northwind",
            "President of NA",
            "SVP marketing search",
            "evp sales",
        ],
    )
    def test_match(self, subject):
        assert has_role_keyword(subject) is True

    @pytest.mark.parametrize(
        "subject",
        [
            "lunch tomorrow",
            "vpn outage",  # must not light up on "vpn" — "vp " requires trailing space
            "",
        ],
    )
    def test_no_match(self, subject):
        assert has_role_keyword(subject) is False


class TestShouldProcess:
    # --- existing fixture-driven cases ------------------------------------
    def test_real_forward_kept(self, gmail_vc_referral):
        subject, from_addr, _to, body = gmail_vc_referral
        assert should_process(subject, body, from_addr=from_addr) is True

    def test_outlook_forward_kept(self, outlook_candidate_referral):
        subject, from_addr, _to, body = outlook_candidate_referral
        assert should_process(subject, body, from_addr=from_addr) is True

    def test_internal_reply_dropped(self, internal_reply_skip):
        subject, from_addr, _to, body = internal_reply_skip
        assert should_process(subject, body, from_addr=from_addr) is False

    def test_fwd_re_kept(self, fwd_re_keep):
        subject, from_addr, _to, body = fwd_re_keep
        assert should_process(subject, body, from_addr=from_addr) is True

    # --- direct-email role-keyword acceptance (matches n8n behavior) ------
    def test_direct_email_with_role_keyword_kept(self):
        """A cold direct email naming a role is a real lead."""
        assert (
            should_process(
                "Acme CMO opportunity",
                "Hi — we'd love to chat about a CMO search.",
                from_addr="jane@accel.com",
            )
            is True
        )

    def test_vp_keyword_at_portfolio_co_kept(self):
        assert (
            should_process(
                "VP Sales search at Stripe portfolio co",
                "Reaching out about a VP Sales opening.",
                from_addr="partner@accel.com",
            )
            is True
        )

    def test_airops_lead_subject_kept(self):
        """Short internal note with the `lead` keyword in subject is kept."""
        assert (
            should_process(
                "airops lead",
                "fyi",
                from_addr="matt@colellc.com",
            )
            is True
        )

    def test_pre_lead_subject_kept(self):
        assert (
            should_process(
                "Pre-lead via former colleague/GC (Generus)",
                "Heads up — Generus might be a real one.",
                from_addr="gillian@colellc.com",
            )
            is True
        )

    # --- drops -----------------------------------------------------------
    def test_plain_re_reply_dropped(self):
        """A pure `Re:` with no forward indicator is dropped regardless of body."""
        assert (
            should_process(
                "Re: lunch tomorrow",
                "Sounds good!",
                from_addr="chloe@colellc.com",
            )
            is False
        )

    def test_short_internal_notes_are_kept(self):
        """Real leads from this week that the old short-note rule threw away."""
        assert should_process(
            "Simile CRO",
            "Sounds like this should be coming our way. Texting with Katie from Index",
            from_addr="geoff@colegroup.com",
        )
        assert should_process(
            "VP/CRO search for GTM tech (Battery Ventures)",
            "Lead came from Jenny at Battery. I'm full but anyone interested! I can intro!!",
            from_addr="david@colegroup.com",
        )
        assert should_process("Fiona will you attach", "", from_addr="matt@colegroup.com")

    def test_long_internal_note_with_no_signals_kept(self):
        """Same internal sender but >=200 chars of body → fall through to accept."""
        long_body = "x" * 250
        assert (
            should_process(
                "thoughts",
                long_body,
                from_addr="fiona@colegroup.com",
            )
            is True
        )

    def test_external_short_note_kept(self):
        """External sender with no forward / no keyword still passes — the
        short-note drop is internal-only."""
        assert (
            should_process(
                "hi",
                "small note",
                from_addr="someone@external.com",
            )
            is True
        )


class TestParseForwardedHeaders:
    def test_gmail_style(self, gmail_vc_referral):
        _subj, _f, _t, body = gmail_vc_referral
        h = parse_forwarded_headers(body)
        assert h.from_ == "Jane Investor <jane@accel.com>"
        assert h.subject == "Intro - Acme Robotics CRO search"
        assert h.to == "Matt Strand <matt@colellc.com>"
        assert h.date is not None
        assert "May 6, 2026" in h.date

    def test_outlook_style(self, outlook_candidate_referral):
        _subj, _f, _t, body = outlook_candidate_referral
        h = parse_forwarded_headers(body)
        assert h.from_ == "Sam Buyer <sam@northwind.io>"
        assert h.subject == "Looking for a VP Marketing at Northwind"
        assert h.to == "Fiona Cooper <fiona@colellc.com>"

    def test_outer_from_line_not_shadowing_inner(self):
        """A From: line in the user's forwarding note shouldn't beat the real inner one."""
        body = (
            "From: ignore-me (this is just a note)\n"
            "fyi\n\n"
            "---------- Forwarded message ----------\n"
            "From: Real Sender <real@elsewhere.com>\n"
            "Date: Mon, 5 May 2026 10:00:00 -0400\n"
            "Subject: the real subject\n"
            "To: Matt <matt@colellc.com>\n\n"
            "body body"
        )
        h = parse_forwarded_headers(body)
        assert h.from_ == "Real Sender <real@elsewhere.com>"
        assert h.subject == "the real subject"

    def test_no_marker_falls_back_to_whole_body(self):
        body = "From: Real <real@x.com>\nSubject: hi\n\nbody"
        h = parse_forwarded_headers(body)
        assert h.from_ == "Real <real@x.com>"
        assert h.subject == "hi"

    def test_empty_body(self):
        h = parse_forwarded_headers("")
        assert h.from_ is None and h.subject is None and h.date is None and h.to is None
