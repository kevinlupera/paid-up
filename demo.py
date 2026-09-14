"""Terminal demo: runs PaidUp through the synthetic month, pausing for the owner's approvals.

Usage: .venv/bin/python demo.py [--auto-approve]
"""

import json
import sys
import uuid
from pathlib import Path

from paidup_agent import build_agent
from store import load_bank_csv, load_invoices_csv

DATA = Path(__file__).parent / "demo_data"
AUTO = "--auto-approve" in sys.argv
START_DATE = "2026-03-31"
APRIL_DATE = "2026-04-10"
REVIEW_PROMPT = "Review the ledger and send the reminders that are allowed today."
CLASSIFY_PROMPT = "A client email arrived. Classify it and tell me what changes.\n\n"
BANK_PROMPT = ("A new bank statement was loaded. Review the ledger, flag unmatched payments "
               "and propose which invoice each belongs to, then handle today's reminders.")


def email_text(reply: dict) -> str:
    return f"From: {reply['from']} ({reply['client']})\nSubject: {reply['subject']}\n\n{reply['body']}"


def run(agent, prompt, today):
    state = {"today": today}
    print(f"\n=== {today} · {prompt.splitlines()[0][:80]}")
    result = agent(prompt, invocation_state=state)
    while result.stop_reason == "interrupt":
        responses = []
        for interrupt in result.interrupts:
            print(f"\n[approval needed] {json.dumps(interrupt.reason, indent=2)}")
            answer = "approve" if AUTO else input("approve / trust / reject > ").strip()
            responses.append({"interruptResponse": {"interruptId": interrupt.id, "response": answer}})
        result = agent(responses, invocation_state=state)
    print(f"\n{result}")


def main():
    session = f"demo-{uuid.uuid4().hex[:8]}"
    Path("sessions").mkdir(exist_ok=True)
    agent, store = build_agent(f"sessions/{session}.sqlite", session)
    store.add_invoices(load_invoices_csv(DATA / "invoices.csv"))
    store.add_bank_rows(load_bank_csv(DATA / "bank_march.csv"))

    run(agent, REVIEW_PROMPT, START_DATE)

    for reply in json.loads((DATA / "replies.json").read_text()):
        run(agent, CLASSIFY_PROMPT + email_text(reply), reply["received_on"])

    store.add_bank_rows(load_bank_csv(DATA / "bank_april.csv"))
    run(agent, BANK_PROMPT, APRIL_DATE)

    print("\n=== Outbox")
    for message in store.outbox():
        print(f"- {message['sent_on']} {message['invoice']} [{message['stage']}]")


if __name__ == "__main__":
    if {"-h", "--help"} & set(sys.argv):
        sys.exit(__doc__)
    main()
