"""Forward/reply detection and forwarded-header parsing.

Mirrors the logic in the n8n `Filter & Extract Lead` node, but parses the inner
`From:` / `Date:` / `Subject:` headers from the forwarded body explicitly so the
LLM doesn't have to guess the original sender from prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import INTERNAL_DOMAINS

if TYPE_CHECKING:
    from .models import RawEmail

_FWD_SUBJECT_RE = re.compile(r"^\s*(?:fwd?|fw):\s*", re.IGNORECASE)
_RE_SUBJECT_RE = re.compile(r"^\s*re:\s*", re.IGNORECASE)
_FWD_THEN_RE_SUBJECT_RE = re.compile(r"^\s*(?:fwd?|fw):\s*re:\s*", re.IGNORECASE)

# Matches the marker Gmail/Outlook insert above the original message.
_FORWARDED_MARKER_RE = re.compile(
    r"-{2,}\s*Forwarded message\s*-{2,}|Begin forwarded message:",
    re.IGNORECASE,
)

# Substring needles (case-insensitive) used by the broad forward + keyword checks.
# These mirror the n8n `Filter & Extract Lead` heuristics: real leads come in as
# explicit forwards, but also as direct emails with a role title in the subject,
# or as short internal "lead" / "intro" notes from teammates.
_FORWARD_SUBJECT_NEEDLES: tuple[str, ...] = ("fwd:", "fw:")
_FORWARD_BODY_NEEDLES: tuple[str, ...] = (
    "forwarded message",
    "---------- forwarded",
    "begin forwarded",
    "fwd:",
)

# Subject keywords that on their own make a direct email worth processing.
# Substring match (case-insensitive). "vp " keeps a trailing space so we don't
# light up on words like "vpn".
ROLE_KEYWORDS: tuple[str, ...] = (
    # role titles
    "cmo",
    "ceo",
    "cto",
    "cfo",
    "coo",
    "cio",
    "vp ",
    "vice president",
    "director",
    "head of",
    "chief",
    "president",
    "svp",
    "evp",
    # intent signals
    "lead",
    "intro",
    "pre-lead",
)

# Short-internal-note heuristic: if a Cole teammate sends a tiny note with no
# forward indicator and no role keyword, drop it.
_INTERNAL_SHORT_NOTE_MAX_CHARS = 200

# Used to pull the inner headers out of the forwarded body.
_INNER_FROM_RE = re.compile(r"^\s*From:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_INNER_DATE_RE = re.compile(r"^\s*Date:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_INNER_SUBJECT_RE = re.compile(r"^\s*Subject:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_INNER_TO_RE = re.compile(r"^\s*To:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

_EMAIL_IN_ANGLES_RE = re.compile(r"<([^>]+@[^>]+)>")
_EMAIL_BARE_RE = re.compile(r"([\w.+-]+@[\w-]+\.[\w.-]+)")


def domain_of(email: str) -> str:
    """Return the lowercase domain from an email-like string."""
    m = _EMAIL_IN_ANGLES_RE.search(email) or _EMAIL_BARE_RE.search(email)
    if not m:
        return ""
    return m.group(1).split("@", 1)[-1].lower()


def is_internal(email: str) -> bool:
    """True if the address (or anything containing one) is on a Cole domain."""
    return domain_of(email) in INTERNAL_DOMAINS


def is_forward(subject: str, body: str) -> bool:
    """Forwarded message detected by subject token (`fwd:` / `fw:`) or body marker.

    Substring match on lowercased text — broader than a strict subject prefix
    because n8n's filter accepts mid-subject `Fwd:` and a wider set of body
    markers.
    """
    s = (subject or "").lower()
    if any(n in s for n in _FORWARD_SUBJECT_NEEDLES):
        return True
    b = (body or "").lower()
    return any(n in b for n in _FORWARD_BODY_NEEDLES)


def is_reply_not_forward(subject: str) -> bool:
    """True for `Re: ...` but NOT for `Fwd: Re: ...`. Used to skip internal replies."""
    s = subject or ""
    if not _RE_SUBJECT_RE.match(s):
        return False
    return not _FWD_THEN_RE_SUBJECT_RE.match(s)


def has_role_keyword(subject: str) -> bool:
    """True if the subject contains any role-title or lead-intent keyword."""
    s = (subject or "").lower()
    return any(k in s for k in ROLE_KEYWORDS)


def should_process(subject: str, body: str, *, from_addr: str = "") -> bool:
    """Top-level filter: keep the email or drop it.

    Mirrors the n8n `Filter & Extract Lead` rules so real leads aren't silently
    dropped:

      1. Drop `Re: ...` unless it's `Fwd: Re: ...` (replies are noise).
      2. Keep anything that looks like a forward (subject token or body marker).
      3. Keep anything with a role/intent keyword in the subject — direct
         emails like "Acme CMO opportunity" or "airops lead" come in cold.
      4. Drop a short internal note that has neither signal (a teammate
         scribbling something to the list with no role and no forward).
      5. Otherwise accept and let the LLM decide.
    """
    if is_reply_not_forward(subject):
        return False

    forward = is_forward(subject, body)
    keyword = has_role_keyword(subject)

    if forward or keyword:
        return True

    if from_addr and is_internal(from_addr) and len(body or "") < _INTERNAL_SHORT_NOTE_MAX_CHARS:
        return False

    return True


@dataclass(frozen=True)
class ForwardedHeaders:
    """Headers pulled from the *inner* forwarded message, not the outer envelope."""

    from_: str | None
    date: str | None
    subject: str | None
    to: str | None


def parse_forwarded_headers(body: str) -> ForwardedHeaders:
    """Extract the original sender's headers from a forwarded body.

    We look only at the slice AFTER the "Forwarded message" marker so that a
    `From:` line in the user's own forwarding note doesn't shadow the real one.
    Falls back to scanning the whole body if no marker is found.
    """
    if not body:
        return ForwardedHeaders(None, None, None, None)

    marker = _FORWARDED_MARKER_RE.search(body)
    haystack = body[marker.end() :] if marker else body

    from_m = _INNER_FROM_RE.search(haystack)
    date_m = _INNER_DATE_RE.search(haystack)
    subject_m = _INNER_SUBJECT_RE.search(haystack)
    to_m = _INNER_TO_RE.search(haystack)

    return ForwardedHeaders(
        from_=from_m.group(1).strip() if from_m else None,
        date=date_m.group(1).strip() if date_m else None,
        subject=subject_m.group(1).strip() if subject_m else None,
        to=to_m.group(1).strip() if to_m else None,
    )


# ---------------------------------------------------------------------------
# RawEmail convenience wrappers used by the pipeline
# ---------------------------------------------------------------------------


def should_process_email(email: RawEmail) -> bool:
    """Wrap `should_process` for callers that already have a `RawEmail`."""
    return should_process(email.subject, email.body_text, from_addr=email.from_addr)


def extract_inner_headers(email: RawEmail) -> ForwardedHeaders:
    """Wrap `parse_forwarded_headers` for callers that already have a `RawEmail`."""
    return parse_forwarded_headers(email.body_text)
