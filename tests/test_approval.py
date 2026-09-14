from paidup_agent import approval_needed


def test_formal_notice_always_needs_approval():
    assert approval_needed("send_formal_notice", {"invoice_number": "INV-1"}, {"formal", "friendly"})


def test_confirm_payment_always_needs_approval():
    assert approval_needed("confirm_payment", {"row_id": "r1"}, {"friendly", "firm"})


def test_reminder_needs_approval_unless_stage_trusted():
    assert approval_needed("send_reminder", {"stage": "friendly"}, set())
    assert not approval_needed("send_reminder", {"stage": "friendly"}, {"friendly"})
    assert approval_needed("send_reminder", {"stage": "firm"}, {"friendly"})


def test_read_only_tools_never_need_approval():
    for name in ("get_ledger", "classify_reply", "propose_reminders"):
        assert not approval_needed(name, {}, set())
