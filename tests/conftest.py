"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> tuple[str, str, str, str]:
    """Return (subject, from_addr, to_addr, body) for a fixture text file.

    Fixture format: an RFC-ish header block (Subject/From/To/Date), a blank
    line, then the body.
    """
    raw = (FIXTURES_DIR / name).read_text()
    head, _, body = raw.partition("\n\n")
    headers: dict[str, str] = {}
    for line in head.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return (
        headers.get("subject", ""),
        headers.get("from", ""),
        headers.get("to", ""),
        body,
    )


@pytest.fixture
def gmail_vc_referral() -> tuple[str, str, str, str]:
    return _load("gmail_vc_referral.txt")


@pytest.fixture
def outlook_candidate_referral() -> tuple[str, str, str, str]:
    return _load("outlook_candidate_referral.txt")


@pytest.fixture
def internal_reply_skip() -> tuple[str, str, str, str]:
    return _load("internal_reply_skip.txt")


@pytest.fixture
def fwd_re_keep() -> tuple[str, str, str, str]:
    return _load("fwd_re_keep.txt")
