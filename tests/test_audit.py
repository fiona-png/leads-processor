"""Tests for the scheduled Claude Lead Check audit."""

from __future__ import annotations

from datetime import date

from cole_leads.audit import AuditRow, audit_rows, should_write
from cole_leads.lead_source import ClientRow, InvestorRow, RelationshipIndex, SearchRow

TODAY = date(2026, 9, 28)
ACME = ClientRow(id="recACME", name="Acme", website="acme.com")
SEQ = ClientRow(id="recSEQ", name="Sequoia Capital", website="sequoiacap.com")


def _index(searches=()):
    return RelationshipIndex(
        clients=[ACME, SEQ],
        investors=[InvestorRow(id="invSEQ", name="Sequoia Capital")],
        searches=list(searches),
    )


def _row(**kw) -> AuditRow:
    base = dict(
        id="recR1",
        name="Acme CRO",
        lead_date=date(2026, 9, 1),
        client_ids=("recACME",),
        lead_source_type="Company",
        lead_source_individuals=(),
        lead_source_client_ids=("recACME",),
        lead_source_vc_ids=(),
        roles=("Sales",),
        seniority="Chief",
        series="B",
        search_type="Core",
        arr=10.0,
        recipient_ids=("recMATT",),
        check_note=None,
    )
    base.update(kw)
    return AuditRow(**base)


def _run(rows, searches=()):
    named = [(r.id, r.name, r.lead_date) for r in rows if r.name and r.lead_date]
    return audit_rows(rows, index=_index(searches), all_named=named, today=TODAY)


def test_clean_row_gets_ok_stamp():
    notes, s = _run([_row()])
    assert notes["recR1"].startswith("OK - audited 2026-09-28")
    assert s.ok == 1


def test_search_type_rule_flagged():
    notes, _ = _run([_row(arr=60.0, search_type="Strategic")])
    assert "SEARCH TYPE: Strategic -> Franchise" in notes["recR1"]


def test_seniority_vs_name_flagged():
    notes, _ = _run([_row(seniority="VP")])
    assert "SENIORITY: VP -> Chief" in notes["recR1"]


def test_missing_fields_flagged():
    notes, _ = _run([_row(arr=None, series=None)])
    assert "MISSING: Series, Biz ARR." in notes["recR1"]


def test_existing_client_suggested():
    won = SearchRow(
        id="recOLD",
        client_ids=("recACME",),
        status="Closed",
        outcome="Won",
        lead_date=date(2024, 1, 1),
    )
    notes, _ = _run([_row()], searches=[won])
    assert "LEAD SOURCE TYPE: Company -> suggest Existing Client" in notes["recR1"]


def test_duplicates_flag_both_rows():
    rows = [_row(id="recA"), _row(id="recB", lead_date=date(2026, 9, 20))]
    notes, _ = _run(rows)
    assert "POSSIBLE DUPLICATE" in notes["recA"] and "POSSIBLE DUPLICATE" in notes["recB"]


def test_incomplete_row():
    notes, _ = _run([_row(name=None)])
    assert notes["recR1"].startswith("INCOMPLETE")


def test_duplicate_investors_on_client():
    notes, _ = _run([_row(client_investor_names=("Accel", "Accel Partners", "Sequoia Capital"))])
    assert (
        "INVESTORS: duplicate investor records linked on client: Accel, Accel Partners."
        in notes["recR1"]
    )


def test_reviewed_rows_are_left_alone():
    notes, s = _run([_row(seniority="VP", check_note="REVIEWED - keep as is")])
    assert "recR1" not in notes and s.skipped_reviewed == 1


def test_ok_rows_not_restamped_every_run():
    assert not should_write("OK - audited 2026-09-01", "OK - audited 2026-09-28")
    assert should_write("OK - audited 2026-09-01", "SEARCH TYPE: Core -> Strategic")
    assert should_write(None, "OK - audited 2026-09-28")
    assert not should_write("MISSING: Series.", "MISSING: Series.")


def test_rolo_findings_are_kept():
    notes, _ = _run(
        [_row(check_note="BIZ ARR: 10 -> Rolo 2026 revenue $30M.\nSEARCH TYPE: old line")]
    )
    assert notes["recR1"] == "BIZ ARR: 10 -> Rolo 2026 revenue $30M."
