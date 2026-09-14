"""Deterministic guards inside the agent tools. No model is called: the classifier is stubbed."""

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from intent import ReplyIntent
from ledger import BankRow, Invoice
from paidup_agent import make_tools
from store import Store

TODAY = date(2026, 3, 31)


class FakeState(dict):
    def get(self, key=None):
        return super().get(key)

    def set(self, key, value):
        self[key] = value


def ctx(trusted=()):
    state = FakeState(trusted_stages=list(trusted)) if trusted else FakeState()
    return SimpleNamespace(invocation_state={"today": TODAY.isoformat()}, agent=SimpleNamespace(state=state))


@pytest.fixture
def env():
    store = Store(":memory:")
    store.add_invoices([
        Invoice("INV-1", "Acme", Decimal("1000.00"), date(2026, 2, 1), date(2026, 2, 27), Decimal("1.5")),
        Invoice("INV-2", "Beta", Decimal("400.00"), date(2026, 3, 10), date(2026, 3, 25), None),
    ])
    store.add_bank_rows([BankRow("r1", date(2026, 3, 20), Decimal("250.00"), "BETA HOLDINGS WIRE")])
    stub = SimpleNamespace(intent=None)
    tools = make_tools(store, classify=lambda email, open_invoices, today: stub.intent)
    return store, tools, stub


def call(tools, name, **kwargs):
    return json.loads(tools[name](tool_context=ctx(kwargs.pop("trusted", ())), **kwargs))


def test_get_ledger_shows_unmatched_rows_and_overdue(env):
    store, tools, _ = env
    out = call(tools, "get_ledger")
    by_number = {i["number"]: i for i in out["invoices"]}
    assert by_number["INV-1"]["days_overdue"] == 32
    assert [r["row_id"] for r in out["unmatched_bank_rows"]] == ["r1"]


def test_propose_reminders_uses_cadence_and_contract_fee(env):
    _, tools, _ = env
    out = {p["invoice"]: p for p in call(tools, "propose_reminders")}
    assert out["INV-1"]["stage"] == "formal"
    assert out["INV-1"]["late_fee"] == "15.00"
    assert out["INV-1"]["requires_approval"] is True
    assert out["INV-2"]["stage"] == "friendly"


def test_send_reminder_refuses_stage_not_proposed(env):
    store, tools, _ = env
    out = call(tools, "send_reminder", invoice_number="INV-2", stage="firm", body="Pay now")
    assert out["status"] == "refused"
    assert store.outbox() == []


def test_send_reminder_refuses_formal_stage(env):
    store, tools, _ = env
    out = call(tools, "send_reminder", invoice_number="INV-1", stage="formal", body="Formal")
    assert out["status"] == "refused"
    assert store.outbox() == []


def test_send_reminder_sends_once(env):
    store, tools, _ = env
    assert call(tools, "send_reminder", invoice_number="INV-2", stage="friendly", body="Hi")["status"] == "sent"
    assert call(tools, "send_reminder", invoice_number="INV-2", stage="friendly", body="Hi")["status"] == "refused"
    assert len(store.outbox()) == 1


def test_formal_notice_requires_the_computed_late_fee(env):
    store, tools, _ = env
    wrong = call(tools, "send_formal_notice", invoice_number="INV-1", body="Formal", late_fee="20.00")
    assert wrong["status"] == "refused"
    assert store.outbox() == []
    ok = call(tools, "send_formal_notice", invoice_number="INV-1", body="Formal", late_fee="15.00")
    assert ok["status"] == "sent"
    assert ok["late_fee"] == "15.00"


def test_formal_notice_refused_when_not_formal_stage(env):
    _, tools, _ = env
    out = call(tools, "send_formal_notice", invoice_number="INV-2", body="Formal", late_fee="")
    assert out["status"] == "refused"


def test_confirm_payment_links_unmatched_row(env):
    store, tools, _ = env
    out = call(tools, "confirm_payment", row_id="r1", invoice_number="INV-2")
    assert out["status"] == "linked"
    assert out["invoice_status"] == "partial"
    assert out["balance"] == "150.00"
    again = call(tools, "confirm_payment", row_id="r1", invoice_number="INV-1")
    assert again["status"] == "refused"


def test_confirm_payment_refuses_unknown_row(env):
    _, tools, _ = env
    assert call(tools, "confirm_payment", row_id="nope", invoice_number="INV-1")["status"] == "refused"


def test_classify_reply_records_promise(env):
    store, tools, stub = env
    email = "We will pay INV-2 on Friday, April 3."
    stub.intent = ReplyIntent(kind="promise_to_pay", invoice_number="INV-2", promised_on=date(2026, 4, 3),
                              quote="We will pay INV-2 on Friday, April 3.")
    out = call(tools, "classify_reply", email_text=email)
    assert out["recorded"] == "promise"
    assert store.promises() == {"INV-2": date(2026, 4, 3)}
    proposals = {p["invoice"]: p for p in call(tools, "propose_reminders")}
    assert proposals["INV-2"]["reason"] == "promise_pending"


def test_classify_reply_claim_is_not_money(env):
    store, tools, stub = env
    email = "We already paid INV-2 on the 3rd."
    stub.intent = ReplyIntent(kind="claims_paid", invoice_number="INV-2", quote=email)
    assert call(tools, "classify_reply", email_text=email)["recorded"] == "claim"
    ledger = {i["number"]: i for i in call(tools, "get_ledger")["invoices"]}
    assert ledger["INV-2"]["status"] == "reported_not_confirmed"
    assert ledger["INV-2"]["balance"] == "400.00"


def test_classify_reply_dispute_pauses_reminders(env):
    _, tools, stub = env
    email = "About INV-2: we never received the final files."
    stub.intent = ReplyIntent(kind="dispute", invoice_number="INV-2", quote=email)
    assert call(tools, "classify_reply", email_text=email)["recorded"] == "dispute"
    proposals = {p["invoice"]: p for p in call(tools, "propose_reminders")}
    assert proposals["INV-2"]["reason"] == "dispute_open"


def test_classify_reply_unknown_invoice_records_nothing(env):
    store, tools, stub = env
    email = "We already paid INV-999."
    stub.intent = ReplyIntent(kind="claims_paid", invoice_number="INV-999", quote=email)
    out = call(tools, "classify_reply", email_text=email)
    assert out["recorded"] is None
    assert out["intent"]["kind"] == "unclear"
    assert store.claims() == set()


def test_classify_reply_grounding_failure_records_nothing(env):
    store, tools, stub = env
    stub.intent = ReplyIntent(kind="claims_paid", invoice_number="INV-2", quote="We paid yesterday")
    out = call(tools, "classify_reply", email_text="Got it, forwarding to accounting.")
    assert out["intent"]["kind"] == "unclear"
    assert store.claims() == set()
