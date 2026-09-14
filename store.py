"""SQLite persistence for PaidUp: invoices, bank rows, client signals, outbox and audit log."""

import csv
import json
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal

from cadence import Sent
from ledger import BankRow, Invoice

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (number TEXT PRIMARY KEY, client TEXT, amount TEXT, issued_on TEXT,
                                     due_on TEXT, late_fee_percent TEXT);
CREATE TABLE IF NOT EXISTS bank_rows (row_id TEXT PRIMARY KEY, booked_on TEXT, amount TEXT, description TEXT);
CREATE TABLE IF NOT EXISTS confirmations (row_id TEXT PRIMARY KEY, invoice TEXT);
CREATE TABLE IF NOT EXISTS claims (invoice TEXT, quote TEXT);
CREATE TABLE IF NOT EXISTS promises (id INTEGER PRIMARY KEY, invoice TEXT, promised_on TEXT, quote TEXT);
CREATE TABLE IF NOT EXISTS disputes (invoice TEXT, quote TEXT, open INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS outbox (invoice TEXT, stage TEXT, body TEXT, sent_on TEXT, UNIQUE(invoice, stage));
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts TEXT, actor TEXT, action TEXT, detail TEXT);
"""


def _dec(value: str | None) -> Decimal | None:
    return Decimal(value) if value else None


class Store:
    def __init__(self, path: str = ":memory:"):
        # ponytail: tools run in a Strands worker thread; the agent uses SequentialToolExecutor so
        # access is never concurrent. Add a lock if tools ever run concurrently.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)

    def audit(self, actor: str, action: str, detail: dict) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self.db.execute("INSERT INTO audit (ts, actor, action, detail) VALUES (?, ?, ?, ?)",
                        (ts, actor, action, json.dumps(detail, default=str)))
        self.db.commit()

    def audit_log(self) -> list[dict]:
        rows = self.db.execute("SELECT ts, actor, action, detail FROM audit ORDER BY id")
        return [{"ts": ts, "actor": actor, "action": action, "detail": json.loads(d)} for ts, actor, action, d in rows]

    def add_invoices(self, invoices: list[Invoice], actor: str = "system") -> None:
        self.db.executemany("INSERT OR REPLACE INTO invoices VALUES (?, ?, ?, ?, ?, ?)", [
            (i.number, i.client, str(i.amount), i.issued_on.isoformat(), i.due_on.isoformat(),
             str(i.late_fee_percent) if i.late_fee_percent is not None else None) for i in invoices])
        self.audit(actor, "add_invoices", {"numbers": [i.number for i in invoices]})

    def add_bank_rows(self, rows: list[BankRow], actor: str = "system") -> None:
        self.db.executemany("INSERT OR REPLACE INTO bank_rows VALUES (?, ?, ?, ?)", [
            (r.row_id, r.booked_on.isoformat(), str(r.amount), r.description) for r in rows])
        self.audit(actor, "add_bank_rows", {"row_ids": [r.row_id for r in rows]})

    def invoices(self) -> list[Invoice]:
        rows = self.db.execute("SELECT * FROM invoices ORDER BY number")
        return [Invoice(n, c, Decimal(a), date.fromisoformat(i), date.fromisoformat(d), _dec(f))
                for n, c, a, i, d, f in rows]

    def bank_rows(self) -> list[BankRow]:
        rows = self.db.execute("SELECT * FROM bank_rows ORDER BY booked_on, row_id")
        return [BankRow(r, date.fromisoformat(b), Decimal(a), d) for r, b, a, d in rows]

    def confirm(self, row_id: str, invoice: str, actor: str = "owner") -> None:
        self.db.execute("INSERT OR REPLACE INTO confirmations VALUES (?, ?)", (row_id, invoice))
        self.audit(actor, "confirm_payment", {"row_id": row_id, "invoice": invoice})

    def confirmations(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT row_id, invoice FROM confirmations"))

    def add_claim(self, invoice: str, quote: str, actor: str = "agent") -> None:
        self.db.execute("INSERT INTO claims VALUES (?, ?)", (invoice, quote))
        self.audit(actor, "record_claim", {"invoice": invoice, "quote": quote})

    def claims(self) -> set[str]:
        return {n for (n,) in self.db.execute("SELECT invoice FROM claims")}

    def add_promise(self, invoice: str, promised_on: date, quote: str, actor: str = "agent") -> None:
        self.db.execute("INSERT INTO promises (invoice, promised_on, quote) VALUES (?, ?, ?)",
                        (invoice, promised_on.isoformat(), quote))
        self.audit(actor, "record_promise", {"invoice": invoice, "promised_on": promised_on, "quote": quote})

    def promises(self) -> dict[str, date]:
        # The latest promise per invoice wins.
        rows = self.db.execute("SELECT invoice, promised_on FROM promises ORDER BY id")
        return {n: date.fromisoformat(d) for n, d in rows}

    def open_dispute(self, invoice: str, quote: str, actor: str = "agent") -> None:
        self.db.execute("INSERT INTO disputes (invoice, quote) VALUES (?, ?)", (invoice, quote))
        self.audit(actor, "open_dispute", {"invoice": invoice, "quote": quote})

    def signals(self) -> list[dict]:
        """Promises, claims and disputes with their quotes, in the order they were recorded."""
        kinds = {"record_promise": "promise", "record_claim": "claim", "open_dispute": "dispute"}
        # ponytail: read from the append-only audit log, which already keeps one global order across kinds.
        rows = self.db.execute("SELECT action, detail FROM audit WHERE action IN (?, ?, ?) ORDER BY id", tuple(kinds))
        return [{"kind": kinds[action], "invoice": d["invoice"], "quote": d["quote"], "promised_on": d.get("promised_on")}
                for action, d in ((a, json.loads(detail)) for a, detail in rows)]

    def open_disputes(self) -> set[str]:
        return {n for (n,) in self.db.execute("SELECT invoice FROM disputes WHERE open = 1")}

    def send(self, invoice: str, stage: str, body: str, sent_on: date, actor: str = "agent") -> str:
        """Record an outgoing message. The UNIQUE(invoice, stage) constraint makes retries harmless."""
        try:
            self.db.execute("INSERT INTO outbox VALUES (?, ?, ?, ?)", (invoice, stage, body, sent_on.isoformat()))
        except sqlite3.IntegrityError:
            self.audit(actor, "send_duplicate_ignored", {"invoice": invoice, "stage": stage})
            return "duplicate"
        self.audit(actor, "send", {"invoice": invoice, "stage": stage, "sent_on": sent_on})
        return "sent"

    def history(self, invoice: str) -> list[Sent]:
        rows = self.db.execute("SELECT stage, sent_on FROM outbox WHERE invoice = ? ORDER BY sent_on", (invoice,))
        return [Sent(stage, date.fromisoformat(d)) for stage, d in rows]

    def outbox(self) -> list[dict]:
        rows = self.db.execute("SELECT invoice, stage, body, sent_on FROM outbox ORDER BY sent_on, invoice")
        return [{"invoice": i, "stage": s, "body": b, "sent_on": d} for i, s, b, d in rows]


def load_invoices_csv(path) -> list[Invoice]:
    with open(path, newline="") as f:
        return [Invoice(r["number"], r["client"], Decimal(r["amount"]), date.fromisoformat(r["issued_on"]),
                        date.fromisoformat(r["due_on"]), _dec(r["late_fee_percent"])) for r in csv.DictReader(f)]


def load_bank_csv(path) -> list[BankRow]:
    with open(path, newline="") as f:
        return [BankRow(r["row_id"], date.fromisoformat(r["booked_on"]), Decimal(r["amount"]), r["description"])
                for r in csv.DictReader(f)]
