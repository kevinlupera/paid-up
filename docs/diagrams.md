# PaidUp diagrams

Every diagram below mirrors the code: `ledger.py`, `cadence.py`, `intent.py`, `paidup_agent.py`, `store.py` and `app.py`.
Code decides money, dates and stages. The model reads replies and writes messages. The owner approves anything sensitive.

- [1. How an agent turn works](#1-how-an-agent-turn-works)
- [2. Approval round trip](#2-approval-round-trip)
- [3. Invoice status](#3-invoice-status)
- [4. Understanding a client reply](#4-understanding-a-client-reply)
- [5. Which reminder is allowed today](#5-which-reminder-is-allowed-today)
- [6. When the owner is asked](#6-when-the-owner-is-asked)
- [7. Data model](#7-data-model)

The system architecture is in [architecture.mmd](architecture.mmd) ([PNG](architecture.png)).

---

## 1. How an agent turn works

Every event — the daily review, a client email, or a new bank statement — runs one Strands agent turn. The turn pauses whenever the owner has to decide.

```mermaid
flowchart TD
  E["Event<br/>daily review · client email · bank statement"] --> API["FastAPI endpoint"]
  API -->|"prompt + invocation_state.today"| A["Strands agent<br/>Claude Sonnet 4.6 on Amazon Bedrock"]
  A --> T["Model requests a tool call"]
  T --> H{"ApprovalHook<br/>owner approval needed?"}
  H -->|"no"| R{"Tool rules pass?"}
  H -->|"yes"| I[["Interrupt: the agent pauses"]]
  I --> P["Approval card in the UI"]
  P --> O{"Owner answers"}
  O -->|"approve"| R
  O -->|"trust"| S["Stage saved in agent.state"] --> R
  O -->|"reject"| C["Tool cancelled · audit: rejected"] --> A
  R -->|"no"| RF["Tool refuses · audit: refused"] --> A
  R -->|"yes"| DB[("SQLite store<br/>outbox · signals · audit")]
  DB --> A
  A -->|"no more tool calls"| SUM["Summary for the owner"]
  SUM --> UI["UI refresh<br/>totals · ledger · inbox · outbox"]
```

## 2. Approval round trip

A formal notice from the daily review: the model drafts it, code decides that it is allowed and what the fee is, and the owner approves it.

```mermaid
sequenceDiagram
  actor Owner
  participant UI as Web UI
  participant API as FastAPI
  participant Agent as Strands agent
  participant LLM as Amazon Bedrock
  participant Hook as ApprovalHook
  participant Store as SQLite store

  Owner->>UI: Run daily review
  UI->>API: POST /api/review
  API->>Agent: review prompt with invocation_state.today
  Agent->>LLM: conversation and tool specs
  LLM-->>Agent: use get_ledger and propose_reminders
  Agent->>Store: read invoices, bank rows, history
  Store-->>Agent: statuses, allowed stage and late fee computed by code
  Agent->>LLM: tool results
  LLM-->>Agent: use send_formal_notice with drafted body and late fee
  Agent->>Hook: BeforeToolCallEvent
  Hook-->>API: interrupt with tool name and arguments
  API-->>UI: pending approval card
  Owner->>UI: Approve
  UI->>API: POST /api/approvals
  API->>Agent: interruptResponse approve
  Agent->>Store: check stage and late fee, write outbox and audit
  Agent->>LLM: tool result
  LLM-->>Agent: summary for the owner
  Agent-->>API: turn finished
  API-->>UI: new state with totals, ledger, outbox, audit
```

## 3. Invoice status

`ledger.reconcile` computes each status from invoices, bank rows, owner confirmations and client claims. Only applied bank rows move money. A claim or a candidate row never does.

```mermaid
stateDiagram-v2
  direction LR
  [*] --> open: invoice issued
  open --> overdue: due date passes
  open --> paid: bank row references the invoice
  overdue --> paid: bank row references the invoice
  overdue --> partial: applied rows below the balance
  partial --> paid: remaining balance applied
  overdue --> needs_confirmation: unreferenced row equals the balance
  needs_confirmation --> paid: owner confirms the row
  overdue --> reported_not_confirmed: client claims payment, no bank row
  reported_not_confirmed --> paid: matching bank row arrives
  reported_not_confirmed --> partial: owner confirms a partial row
  paid --> [*]

  note right of overdue
    An open dispute is shown on top of the status
    and stops all reminders for that invoice.
  end note
```

Priority when several conditions apply: `paid` > `partial` > `needs_confirmation` > `reported_not_confirmed` > `overdue` > `open`.

## 4. Understanding a client reply

The model classifies the email with structured output. Deterministic checks decide whether that label can be trusted, and only three kinds change state.

```mermaid
flowchart TD
  M["Client email"] --> C["classify_reply<br/>structured output: ReplyIntent"]
  C --> V{"verify<br/>quote literally in the email?<br/>promise includes a date?"}
  V -->|"no"| U["Downgraded to unclear"]
  V -->|"yes"| K{"Invoice is one of the open invoices?"}
  K -->|"no"| U
  K -->|"yes"| KIND{"Kind"}
  KIND -->|"promise_to_pay"| PR["Record promise with quote<br/>reminders pause until that date"]
  KIND -->|"claims_paid"| CL["Record claim with quote<br/>status: claimed, not in bank<br/>escalation pauses"]
  KIND -->|"dispute"| DI["Open dispute with quote<br/>chasing stops"]
  KIND -->|"partial_offer · needs_info · out_of_office"| N["Nothing recorded<br/>agent informs the owner"]
  U --> N
  PR --> AUD["Audit: classify_reply"]
  CL --> AUD
  DI --> AUD
  N --> AUD
```

## 5. Which reminder is allowed today

`cadence.next_action` runs for every unpaid invoice. The checks go in this order, and the first match wins.

```mermaid
flowchart TD
  S["Invoice status for today"] --> P{"Paid?"}
  P -->|"yes"| R1["No action · paid"]
  P -->|"no"| D{"Open dispute?"}
  D -->|"yes"| R2["No action · dispute_open"]
  D -->|"no"| NC{"Needs confirmation?"}
  NC -->|"yes"| R3["No action · needs_confirmation"]
  NC -->|"no"| CL{"Claimed, not in bank?"}
  CL -->|"yes"| R4["No action · claim_unconfirmed"]
  CL -->|"no"| ND{"At least 1 day overdue?"}
  ND -->|"no"| R5["No action · not_due"]
  ND -->|"yes"| PP{"Promised date not passed yet?"}
  PP -->|"yes"| R6["No action · promise_pending"]
  PP -->|"no"| ST["Stage by days overdue<br/>30+ formal · 15+ firm · 1+ friendly"]
  ST --> AS{"Stage already sent?"}
  AS -->|"yes"| R7["No action · already_sent"]
  AS -->|"no"| TS{"Last message fewer than 7 days ago?"}
  TS -->|"yes"| R8["No action · too_soon"]
  TS -->|"no"| FEE["Late fee only at formal and only if the contract has one<br/>balance × percent, rounded half up to cents"]
  FEE --> OUT["Proposal: stage, fee, idempotency key invoice:stage"]
```

The send tools apply the same rules. `send_reminder` refuses any stage other than the one allowed today, and `send_formal_notice` refuses a late fee different from the computed one.

## 6. When the owner is asked

`approval_needed` plus the answer handling in `ApprovalHook`.

```mermaid
flowchart LR
  TC["Tool call"] --> N{"Which tool?"}
  N -->|"send_formal_notice · confirm_payment"| ASK["Always ask the owner"]
  N -->|"send_reminder"| TR{"Stage already trusted?"}
  TR -->|"yes"| RUN["Run the tool"]
  TR -->|"no"| ASK
  N -->|"get_ledger · propose_reminders · classify_reply"| RUN
  ASK --> ANS{"Owner answer"}
  ANS -->|"approve"| RUN
  ANS -->|"trust, send_reminder only"| SAVE["Add stage to trusted_stages"] --> RUN
  ANS -->|"reject or anything else"| CAN["Cancel the tool · audit: rejected"]
```

## 7. Data model

SQLite tables in `store.py`. Money is stored as decimal strings, never floats. `UNIQUE(invoice, stage)` in the outbox makes a duplicate reminder impossible.

```mermaid
erDiagram
  INVOICES ||--o{ CONFIRMATIONS : "paid by"
  BANK_ROWS ||--o| CONFIRMATIONS : "linked in"
  INVOICES ||--o{ PROMISES : "has"
  INVOICES ||--o{ CLAIMS : "has"
  INVOICES ||--o{ DISPUTES : "has"
  INVOICES ||--o{ OUTBOX : "reminded by"

  INVOICES {
    text number PK
    text client
    text amount
    text issued_on
    text due_on
    text late_fee_percent
  }
  BANK_ROWS {
    text row_id PK
    text booked_on
    text amount
    text description
  }
  CONFIRMATIONS {
    text row_id PK
    text invoice
  }
  PROMISES {
    integer id PK
    text invoice
    text promised_on
    text quote
  }
  CLAIMS {
    text invoice
    text quote
  }
  DISPUTES {
    text invoice
    text quote
    integer open
  }
  OUTBOX {
    text invoice "UNIQUE with stage"
    text stage
    text body
    text sent_on
  }
  AUDIT {
    integer id PK
    text ts
    text actor
    text action
    text detail
  }
```
