"""Deterministic invoice ledger: bank reconciliation and invoice status. No LLM here."""

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Invoice:
    number: str
    client: str
    amount: Decimal
    issued_on: date
    due_on: date
    late_fee_percent: Decimal | None = None


@dataclass(frozen=True)
class BankRow:
    row_id: str
    booked_on: date
    amount: Decimal
    description: str


@dataclass
class Result:
    invoice: Invoice
    status: str
    paid: Decimal
    balance: Decimal
    overpaid: Decimal
    days_overdue: int
    row_ids: list[str] = field(default_factory=list)
    candidate_row_ids: list[str] = field(default_factory=list)


def _references(row: BankRow, number: str) -> bool:
    # Whole-token match so "INV-001" does not match "INV-0010".
    return re.search(rf"(?<!\w){re.escape(number)}(?!\w)", row.description, re.IGNORECASE) is not None


def unmatched_rows(rows: list[BankRow], results: dict[str, Result]) -> list[BankRow]:
    """Bank rows that are neither applied to nor a candidate for any invoice: a person must look at them."""
    seen = {row_id for r in results.values() for row_id in (*r.row_ids, *r.candidate_row_ids)}
    return [row for row in rows if row.row_id not in seen]


def reconcile(
    invoices: list[Invoice],
    rows: list[BankRow],
    confirmed: dict[str, str] | None = None,
    claims: set[str] | None = None,
    *,
    today: date,
) -> dict[str, Result]:
    """Apply bank rows to invoices. Only applied rows move money; claims and candidates never do."""
    confirmed = confirmed or {}
    claims = claims or set()
    by_number = {i.number: i for i in invoices}
    rows_by_id = {r.row_id: r for r in rows}
    applied: dict[str, list[BankRow]] = {i.number: [] for i in invoices}
    used: set[str] = set()

    # A person's confirmation wins over any automatic rule.
    for row_id, number in confirmed.items():
        if row_id in rows_by_id and number in by_number and row_id not in used:
            applied[number].append(rows_by_id[row_id])
            used.add(row_id)

    # A row is applied to the first invoice it references, never to two.
    for row in rows:
        if row.row_id in used:
            continue
        for invoice in invoices:
            if _references(row, invoice.number):
                applied[invoice.number].append(row)
                used.add(row.row_id)
                break

    results = {}
    for invoice in invoices:
        paid = sum((r.amount for r in applied[invoice.number]), Decimal("0"))
        balance = max(Decimal("0"), invoice.amount - paid)
        overpaid = max(Decimal("0"), paid - invoice.amount)
        candidates = [
            r.row_id
            for r in rows
            if balance > 0 and r.row_id not in used and r.amount == balance and r.booked_on >= invoice.issued_on
        ]
        if balance == 0:
            status = "paid"
        elif paid > 0:
            status = "partial"
        elif candidates:
            status = "needs_confirmation"
        elif invoice.number in claims:
            status = "reported_not_confirmed"
        elif today > invoice.due_on:
            status = "overdue"
        else:
            status = "open"
        results[invoice.number] = Result(
            invoice=invoice,
            status=status,
            paid=paid,
            balance=balance,
            overpaid=overpaid,
            days_overdue=max(0, (today - invoice.due_on).days) if balance > 0 else 0,
            row_ids=[r.row_id for r in applied[invoice.number]],
            candidate_row_ids=candidates,
        )
    return results
