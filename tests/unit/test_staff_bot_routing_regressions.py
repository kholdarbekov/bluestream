"""Static regression checks for staff bot callback and conversation wiring."""

import re
from pathlib import Path
from typing import List, Set


ROOT = Path(__file__).resolve().parents[2]
STAFF_BOT_ROOT = ROOT / "staff_bot"
BOT_FILE = STAFF_BOT_ROOT / "bot.py"


def _iter_callback_literals() -> List[str]:
    """Collect callback_data literals from staff bot source."""
    values: Set[str] = set()
    pattern = re.compile(r"callback_data\s*=\s*f?(['\"])([^'\"]+)\1")
    for path in STAFF_BOT_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for _, value in pattern.findall(text):
            if value.startswith("staff_") or value == "noop":
                values.add(value)
    return sorted(values)


def _iter_callback_patterns() -> List[str]:
    """Collect CallbackQueryHandler regex patterns from bot.py."""
    text = BOT_FILE.read_text(encoding="utf-8")
    return [
        m.group(2)
        for m in re.finditer(
            r"CallbackQueryHandler\([^\n]+pattern\s*=\s*r?(['\"])([^'\"]+)\1", text
        )
    ]


def _materialize_literal(value: str) -> str:
    """Convert f-string callback templates into a concrete sample callback."""
    # Replace variable placeholders like {user_id} with a numeric sample.
    return re.sub(r"\{[^{}]+\}", "1", value)


def test_staff_callback_literals_have_registered_handlers():
    """Every callback_data literal should match at least one registered handler pattern."""
    patterns = _iter_callback_patterns()
    assert patterns, "No callback handler patterns found in staff_bot/bot.py"

    unmatched = []
    for literal in _iter_callback_literals():
        sample = _materialize_literal(literal)
        if not any(re.match(pattern, sample) for pattern in patterns):
            unmatched.append(literal)

    assert not unmatched, (
        "Unregistered callback_data literals found. Add matching CallbackQueryHandler "
        f"patterns in staff_bot/bot.py: {unmatched}"
    )


def test_staff_create_order_conversation_wiring_present():
    """Guard create-order conversation wiring against regressions."""
    text = BOT_FILE.read_text(encoding="utf-8")

    required_fragments = [
        'CallbackQueryHandler(create_order_handler.start_create_order, pattern="^staff_create_order$")',
        'CallbackQueryHandler(create_order_handler.start_order_for_client, pattern=r"^staff_op_order_\\d+$")',
        'MessageHandler(filters.TEXT & ~filters.COMMAND, create_order_handler.receive_client_search)',
        'CallbackQueryHandler(create_order_handler.select_address, pattern=r"^staff_op_addr_\\d+$")',
        'CallbackQueryHandler(create_order_handler.select_product, pattern=r"^staff_op_product_\\d+$")',
        'CallbackQueryHandler(create_order_handler.select_quantity, pattern=r"^staff_op_qty_\\d+_\\d+$")',
        'CallbackQueryHandler(create_order_handler.select_payment, pattern=r"^staff_op_pay_")',
        'CallbackQueryHandler(create_order_handler.skip_notes, pattern="^staff_op_skip_notes$")',
        'CallbackQueryHandler(create_order_handler.confirm_order, pattern="^staff_op_confirm_order$")',
        'name="staff_create_order"',
    ]

    missing = [fragment for fragment in required_fragments if fragment not in text]
    assert not missing, f"Missing create-order conversation fragments: {missing}"


def test_staff_cod_callback_and_reconciliation_wiring_present():
    """Ensure COD completion and reconciliation callbacks stay registered.

    Reconciliation and COD collection are reached from the Cash hub sub-menu,
    so the hub callback wiring must also be present.
    """
    text = BOT_FILE.read_text(encoding="utf-8")
    required_fragments = [
        'CallbackQueryHandler(status_update_handler.confirm_full_cash_collection, pattern=r"^staff_cash_full_\\d+$")',
        'CallbackQueryHandler(status_update_handler.start_partial_cash_collection, pattern=r"^staff_cash_partial_\\d+$")',
        'CallbackQueryHandler(status_update_handler.start_no_cash_collection, pattern=r"^staff_cash_none_\\d+$")',
        'CallbackQueryHandler(status_update_handler.show_reconciliation_session, pattern="^staff_reconcile_session$")',
        'CallbackQueryHandler(status_update_handler.submit_reconciliation_all, pattern="^staff_reconcile_submit_all$")',
        'CallbackQueryHandler(status_update_handler.start_reconciliation_submit, pattern="^staff_reconcile_submit$")',
        'CallbackQueryHandler(cash_collection_handler.show_debtor_list, pattern="^staff_cod_collect_menu$")',
        'CallbackQueryHandler(cash_collection_handler.paginate_debtor_list, pattern=r"^staff_cod_list_page_\\d+$")',
        'CallbackQueryHandler(cash_collection_handler.show_customer_statement, pattern=r"^staff_cod_customer_\\d+$")',
        'CallbackQueryHandler(cash_collection_handler.start_full_collection, pattern=r"^staff_cod_collect_full_\\d+$")',
        'CallbackQueryHandler(cash_collection_handler.start_custom_collection, pattern=r"^staff_cod_collect_custom_\\d+$")',
        'CallbackQueryHandler(cash_collection_handler.confirm_overpayment_collection, pattern=r"^staff_cod_confirm_overpay_yes$")',
        'CallbackQueryHandler(cash_collection_handler.cancel_overpayment_collection, pattern=r"^staff_cod_confirm_overpay_no$")',
        'CallbackQueryHandler(status_update_handler.show_cash_hub, pattern="^staff_cash_hub$")',
        "i18n.get('staff.menu.cash', language): 'staff_cash_hub'",
        "if cod_collection_flow:",
        "await cash_collection_handler.receive_collection_amount(update, context)",
        "await cash_collection_handler.receive_collection_note(update, context)",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in text]
    assert not missing, f"Missing COD/reconciliation callback fragments: {missing}"


def test_staff_operator_text_entry_patterns_present():
    """Ensure reply-keyboard operator flows enter conversations via message handlers."""
    text = BOT_FILE.read_text(encoding="utf-8")
    required_fragments = [
        "create_client_text_pattern = self._menu_text_pattern('staff.menu.create_client')",
        "search_client_text_pattern = self._menu_text_pattern('staff.menu.search_client')",
        "create_order_text_pattern = self._menu_text_pattern('staff.menu.create_order')",
        "filters.Regex(create_client_text_pattern) & ~filters.COMMAND",
        "filters.Regex(search_client_text_pattern) & ~filters.COMMAND",
        "filters.Regex(create_order_text_pattern) & ~filters.COMMAND",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in text]
    assert not missing, f"Missing operator text-conversation entry fragments: {missing}"


def test_staff_tryout_conversation_and_callbacks_present():
    """Ensure try-out create/task callbacks and conversation entry wiring are registered.

    Try-out create / tasks / active are reached via the Try-outs hub sub-menu, so the
    hub callback wiring must also be present.
    """
    text = BOT_FILE.read_text(encoding="utf-8")
    required_fragments = [
        'CallbackQueryHandler(tryout_handler.show_hub, pattern="^staff_tryouts_hub$")',
        'CallbackQueryHandler(tryout_handler.start_create_tryout, pattern="^staff_tryout_create$")',
        'CallbackQueryHandler(tryout_handler.show_create_products, pattern="^staff_tryout_select_products$")',
        'CallbackQueryHandler(tryout_handler.select_create_product, pattern=r"^staff_tryout_product_\\d+$")',
        'CallbackQueryHandler(tryout_handler.select_create_quantity, pattern=r"^staff_tryout_qty_\\d+_\\d+$")',
        'CallbackQueryHandler(tryout_handler.remove_create_product, pattern=r"^staff_tryout_remove_\\d+$")',
        'CallbackQueryHandler(tryout_handler.finish_product_selection, pattern="^staff_tryout_products_done$")',
        'CallbackQueryHandler(tryout_handler.confirm_create_tryout, pattern="^staff_tryout_confirm_create$")',
        'CallbackQueryHandler(tryout_handler.show_pickup_overview, pattern=r"^staff_tryout_pickup_back_\\d+$")',
        'CallbackQueryHandler(tryout_handler.edit_pickup_product, pattern=r"^staff_tryout_pickup_edit_\\d+_\\d+$")',
        'CallbackQueryHandler(tryout_handler.select_pickup_quantity, pattern=r"^staff_tryout_pickup_qty_\\d+_\\d+_\\d+$")',
        'CallbackQueryHandler(tryout_handler.clear_pickup_product, pattern=r"^staff_tryout_pickup_clear_\\d+_\\d+$")',
        'CallbackQueryHandler(tryout_handler.fill_pickup_all, pattern=r"^staff_tryout_pickup_all_\\d+$")',
        'CallbackQueryHandler(tryout_handler.clear_pickup_selection, pattern=r"^staff_tryout_pickup_clearall_\\d+$")',
        'CallbackQueryHandler(tryout_handler.submit_pickup, pattern=r"^staff_tryout_pickup_submit_\\d+$")',
        'MessageHandler(filters.LOCATION, tryout_handler.receive_create_location)',
        'MessageHandler(filters.TEXT & ~filters.COMMAND, tryout_handler.receive_create_address)',
        'name="staff_create_tryout"',
    ]
    missing = [fragment for fragment in required_fragments if fragment not in text]
    assert not missing, f"Missing try-out conversation fragments: {missing}"
