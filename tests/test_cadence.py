from datetime import date, timedelta
from decimal import Decimal

import pytest

from cadence import Sent, next_action
from ledger import Invoice, reconcile

DUE = date(2026, 8, 1)


def result_at(days_overdue, fee=None, amount="1000.00", rows=(), claims=frozenset()):
    invoice = Invoice("INV-001", "Acme", Decimal(amount), date(2026, 7, 1), DUE, Decimal(fee) if fee else None)
    today = DUE + timedelta(days=days_overdue)
    return reconcile([invoice], list(rows), claims=set(claims), today=today)["INV-001"], invoice, today


def act(days_overdue, history=(), fee=None, amount="1000.00", **kw):
    result, invoice, today = result_at(days_overdue, fee=fee, amount=amount)
    return next_action(result, list(history), today, **kw)


@pytest.mark.parametrize("days,stage", [(1, "friendly"), (14, "friendly"), (15, "firm"), (29, "firm"), (30, "formal")])
def test_stage_boundaries(days, stage):
    a = act(days)
    assert a.stage == stage
    assert a.key == f"INV-001:{stage}"


def test_not_due_has_no_action():
    a = act(0)
    assert a.stage is None
    assert a.reason == "not_due"


def test_paid_has_no_action():
    from ledger import BankRow

    invoice = Invoice("INV-001", "Acme", Decimal("1000.00"), date(2026, 7, 1), DUE)
    today = DUE + timedelta(days=40)
    result = reconcile([invoice], [BankRow("r1", today, Decimal("1000.00"), "INV-001")], today=today)["INV-001"]
    a = next_action(result, [], today)
    assert a.reason == "paid"


def test_needs_confirmation_blocks_reminders():
    from ledger import BankRow

    invoice = Invoice("INV-001", "Acme", Decimal("1000.00"), date(2026, 7, 1), DUE)
    today = DUE + timedelta(days=20)
    result = reconcile([invoice], [BankRow("r1", today, Decimal("1000.00"), "ACME LLC")], today=today)["INV-001"]
    a = next_action(result, [], today)
    assert a.stage is None
    assert a.reason == "needs_confirmation"


def test_dispute_pauses_everything():
    a = act(40, dispute_open=True)
    assert a.stage is None
    assert a.reason == "dispute_open"


def test_pending_promise_pauses():
    today = DUE + timedelta(days=20)
    a = act(20, promised_on=today)
    assert a.reason == "promise_pending"


def test_broken_promise_proposes_current_stage():
    today = DUE + timedelta(days=20)
    a = act(20, promised_on=today - timedelta(days=1))
    assert a.stage == "firm"


def test_stage_already_sent_is_not_repeated():
    today = DUE + timedelta(days=20)
    a = act(20, history=[Sent("firm", today - timedelta(days=10))])
    assert a.stage is None
    assert a.reason == "already_sent"


def test_min_gap_between_messages():
    today = DUE + timedelta(days=16)
    a = act(16, history=[Sent("friendly", today - timedelta(days=3))])
    assert a.stage is None
    assert a.reason == "too_soon"


def test_min_gap_allows_after_enough_days():
    today = DUE + timedelta(days=16)
    a = act(16, history=[Sent("friendly", today - timedelta(days=7))])
    assert a.stage == "firm"


def test_approval_required_by_default():
    assert act(5).requires_approval is True


def test_trusted_friendly_skips_approval():
    assert act(5, trusted_stages={"friendly"}).requires_approval is False


def test_formal_always_requires_approval_even_if_trusted():
    assert act(30, trusted_stages={"friendly", "firm", "formal"}).requires_approval is True


def test_late_fee_only_at_formal_with_contract_fee():
    assert act(20, fee="1.5").late_fee is None
    assert act(30, fee="1.5").late_fee == Decimal("15.00")


def test_late_fee_rounds_half_up():
    # 1234.565 * 100 / 100 = 1234.565 -> 1234.57
    assert act(30, fee="100", amount="1234.565").late_fee == Decimal("1234.57")


def test_no_late_fee_without_contract_fee():
    assert act(45).late_fee is None


def test_unconfirmed_payment_claim_pauses_escalation():
    # "We already paid" without a bank row: ask for a reference, do not escalate to firm/formal.
    result, _, today = result_at(40, fee="1.5", claims={"INV-001"})
    a = next_action(result, [], today)
    assert a.stage is None
    assert a.reason == "claim_unconfirmed"
