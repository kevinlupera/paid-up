"""PaidUp agent: Strands wiring over the deterministic ledger, cadence and store.

The model reads and writes language. Money, dates, stages and late fees come from code.
"""

import json
import os
from datetime import date
from decimal import Decimal, InvalidOperation

from strands import Agent, ToolContext, tool
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from strands.models import BedrockModel
from strands.session import FileSessionManager
from strands.tools.executors import SequentialToolExecutor

from cadence import next_action
from intent import ReplyIntent, verify
from ledger import reconcile, unmatched_rows
from store import Store

MODEL_ID = "us.anthropic.claude-sonnet-4-6"
REGION = "us-east-1"
OWNER_NAME = os.environ.get("PAIDUP_OWNER_NAME", "Alex Morgan")
ALWAYS_APPROVE = {"send_formal_notice", "confirm_payment"}

SYSTEM_PROMPT = f"""You are PaidUp, an accounts-receivable assistant working for {OWNER_NAME}, a freelancer (the owner).

Rules you must always follow:
- Money, balances, dates, stages and late fees come only from your tools. Never compute or invent them.
- All amounts are in US dollars. Always write them with a $ sign, never another currency symbol.
- You never move money, never threaten, never mention legal action or credit bureaus, and never contact anyone other than the invoiced client.
- You never mark an invoice as paid yourself. Only confirm_payment links a bank row to an invoice, and the owner approves it.
- A client saying they paid is not a payment. Only a matching bank row is. While that claim is unconfirmed, propose_reminders pauses the invoice; tell the owner and offer to ask the client for a payment reference.
- When a client disputes the work, stop chasing that invoice and tell the owner.
- For an unmatched bank row whose payer clearly relates to one client with an open invoice, call confirm_payment with that row and invoice. The owner approves or rejects it; do not ask in text.
- Only send what propose_reminders allows, with the exact stage and late fee it returns. Formal stages go through send_formal_notice.
- To act, call the tool directly with the drafted message. The system pauses and asks the owner automatically whenever approval is required, so never ask for approval in text and never wait for permission before calling a send tool.
- Messages to clients are short, polite and specific: invoice number, amount due and due date. Sign them as {OWNER_NAME}; never use placeholders.
- If a tool refuses or the owner rejects an action, do not retry it. Explain and ask the owner.
- When unsure, ask the owner instead of acting.

Finish every turn with a short summary for the owner: what you did, what is waiting for them, and why."""

CLASSIFIER_PROMPT = """You classify one client email about unpaid invoices.
Copy the sentence that supports your label verbatim into quote.
Use 'unclear' when the email does not clearly say one thing.
Only set promised_on or amount when the email states them. Resolve relative dates with the given today date.
Pick invoice_number only from the open invoices provided."""


def approval_needed(tool_name: str, args: dict, trusted_stages: set[str]) -> bool:
    if tool_name in ALWAYS_APPROVE:
        return True
    if tool_name == "send_reminder":
        return args.get("stage") not in trusted_stages
    return False


class ApprovalHook(HookProvider):
    """Pauses the agent with an interrupt before any action that needs the owner."""

    def __init__(self, store: Store):
        self.store = store

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self.check)

    def check(self, event: BeforeToolCallEvent) -> None:
        name, args = event.tool_use["name"], event.tool_use["input"]
        trusted = event.agent.state.get("trusted_stages") or []
        if not approval_needed(name, args, set(trusted)):
            return
        answer = str(event.interrupt("paidup-approval", reason={"tool": name, "args": args})).strip().lower()
        if answer == "approve":
            self.store.audit("owner", "approved", {"tool": name, "args": args})
        elif answer == "trust" and name == "send_reminder":
            event.agent.state.set("trusted_stages", [*trusted, args["stage"]])
            self.store.audit("owner", "trusted_stage", {"tool": name, "args": args})
        else:
            event.cancel_tool = f"The owner rejected {name}. Do not retry it; ask the owner what to do instead."
            self.store.audit("owner", "rejected", {"tool": name, "args": args, "answer": answer})


def classify_email(email_text: str, open_invoices: list[dict], today: date, model=None) -> ReplyIntent:
    """One structured-output call. The caller must still run verify()."""
    classifier = Agent(model=model or BedrockModel(model_id=MODEL_ID, region_name=REGION),
                       system_prompt=CLASSIFIER_PROMPT, callback_handler=None)
    prompt = f"Today is {today.isoformat()}.\nOpen invoices: {json.dumps(open_invoices)}\n\nEmail:\n{email_text}"
    return classifier(prompt, structured_output_model=ReplyIntent).structured_output


def _today(tool_context: ToolContext) -> date:
    # The date is supplied by the caller through invocation_state, never by the model.
    return date.fromisoformat(tool_context.invocation_state["today"])


def _fee(value: str | None) -> Decimal | None:
    try:
        fee = Decimal(value) if value not in (None, "") else None
    except InvalidOperation:
        return Decimal("-1")  # never equal to a computed fee
    return fee if fee else None


def make_tools(store: Store, classify=classify_email) -> dict:
    def results_for(today: date):
        return reconcile(store.invoices(), store.bank_rows(), store.confirmations(), store.claims(), today=today)

    def action_for(result, today: date, trusted=frozenset()):
        number = result.invoice.number
        return next_action(result, store.history(number), today, dispute_open=number in store.open_disputes(),
                           promised_on=store.promises().get(number), trusted_stages=set(trusted))

    def refused(action: str, reason: str, **detail) -> str:
        store.audit("agent", f"{action}_refused", {"reason": reason, **detail})
        return json.dumps({"status": "refused", "reason": reason})

    @tool(context=True)
    def get_ledger(tool_context: ToolContext) -> str:
        """Current receivables: status, balance and days overdue per invoice, plus bank rows linked to no invoice."""
        today = _today(tool_context)
        results = results_for(today)
        disputes, promises = store.open_disputes(), store.promises()
        store.audit("agent", "get_ledger", {"today": today})
        return json.dumps({
            "today": today.isoformat(),
            "invoices": [{
                "number": n, "client": r.invoice.client, "status": r.status, "amount": str(r.invoice.amount),
                "paid": str(r.paid), "balance": str(r.balance), "due_on": r.invoice.due_on.isoformat(),
                "days_overdue": r.days_overdue, "bank_rows": r.row_ids, "candidate_bank_rows": r.candidate_row_ids,
                "open_dispute": n in disputes, "promised_on": promises[n].isoformat() if n in promises else None,
            } for n, r in results.items()],
            "unmatched_bank_rows": [{"row_id": row.row_id, "booked_on": row.booked_on.isoformat(),
                                     "amount": str(row.amount), "description": row.description}
                                    for row in unmatched_rows(store.bank_rows(), results)],
        })

    @tool(context=True)
    def propose_reminders(tool_context: ToolContext) -> str:
        """For each unpaid invoice: the reminder stage allowed today and its late fee, or the reason none is allowed."""
        today = _today(tool_context)
        trusted = tool_context.agent.state.get("trusted_stages") or []
        proposals = []
        for number, result in results_for(today).items():
            action = action_for(result, today, trusted)
            proposals.append({
                "invoice": number, "client": result.invoice.client, "balance": str(result.balance),
                "days_overdue": result.days_overdue, "stage": action.stage, "reason": action.reason,
                "requires_approval": action.requires_approval if action.stage else None,
                "late_fee": str(action.late_fee) if action.late_fee is not None else None,
            })
        store.audit("agent", "propose_reminders", {"today": today})
        return json.dumps(proposals)

    @tool(context=True)
    def classify_reply(email_text: str, tool_context: ToolContext) -> str:
        """Classify a client email (promise, payment claim, dispute...) and record it on the invoice.

        Args:
            email_text: The full text of the client's email.
        """
        today = _today(tool_context)
        results = results_for(today)
        open_invoices = [{"number": n, "client": r.invoice.client, "balance": str(r.balance)}
                         for n, r in results.items() if r.balance > 0]
        intent = verify(classify(email_text, open_invoices, today), email_text)
        if intent.kind != "unclear" and intent.invoice_number not in {i["number"] for i in open_invoices}:
            intent = intent.model_copy(update={"kind": "unclear"})
        recorded = None
        if intent.kind == "promise_to_pay":
            store.add_promise(intent.invoice_number, intent.promised_on, intent.quote)
            recorded = "promise"
        elif intent.kind == "claims_paid":
            store.add_claim(intent.invoice_number, intent.quote)
            recorded = "claim"
        elif intent.kind == "dispute":
            store.open_dispute(intent.invoice_number, intent.quote)
            recorded = "dispute"
        store.audit("agent", "classify_reply", {"intent": intent.model_dump(mode="json"), "recorded": recorded})
        return json.dumps({"intent": intent.model_dump(mode="json"), "recorded": recorded})

    @tool(context=True)
    def send_reminder(invoice_number: str, stage: str, body: str, tool_context: ToolContext) -> str:
        """Send a friendly or firm reminder that propose_reminders allows today.

        Args:
            invoice_number: Invoice to remind about.
            stage: Exactly the stage returned by propose_reminders ("friendly" or "firm").
            body: The email text to send to the client.
        """
        today = _today(tool_context)
        result = results_for(today).get(invoice_number)
        if result is None:
            return refused("send_reminder", "unknown invoice", invoice=invoice_number)
        if stage == "formal":
            return refused("send_reminder", "formal notices must use send_formal_notice", invoice=invoice_number)
        action = action_for(result, today)
        if action.stage != stage:
            reason = action.reason or f"the stage allowed today is {action.stage}"
            return refused("send_reminder", reason, invoice=invoice_number, stage=stage)
        return json.dumps({"status": store.send(invoice_number, stage, body, today)})

    @tool(context=True)
    def send_formal_notice(invoice_number: str, body: str, late_fee: str, tool_context: ToolContext) -> str:
        """Send the formal notice for an invoice at the formal stage.

        Args:
            invoice_number: Invoice to notify about.
            body: The email text to send to the client.
            late_fee: Exactly the late fee returned by propose_reminders, or an empty string if none.
        """
        today = _today(tool_context)
        result = results_for(today).get(invoice_number)
        if result is None:
            return refused("send_formal_notice", "unknown invoice", invoice=invoice_number)
        action = action_for(result, today)
        if action.stage != "formal":
            return refused("send_formal_notice", action.reason or f"the stage allowed today is {action.stage}",
                           invoice=invoice_number)
        if _fee(late_fee) != action.late_fee:
            expected = str(action.late_fee) if action.late_fee is not None else "no late fee"
            return refused("send_formal_notice", f"late fee must be {expected}", invoice=invoice_number)
        status = store.send(invoice_number, "formal", body, today)
        return json.dumps({"status": status, "late_fee": str(action.late_fee) if action.late_fee else None})

    @tool(context=True)
    def confirm_payment(row_id: str, invoice_number: str, tool_context: ToolContext) -> str:
        """Link a bank row that has no invoice reference to an invoice. The owner must approve.

        Args:
            row_id: The unmatched or candidate bank row.
            invoice_number: The invoice the payment belongs to.
        """
        today = _today(tool_context)
        results = results_for(today)
        if row_id not in {r.row_id for r in store.bank_rows()}:
            return refused("confirm_payment", "unknown bank row", row_id=row_id)
        if invoice_number not in results:
            return refused("confirm_payment", "unknown invoice", invoice=invoice_number)
        if any(row_id in r.row_ids for r in results.values()):
            return refused("confirm_payment", "bank row is already linked to an invoice", row_id=row_id)
        store.confirm(row_id, invoice_number, actor="owner")
        after = results_for(today)[invoice_number]
        return json.dumps({"status": "linked", "invoice_status": after.status, "balance": str(after.balance)})

    return {t.tool_name: t for t in
            (get_ledger, propose_reminders, classify_reply, send_reminder, send_formal_notice, confirm_payment)}


def build_agent(db_path: str, session_id: str, storage_dir: str = "sessions") -> tuple[Agent, Store]:
    store = Store(db_path)
    model = BedrockModel(model_id=MODEL_ID, region_name=REGION)
    tools = make_tools(store, classify=lambda email, invoices, today: classify_email(email, invoices, today, model))
    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=list(tools.values()),
        hooks=[ApprovalHook(store)],
        # One tool at a time: the SQLite store is shared and approvals are asked in order.
        tool_executor=SequentialToolExecutor(),
        session_manager=FileSessionManager(session_id=session_id, storage_dir=storage_dir),
        callback_handler=None,
    )
    return agent, store
