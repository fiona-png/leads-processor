"""Forward/reply detection and forwarded-header parsing.

Mirrors the logic in the n8n `Filter & Extract Lead` node, but parses the inner
`From:` / `Date:` / `Subject:` headers from the forwarded body explicitly so the
LLM doesn't have to guess the original sender from prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import INTERNAL_DOMAINS

_FWD_SUBJECT_RE = re.compile(r"^\s*(?:fwd?|fw):\s*", re.IGNORECASE)
_RE_SUBJECT_RE = re.compile(r"^\s*re:\s*", re.IGNORECASE)
_FWD_THEN_RE_SUBJECT_RE = re.compile(r"^\s*(?:fwd?|fw):\s*re:\s*", re.IGNORECASE)

# Matches the marker Gmail/Outlook insert above the original message.
_FORWARDED_MARKER_RE = re.compile(
    r"-{2,}\s*Forwarded message\s*-{2,}|Begin forwarded message:",
    re.IGNORECASE,
)

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
    """A forward by subject prefix or by forwarded-marker in body."""
    if _FWD_SUBJECT_RE.match(subject or ""):
        return True
    if _FORWARDED_MARKER_RE.search(body or ""):
        return True
    return False


def is_reply_not_forward(subject: str) -> bool:
    """True for `Re: ...` but NOT for `Fwd: Re: ...`. Used to skip internal replies."""
    s = subject or ""
    if not _RE_SUBJECT_RE.match(s):
        return False
    return not _FWD_THEN_RE_SUBJECT_RE.match(s)


def should_process(subject: str, body: str, *, from_addr: str = "") -> bool:
    """Top-level filter: keep the email or drop it.

    Rules (from the n8n workflow):
      - Drop `Re: ...` unless it's `Fwd: Re: ...`.
      - Keep if subject is `Fwd:` / `Fw:` or body contains a forwarded marker.
      - Drop short internal-only replies (heuristic: from an internal domain AND
        not a forward).
    """
    if is_reply_not_forward(subject):
        return False
    if not is_forward(subject, body):
        # Internal short replies don't have a forward marker — drop them.
        return False
    if from_addr and is_internal(from_addr) and not is_forward(subject, body):
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
