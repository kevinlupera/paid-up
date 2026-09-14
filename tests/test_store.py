from datetime import date
from decimal import Decimal

from ledger import BankRow, Invoice
from store import Store, load_bank_csv, load_invoices_csv


def invoice(number="INV-1", fee=None):
    return Invoice(number, "Acme", Decimal("100.00"), date(2026, 3, 1), date(2026, 3, 16), fee)


def test_invoices_and_rows_round_trip_with_decimal_and_date():
    s = Store(":memory:")
    s.add_invoices([invoice(fee=Decimal("1.5"))])
    s.add_bank_rows([BankRow("r1", date(2026, 3, 5), Decimal("99.99"), "ACME INV-1")])
    assert s.invoices() == [invoice(fee=Decimal("1.5"))]
    assert s.bank_rows() == [BankRow("r1", date(2026, 3, 5), Decimal("99.99"), "ACME INV-1")]


def test_outbox_duplicate_is_ignored():
    s = Store(":memory:")
    assert s.send("INV-1", "friendly", "Hi", date(2026, 3, 20)) == "sent"
    assert s.send("INV-1", "friendly", "Hi again", date(2026, 3, 21)) == "duplicate"
    assert [(h.stage, h.sent_on) for h in s.history("INV-1")] == [("friendly", date(2026, 3, 20))]


def test_every_mutation_writes_audit():
    s = Store(":memory:")
    s.add_invoices([invoice()])
    s.add_bank_rows([BankRow("r1", date(2026, 3, 5), Decimal("100.00"), "x")])
    s.confirm("r1", "INV-1", actor="owner")
    s.add_claim("INV-1", "we paid")
    s.add_promise("INV-1", date(2026, 4, 3), "friday")
    s.open_dispute("INV-1", "never received files")
    s.send("INV-1", "friendly", "Hi", date(2026, 3, 20))
    s.send("INV-1", "friendly", "Hi", date(2026, 3, 20))
    actions = [a["action"] for a in s.audit_log()]
    assert actions == [
        "add_invoices", "add_bank_rows", "confirm_payment", "record_claim",
        "record_promise", "open_dispute", "send", "send_duplicate_ignored",
    ]
    assert s.audit_log()[2]["actor"] == "owner"
    assert s.audit_log()[2]["detail"] == {"row_id": "r1", "invoice": "INV-1"}


def test_state_queries():
    s = Store(":memory:")
    s.confirm("r1", "INV-1")
    s.add_claim("INV-2", "paid")
    s.add_promise("INV-3", date(2026, 4, 1), "soon")
    s.add_promise("INV-3", date(2026, 4, 3), "friday")
    s.open_dispute("INV-4", "files")
    assert s.confirmations() == {"r1": "INV-1"}
    assert s.claims() == {"INV-2"}
    assert s.promises() == {"INV-3": date(2026, 4, 3)}
    assert s.open_disputes() == {"INV-4"}


def test_csv_loaders_parse_decimal_and_date(tmp_path):
    inv = tmp_path / "invoices.csv"
    inv.write_text(
        "number,client,amount,issued_on,due_on,late_fee_percent\n"
        "INV-1,Acme,100.50,2026-03-01,2026-03-16,1.5\n"
        "INV-2,Beta,20.00,2026-03-02,2026-03-17,\n"
    )
    bank = tmp_path / "bank.csv"
    bank.write_text("row_id,booked_on,amount,description\nr1,2026-03-05,100.50,ACME INV-1\n")
    assert load_invoices_csv(inv) == [
        Invoice("INV-1", "Acme", Decimal("100.50"), date(2026, 3, 1), date(2026, 3, 16), Decimal("1.5")),
        Invoice("INV-2", "Beta", Decimal("20.00"), date(2026, 3, 2), date(2026, 3, 17), None),
    ]
    assert load_bank_csv(bank) == [BankRow("r1", date(2026, 3, 5), Decimal("100.50"), "ACME INV-1")]


def test_store_is_usable_from_a_tool_worker_thread():
    # Strands runs tools in worker threads; the connection is created in the main thread.
    from concurrent.futures import ThreadPoolExecutor

    s = Store(":memory:")
    s.add_invoices([invoice()])
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(s.invoices).result() == [invoice()]


def test_signals_expose_quotes_across_kinds_in_recorded_order():
    s = Store(":memory:")
    s.add_claim("INV-2", "We already paid")
    s.add_promise("INV-1", date(2026, 4, 3), "I'll pay Friday")
    s.open_dispute("INV-3", "We never received the files")
    assert s.signals() == [
        {"kind": "claim", "invoice": "INV-2", "quote": "We already paid", "promised_on": None},
        {"kind": "promise", "invoice": "INV-1", "quote": "I'll pay Friday", "promised_on": "2026-04-03"},
        {"kind": "dispute", "invoice": "INV-3", "quote": "We never received the files", "promised_on": None},
    ]
