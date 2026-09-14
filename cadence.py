"""Deterministic reminder cadence: which message may be proposed today, if any. No LLM here."""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from ledger import Result

# Checked from the most severe stage down.
STAGES = (("formal", 30), ("firm", 15), ("friendly", 1))


@dataclass(frozen=True)
class Sent:
    stage: str
    sent_on: date


@dataclass(frozen=True)
class Action:
    stage: str | None = None
    reason: str | None = None
    key: str | None = None
    requires_approval: bool = True
    late_fee: Decimal | None = None


def next_action(
    result: Result,
    history: list[Sent],
    today: date,
    dispute_open: bool = False,
    promised_on: date | None = None,
    min_gap_days: int = 7,
    trusted_stages: set[str] = frozenset(),
) -> Action:
    if result.status == "paid":
        return Action(reason="paid")
    if dispute_open:
        return Action(reason="dispute_open")
    if result.status == "needs_confirmation":
        return Action(reason="needs_confirmation")
    if result.status == "reported_not_confirmed":
        return Action(reason="claim_unconfirmed")
    if result.days_overdue < 1:
        return Action(reason="not_due")
    if promised_on and today <= promised_on:
        return Action(reason="promise_pending")

    stage = next(name for name, days in STAGES if result.days_overdue >= days)
    if stage in {s.stage for s in history}:
        return Action(reason="already_sent")
    if history and (today - max(s.sent_on for s in history)).days < min_gap_days:
        return Action(reason="too_soon")

    late_fee = None
    pct = result.invoice.late_fee_percent
    if stage == "formal" and pct:
        late_fee = (result.balance * pct / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return Action(
        stage=stage,
        key=f"{result.invoice.number}:{stage}",
        requires_approval=stage == "formal" or late_fee is not None or stage not in trusted_stages,
        late_fee=late_fee,
    )
