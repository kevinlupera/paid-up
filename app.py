"""PaidUp web demo: a thin HTTP layer over the same agent turns demo.py runs in the terminal.

Run: .venv/bin/uvicorn app:app --port 8000
"""

import json
import threading
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from cadence import next_action
from demo import APRIL_DATE, BANK_PROMPT, CLASSIFY_PROMPT, DATA, REVIEW_PROMPT, START_DATE, email_text
from ledger import reconcile, unmatched_rows
from paidup_agent import build_agent
from store import load_bank_csv, load_invoices_csv

STATIC = Path(__file__).parent / "static"
ANSWERS = {"approve", "trust", "reject"}
PAUSED = {"dispute_open", "promise_pending", "claim_unconfirmed"}


class Approvals(BaseModel):
    answers: dict[str, str]


def _money(value) -> str:
    return f"{value:.2f}"


def create_app(build=build_agent, sessions_dir: str = "sessions") -> FastAPI:
    app = FastAPI(title="PaidUp")
    replies = json.loads((DATA / "replies.json").read_text())
    # ponytail: one in-memory demo session behind a global lock (single user, one browser tab).
    # Per-user sessions keyed by a cookie when more than one person needs the demo at once.
    lock = threading.Lock()
    session: dict = {}

    def reset():
        Path(sessions_dir).mkdir(parents=True, exist_ok=True)
        session_id = f"web-{uuid.uuid4().hex[:8]}"
        agent, store = build(f"{sessions_dir}/{session_id}.sqlite", session_id)
        store.add_invoices(load_invoices_csv(DATA / "invoices.csv"))
        store.add_bank_rows(load_bank_csv(DATA / "bank_march.csv"))
        session.clear()
        session.update(agent=agent, store=store, today=START_DATE, pending=[], summary="",
                       delivered={}, april_loaded=False)

    def state() -> dict:
        store, today = session["store"], session["today"]
        day = date.fromisoformat(today)
        results = reconcile(store.invoices(), store.bank_rows(), store.confirmations(), store.claims(), today=day)
        disputes, promises = store.open_disputes(), store.promises()
        paused = sum(1 for n, r in results.items() if r.balance > 0 and next_action(
            r, store.history(n), day, dispute_open=n in disputes, promised_on=promises.get(n)).reason in PAUSED)
        total = lambda values: _money(sum(values, Decimal("0")))  # noqa: E731
        return {
            "today": today,
            "totals": {
                "outstanding": total(r.balance for r in results.values()),
                "confirmed_in_bank": total(r.paid for r in results.values()),
                "claimed_not_in_bank": total(r.balance for r in results.values() if r.status == "reported_not_confirmed"),
                "paused": paused,
            },
            "invoices": [{
                "number": n, "client": r.invoice.client, "amount": _money(r.invoice.amount),
                "paid": _money(r.paid), "balance": _money(r.balance), "due_on": r.invoice.due_on.isoformat(),
                "days_overdue": r.days_overdue, "status": r.status, "bank_rows": r.row_ids,
                "candidate_bank_rows": r.candidate_row_ids, "open_dispute": n in disputes,
                "promised_on": promises[n].isoformat() if n in promises else None,
            } for n, r in results.items()],
            "unmatched_bank_rows": [{"row_id": row.row_id, "booked_on": row.booked_on.isoformat(),
                                     "amount": _money(row.amount), "description": row.description}
                                    for row in unmatched_rows(store.bank_rows(), results)],
            "disputes": sorted(disputes),
            "promises": {n: d.isoformat() for n, d in promises.items()},
            "claims": sorted(store.claims()),
            "outbox": store.outbox(),
            "audit": store.audit_log()[-30:][::-1],
            "pending": session["pending"],
            "summary": session["summary"],
            "replies": [{**reply, "index": i, "delivered": i in session["delivered"],
                         "detected": session["delivered"].get(i, [])}
                        for i, reply in enumerate(replies)],
            "april_loaded": session["april_loaded"],
        }

    def run(prompt) -> None:
        """One agent call. Interrupts become pending approvals; otherwise the summary is kept."""
        try:
            result = session["agent"](prompt, invocation_state={"today": session["today"]})
        except Exception as error:  # the model or AWS failed; show it instead of a blank 500
            raise HTTPException(502, f"The agent could not finish this step: {error}") from error
        if result.stop_reason == "interrupt":
            session["pending"] = [{"id": i.id, "tool": i.reason["tool"], "args": i.reason["args"]}
                                  for i in result.interrupts]
        else:
            session["pending"] = []
            session["summary"] = str(result)

    def ensure_session():
        if not session:
            reset()

    def refuse_if_pending():
        if session["pending"]:
            raise HTTPException(409, "Answer the pending approvals first.")

    @app.get("/")
    def home():
        return FileResponse(STATIC / "index.html")

    @app.post("/api/reset")
    def reset_demo():
        with lock:
            reset()
            return state()

    @app.get("/api/state")
    def get_state():
        with lock:
            ensure_session()
            return state()

    @app.post("/api/review")
    def review():
        with lock:
            ensure_session()
            refuse_if_pending()
            run(REVIEW_PROMPT)
            return state()

    @app.post("/api/replies/{index}")
    def deliver_reply(index: int):
        with lock:
            ensure_session()
            if not 0 <= index < len(replies):
                raise HTTPException(404, "There is no demo email with that number.")
            refuse_if_pending()
            if index in session["delivered"]:
                raise HTTPException(409, "That email was already delivered.")
            reply = replies[index]
            session["delivered"][index] = []
            session["today"] = reply["received_on"]
            before = len(session["store"].signals())
            run(CLASSIFY_PROMPT + email_text(reply))
            # Signals are append-only, so whatever appeared during this turn came from this email.
            session["delivered"][index] = session["store"].signals()[before:]
            return state()

    @app.post("/api/bank/april")
    def load_april():
        with lock:
            ensure_session()
            refuse_if_pending()
            if session["april_loaded"]:
                raise HTTPException(409, "The April statement is already loaded.")
            session["store"].add_bank_rows(load_bank_csv(DATA / "bank_april.csv"))
            session["april_loaded"] = True
            session["today"] = APRIL_DATE
            run(BANK_PROMPT)
            return state()

    @app.post("/api/approvals")
    def answer_approvals(body: Approvals):
        with lock:
            ensure_session()
            pending = session["pending"]
            if not pending:
                raise HTTPException(409, "There is nothing waiting for approval.")
            answers = {item["id"]: body.answers.get(item["id"]) for item in pending}
            if any(answer not in ANSWERS for answer in answers.values()):
                raise HTTPException(400, "Every pending item needs approve, trust or reject.")
            run([{"interruptResponse": {"interruptId": i, "response": a}} for i, a in answers.items()])
            return state()

    return app


app = create_app()
