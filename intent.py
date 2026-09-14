"""Client reply intent: the model labels it, this module checks the label against the email."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["promise_to_pay", "claims_paid", "dispute", "partial_offer", "needs_info", "out_of_office", "unclear"]


class ReplyIntent(BaseModel):
    """Intent of one client email about an invoice."""

    kind: Kind = Field(description="What the client is saying. Use 'unclear' when unsure.")
    invoice_number: str | None = Field(default=None, description="Invoice the email refers to, if identifiable.")
    promised_on: date | None = Field(default=None, description="Payment date the client commits to, if any.")
    amount: Decimal | None = Field(default=None, description="Amount the client mentions, if any.")
    quote: str = Field(description="Exact sentence copied verbatim from the email that supports the kind.")


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def verify(intent: ReplyIntent, email_text: str) -> ReplyIntent:
    """Downgrade to 'unclear' when the evidence does not hold. Never upgrades."""
    quote = _norm(intent.quote)
    grounded = bool(quote) and quote in _norm(email_text)
    complete = not (
        (intent.kind == "promise_to_pay" and intent.promised_on is None)
        or (intent.kind == "partial_offer" and intent.amount is None)
    )
    return intent if grounded and complete else intent.model_copy(update={"kind": "unclear"})
