from datetime import date
from decimal import Decimal

from intent import ReplyIntent, verify

EMAIL = """Hi Ana,

Thanks for the reminder. We will pay   INV-1005 this
Friday, April 3.

Best, Tom"""


def test_quote_present_keeps_kind():
    i = ReplyIntent(kind="promise_to_pay", invoice_number="INV-1005", promised_on=date(2026, 4, 3),
                    quote="we will pay INV-1005 this friday, April 3")
    assert verify(i, EMAIL).kind == "promise_to_pay"


def test_quote_not_in_email_downgrades_to_unclear():
    i = ReplyIntent(kind="claims_paid", invoice_number="INV-1005", quote="We already paid last week")
    assert verify(i, EMAIL).kind == "unclear"


def test_empty_quote_downgrades_to_unclear():
    i = ReplyIntent(kind="dispute", invoice_number="INV-1005", quote="   ")
    assert verify(i, EMAIL).kind == "unclear"


def test_promise_without_date_downgrades_to_unclear():
    i = ReplyIntent(kind="promise_to_pay", invoice_number="INV-1005", quote="We will pay INV-1005 this Friday")
    assert verify(i, EMAIL).kind == "unclear"


def test_partial_offer_without_amount_downgrades_to_unclear():
    i = ReplyIntent(kind="partial_offer", invoice_number="INV-1005", quote="We will pay INV-1005 this")
    assert verify(i, EMAIL).kind == "unclear"


def test_partial_offer_with_amount_is_kept():
    i = ReplyIntent(kind="partial_offer", invoice_number="INV-1005", amount=Decimal("500.00"),
                    quote="We will pay INV-1005 this")
    assert verify(i, EMAIL).kind == "partial_offer"


def test_downgrade_keeps_quote_for_the_person():
    i = ReplyIntent(kind="claims_paid", quote="not there")
    out = verify(i, EMAIL)
    assert out.quote == "not there"
    assert i.kind == "claims_paid"  # original untouched
