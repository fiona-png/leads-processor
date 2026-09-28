"""Scheduled audit of lead rows -> the "Claude Lead Check" field.

Every audited row ends up with a non-empty Claude Lead Check:
  * "OK - audited YYYY-MM-DD" when nothing is wrong, or
  * one line per issue, each prefixed (LEAD SOURCE TYPE, SEARCH TYPE, ROLE,
    SENIORITY, MISSING, POSSIBLE DUPLICATE, INVESTORS, ARR, INCOMPLETE, CHECK)
    so the table can be filtered by issue type.

Human workflow:
  * Fix the row and clear the cell -> the next audit re-checks it.
  * Decided to keep the row as it is -> start the cell with "REVIEWED" and
    the audit leaves it alone from then on.

The checks are deterministic (no LLM, no web) so the audit is free to run
often and never makes up values. Only the Claude Lead Check field is written.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .derive import roles_from_title, search_type_for, seniority_from_title
from .lead_source import RelationshipIndex, norm_name, resolve_lead_source

OK_PREFIX = "OK - audited"
# Line prefixes this audit owns and recomputes every run. Lines with any other
# prefix (e.g. BIZ ARR / SERIES findings from a Rolo review) are carried over
# untouched until a human removes them.
OWNED_PREFIXES = (
    "LEAD SOURCE TYPE",
    "SEARCH TYPE",
    "ROLE",
    "SENIORITY",
    "MISSING",
    "POSSIBLE DUPLICATE",
    "INVESTORS",
    "ARR",
    "INCOMPLETE",
    "CHECK",
    "OK - ",
)
REVIEWED_PREFIX = "REVIEWED"
DUPLICATE_WINDOW_DAYS = 120


@dataclass
class AuditRow:
    id: str
    name: str | None
    lead_date: date | None
    client_ids: tuple[str, ...]
    lead_source_type: str | None
    lead_source_individuals: tuple[str, ...]
    lead_source_client_ids: tuple[str, ...]
    lead_source_vc_ids: tuple[str, ...]
    roles: tuple[str, ...]
    seniority: str | None
    series: str | None
    search_type: str | None
    arr: float | None
    recipient_ids: tuple[str, ...]
    check_note: str | None = None
    client_investor_names: tuple[str, ...] = field(default_factory=tuple)


def check_row(
    row: AuditRow,
    *,
    index: RelationshipIndex,
    same_name_dates: dict[str, list[tuple[str, date]]],
) -> list[str]:
    notes: list[str] = []

    if not row.name or not row.client_ids:
        return [
            "INCOMPLETE: no Search name and/or no Client - probably not a real lead. "
            "Delete it or fill it in."
        ]

    # --- Missing basics ------------------------------------------------------
    missing = [
        label
        for label, val in [
            ("Lead Date", row.lead_date),
            ("Lead Source Type", row.lead_source_type),
            ("Role", row.roles),
            ("Seniority", row.seniority),
            ("Series", row.series),
            ("Biz ARR", row.arr),
            ("Lead Recipient", row.recipient_ids),
        ]
        if not val
    ]
    if missing:
        notes.append("MISSING: " + ", ".join(missing) + ".")

    # --- Lead Source Type vs Cole's history ---------------------------------
    if row.lead_date and row.lead_source_type:
        hc = index.clients.get(row.client_ids[0])
        org = next(
            (index.client_name(c) for c in row.lead_source_client_ids if c not in row.client_ids),
            None,
        )
        res = resolve_lead_source(
            index,
            llm_type=row.lead_source_type,
            llm_individual=row.lead_source_individuals[0] if row.lead_source_individuals else None,
            llm_company=org,
            referrer_email=None,
            hiring_client_id=row.client_ids[0],
            hiring_client_name=hc.name if hc else row.name,
            hiring_website=hc.website if hc else None,
            lead_date=row.lead_date,
        )
        if res.lead_source_type != row.lead_source_type:
            why = "; ".join(r for r in res.reasons if not r.startswith("overrode"))
            notes.append(
                f"LEAD SOURCE TYPE: {row.lead_source_type} -> suggest "
                f"{res.lead_source_type} ({why})."
            )
        notes += ["CHECK: " + n for n in res.review_notes]
        if row.lead_source_type == "VC" and not row.lead_source_vc_ids:
            notes.append("MISSING: Lead Source (VC Only) is blank on a VC lead.")

    # --- Search Type rule ----------------------------------------------------
    if row.arr is not None or row.series == "Public":
        expected, _ = search_type_for(row.arr, "Public" if row.series == "Public" else row.series)
        if expected and row.search_type and expected != row.search_type:
            notes.append(
                f"SEARCH TYPE: {row.search_type} -> {expected} (ARR ${row.arr:g}M: "
                "<16 Core, <51 Strategic, else Franchise)."
                if row.arr is not None
                else f"SEARCH TYPE: {row.search_type} -> {expected} (Public)."
            )

    # --- ARR sanity ------------------------------------------------------------
    if row.arr is not None:
        if row.series in {"Pre-Seed", "Seed", "A"} and row.arr >= 40:
            notes.append(
                f"ARR: ${row.arr:g}M looks too high for Series {row.series} - check it's "
                "revenue at the lead date, not funding or today's revenue."
            )
        if row.series == "Public" and row.arr < 50:
            notes.append(f"ARR: ${row.arr:g}M looks low for a public company.")

    # --- Role / seniority vs the search name ------------------------------------
    title = " ".join(row.name.split()[1:])
    t_roles = roles_from_title(title)
    t_sen = seniority_from_title(title)
    if t_roles and row.roles and not set(t_roles) & set(row.roles):
        notes.append(
            f"ROLE: {'/'.join(row.roles)} -> {'/'.join(t_roles)} (search name '{row.name}')."
        )
    if t_sen and row.seniority and t_sen != row.seniority:
        notes.append(f"SENIORITY: {row.seniority} -> {t_sen} (search name '{row.name}').")

    # --- Duplicate leads ------------------------------------------------------
    if row.lead_date:
        for other_id, other_date in same_name_dates.get(row.name.strip().lower(), []):
            if (
                other_id != row.id
                and abs((other_date - row.lead_date).days) <= DUPLICATE_WINDOW_DAYS
            ):
                notes.append(f"POSSIBLE DUPLICATE: another '{row.name}' lead dated {other_date}.")

    # --- Duplicate investor records on the client ------------------------------
    if row.client_investor_names:
        counts = Counter(norm_name(n) for n in row.client_investor_names if norm_name(n))
        dups = [n for n in row.client_investor_names if counts.get(norm_name(n), 0) > 1]
        if dups:
            notes.append(
                "INVESTORS: duplicate investor records linked on client: " + ", ".join(dups) + "."
            )

    return list(dict.fromkeys(notes))


def note_text(notes: list[str], today: date) -> str:
    return "\n".join(notes) if notes else f"{OK_PREFIX} {today.isoformat()}"


def should_write(existing: str | None, new: str) -> bool:
    """Never touch rows a human marked REVIEWED; skip no-op writes. An OK row
    is only re-stamped when its content changes (keeps the date of the first
    clean audit rather than churning every run)."""
    cur = (existing or "").strip()
    if cur.upper().startswith(REVIEWED_PREFIX):
        return False
    if cur.startswith(OK_PREFIX) and new.startswith(OK_PREFIX):
        return False
    return cur != new.strip()


@dataclass
class AuditSummary:
    audited: int = 0
    ok: int = 0
    flagged: int = 0
    written: int = 0
    skipped_reviewed: int = 0
    by_issue: Counter = field(default_factory=Counter)


def audit_rows(
    rows: list[AuditRow],
    *,
    index: RelationshipIndex,
    all_named: list[tuple[str, str, date]],
    today: date,
) -> tuple[dict[str, str], AuditSummary]:
    """Return {record_id: new Claude Lead Check text} for rows that need a write."""
    same_name: dict[str, list[tuple[str, date]]] = defaultdict(list)
    for rid, name, ld in all_named:
        same_name[name.strip().lower()].append((rid, ld))

    out: dict[str, str] = {}
    s = AuditSummary()
    for row in rows:
        s.audited += 1
        if (row.check_note or "").strip().upper().startswith(REVIEWED_PREFIX):
            s.skipped_reviewed += 1
            continue
        notes = check_row(row, index=index, same_name_dates=same_name)
        notes += [
            line
            for line in (row.check_note or "").splitlines()
            if line.strip() and not line.startswith(OWNED_PREFIXES)
        ]
        if notes:
            s.flagged += 1
            for n in notes:
                s.by_issue[re.split(r":", n, maxsplit=1)[0]] += 1
        else:
            s.ok += 1
        text = note_text(notes, today)
        if should_write(row.check_note, text):
            out[row.id] = text
    s.written = len(out)
    return out, s


def default_window(today: date, days: int) -> date:
    return today - timedelta(days=days)
