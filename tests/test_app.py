"""Web API around the agent. The agent is faked: no model is called."""

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import create_app
from store import Store


class Turn:
    def __init__(self, text="", interrupts=()):
        self.text = text
        self.interrupts = list(interrupts)
        self.stop_reason = "interrupt" if self.interrupts else "end_turn"

    def __str__(self):
        return self.text


class FakeAgent:
    def __init__(self):
        self.script, self.calls = [], []

    def __call__(self, prompt, invocation_state=None):
        self.calls.append((prompt, invocation_state))
        turn = self.script.pop(0) if self.script else Turn("Nothing to do.")
        return turn() if callable(turn) else turn  # a callable turn can write to the store like a tool


REMINDER = SimpleNamespace(id="i1", reason={
    "tool": "send_reminder", "args": {"invoice_number": "INV-1005", "stage": "friendly", "body": "Hi Tom"}})


@pytest.fixture
def env(tmp_path):
    agent = FakeAgent()

    def build(db_path, session_id):
        agent.store = Store(db_path)
        return agent, agent.store

    client = TestClient(create_app(build=build, sessions_dir=str(tmp_path)))
    client.post("/api/reset").raise_for_status()
    return client, agent


def test_reset_loads_demo_invoices_and_march_statement(env):
    client, _ = env
    state = client.get("/api/state").json()
    assert state["today"] == "2026-03-31"
    assert [i["number"] for i in state["invoices"]] == [f"INV-100{n}" for n in range(1, 7)]
    paid = state["invoices"][0]
    assert (paid["status"], paid["paid"], paid["balance"], paid["bank_rows"]) == ("paid", "1200.00", "0.00", ["mar-001"])
    assert state["pending"] == []
    assert [r["delivered"] for r in state["replies"]] == [False] * 4


def test_interrupt_becomes_pending_approval(env):
    client, agent = env
    agent.script = [Turn(interrupts=[REMINDER])]
    state = client.post("/api/review").json()
    assert state["pending"] == [{"id": "i1", "tool": "send_reminder",
                                 "args": {"invoice_number": "INV-1005", "stage": "friendly", "body": "Hi Tom"}}]
    assert agent.calls[0][1] == {"today": "2026-03-31"}


def test_new_turns_are_refused_while_approvals_are_pending(env):
    client, agent = env
    agent.script = [Turn(interrupts=[REMINDER])]
    client.post("/api/review")
    assert client.post("/api/review").status_code == 409
    assert client.post("/api/replies/0").status_code == 409
    assert client.get("/api/state").json()["replies"][0]["delivered"] is False


def test_approvals_resume_the_agent_and_clear_pending(env):
    client, agent = env
    agent.script = [Turn(interrupts=[REMINDER]), Turn("Sent 1 reminder.")]
    client.post("/api/review")
    state = client.post("/api/approvals", json={"answers": {"i1": "trust"}}).json()
    assert state["pending"] == []
    assert state["summary"] == "Sent 1 reminder."
    assert agent.calls[-1][0] == [{"interruptResponse": {"interruptId": "i1", "response": "trust"}}]


def test_approvals_require_a_valid_answer_for_every_pending_item(env):
    client, agent = env
    agent.script = [Turn(interrupts=[REMINDER])]
    client.post("/api/review")
    assert client.post("/api/approvals", json={"answers": {"i1": "maybe"}}).status_code == 400
    assert client.post("/api/approvals", json={"answers": {}}).status_code == 400
    assert client.get("/api/state").json()["pending"][0]["id"] == "i1"


def test_delivering_a_reply_marks_it_and_moves_the_date(env):
    client, agent = env
    state = client.post("/api/replies/0").json()
    assert state["replies"][0]["delivered"] is True
    assert state["today"] == "2026-04-01"
    assert "I'll pay INV-1005 this Friday" in agent.calls[-1][0]
    assert client.post("/api/replies/0").status_code == 409
    assert client.post("/api/replies/99").status_code == 404


def test_april_statement_surfaces_the_unmatched_partial_payment(env):
    client, _ = env
    state = client.post("/api/bank/april").json()
    assert state["today"] == "2026-04-10"
    assert [r["row_id"] for r in state["unmatched_bank_rows"]] == ["apr-001"]
    assert client.post("/api/bank/april").status_code == 409


def test_agent_failure_is_reported_in_plain_words(env):
    client, agent = env

    def boom(prompt, invocation_state=None):
        raise RuntimeError("Bedrock is unreachable")

    agent.script = []
    agent.__class__ = type("Broken", (FakeAgent,), {"__call__": staticmethod(boom)})
    response = client.post("/api/review")
    assert response.status_code == 502
    assert "Bedrock is unreachable" in response.json()["detail"]


def test_home_page_is_served(env):
    client, _ = env
    response = client.get("/")
    assert response.status_code == 200
    assert "PaidUp" in response.text


def test_totals_after_reset(env):
    client, _ = env
    assert client.get("/api/state").json()["totals"] == {
        "outstanding": "7950.00", "confirmed_in_bank": "1200.00", "claimed_not_in_bank": "0.00", "paused": 0}


def test_totals_follow_payments_claims_disputes_and_promises(env):
    client, agent = env
    client.post("/api/bank/april")  # today 2026-04-10, adds the unreferenced $1,800 row
    agent.store.confirm("apr-001", "INV-1002")
    agent.store.add_claim("INV-1003", "We already paid INV-1003 on the 3rd")
    agent.store.open_dispute("INV-1004", "we never received the final files")
    agent.store.add_promise("INV-1005", date(2026, 4, 15), "I'll pay on the 15th")
    assert client.get("/api/state").json()["totals"] == {
        "outstanding": "6150.00", "confirmed_in_bank": "3000.00", "claimed_not_in_bank": "850.00", "paused": 3}


def test_delivered_reply_lists_only_the_signals_recorded_during_its_turn(env):
    client, agent = env
    agent.store.add_claim("INV-1003", "an earlier claim")

    def turn():
        agent.store.add_promise("INV-1005", date(2026, 4, 3), "I'll pay INV-1005 this Friday, April 3.")
        return Turn("Recorded a promise.")

    agent.script = [turn]
    replies = client.post("/api/replies/0").json()["replies"]
    assert replies[0]["detected"] == [{"kind": "promise", "invoice": "INV-1005",
                                       "quote": "I'll pay INV-1005 this Friday, April 3.", "promised_on": "2026-04-03"}]
    assert replies[1]["detected"] == []


def test_reply_where_the_agent_records_nothing_has_no_signals(env):
    client, _ = env
    reply = client.post("/api/replies/3").json()["replies"][3]
    assert (reply["delivered"], reply["detected"]) == (True, [])
