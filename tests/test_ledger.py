from datetime import date
from decimal import Decimal

from ledger import BankRow, Invoice, reconcile, unmatched_rows

TODAY = date(2026, 9, 13)


def inv(number="INV-001", amount="1000.00", issued=date(2026, 7, 1), due=date(2026, 8, 1), fee=None):
    return Invoice(number, "Acme", Decimal(amount), issued, due, Decimal(fee) if fee else None)


def row(row_id, amount, description="", booked=date(2026, 8, 10)):
    return BankRow(row_id, booked, Decimal(amount), description)


def one(invoice, rows, **kw):
    kw.setdefault("today", TODAY)
    return reconcile([invoice], rows, **kw)["INV-001" if invoice.number == "INV-001" else invoice.number]


def test_exact_reference_match_pays_invoice():
    r = one(inv(), [row("r1", "1000.00", "TRANSFER INV-001 ACME")])
    assert r.status == "paid"
    assert r.paid == Decimal("1000.00")
    assert r.balance == Decimal("0.00")
    assert r.row_ids == ["r1"]
    assert r.days_overdue == 0


def test_reference_match_is_case_insensitive():
    r = one(inv(), [row("r1", "1000.00", "payment inv-001")])
    assert r.status == "paid"


def test_partial_payment_via_two_rows():
    rows = [row("r1", "400.00", "INV-001 part 1"), row("r2", "300.00", "INV-001 part 2")]
    r = one(inv(), rows)
    assert r.status == "partial"
    assert r.paid == Decimal("700.00")
    assert r.balance == Decimal("300.00")
    assert r.row_ids == ["r1", "r2"]
    assert r.days_overdue == (TODAY - date(2026, 8, 1)).days


def test_overpayment_is_paid_and_reports_extra():
    r = one(inv(), [row("r1", "1050.00", "INV-001")])
    assert r.status == "paid"
    assert r.balance == Decimal("0.00")
    assert r.overpaid == Decimal("50.00")


def test_same_amount_without_reference_is_only_a_candidate():
    r = one(inv(), [row("r1", "1000.00", "ACME HOLDINGS LLC")])
    assert r.status == "needs_confirmation"
    assert r.paid == Decimal("0")
    assert r.row_ids == []
    assert r.candidate_row_ids == ["r1"]


def test_candidate_must_be_booked_on_or_after_issue_date():
    r = one(inv(), [row("r1", "1000.00", "ACME", booked=date(2026, 6, 30))])
    assert r.status == "overdue"
    assert r.candidate_row_ids == []


def test_confirmed_row_is_applied():
    r = one(inv(), [row("r1", "1000.00", "ACME HOLDINGS LLC")], confirmed={"r1": "INV-001"})
    assert r.status == "paid"
    assert r.row_ids == ["r1"]


def test_one_row_cannot_pay_two_invoices():
    a = inv("INV-001")
    b = inv("INV-002")
    results = reconcile([a, b], [row("r1", "1000.00", "INV-001 INV-002")], today=TODAY)
    paid = [n for n, r in results.items() if r.status == "paid"]
    assert paid == ["INV-001"]
    assert results["INV-002"].status == "overdue"


def test_applied_row_is_not_a_candidate_for_another_invoice():
    a = inv("INV-001")
    b = inv("INV-002")
    results = reconcile([a, b], [row("r1", "1000.00", "INV-001")], today=TODAY)
    assert results["INV-002"].candidate_row_ids == []


def test_claim_without_row_is_reported_not_confirmed():
    r = one(inv(), [], claims={"INV-001"})
    assert r.status == "reported_not_confirmed"
    assert r.balance == Decimal("1000.00")


def test_claim_with_matching_row_is_paid():
    r = one(inv(), [row("r1", "1000.00", "INV-001")], claims={"INV-001"})
    assert r.status == "paid"


def test_not_yet_due_is_open():
    r = one(inv(due=date(2026, 9, 30)), [])
    assert r.status == "open"
    assert r.days_overdue == 0


def test_past_due_without_payment_is_overdue():
    r = one(inv(), [])
    assert r.status == "overdue"
    assert r.days_overdue == 43


def test_unmatched_rows_surface_partial_payment_without_reference():
    invoices = [inv(), inv(number="INV-002", amount="500.00")]
    rows = [
        row("r1", "1000.00", "ACME INV-001"),  # applied by reference
        row("r2", "500.00", "WIRE"),  # candidate for INV-002
        row("r3", "600.00", "PARENT HOLDINGS LLC WIRE"),  # 60% partial, no reference
    ]
    results = reconcile(invoices, rows, today=TODAY)
    assert [r.row_id for r in unmatched_rows(rows, results)] == ["r3"]


def test_confirmed_row_is_not_unmatched():
    rows = [row("r3", "600.00", "PARENT HOLDINGS LLC WIRE")]
    results = reconcile([inv()], rows, confirmed={"r3": "INV-001"}, today=TODAY)
    assert unmatched_rows(rows, results) == []
