"""Filter and forwarded-header parsing tests."""

from __future__ import annotations

from cole_leads.filters import (
    domain_of,
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


class TestShouldProcess:
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
