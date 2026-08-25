"""Unit tests for the shared compact order-card formatter.

No DB / app context: i18n falls back to humanized key tails, so we assert on
emojis, dynamic values, and structural presence/absence — never on label text.
"""
import pytest

from staff_bot.utils.formatters import format_active_delivery_summary


def _cash_delivery(**overrides):
    d = {
        "order_number": "AD_000028_26",
        "status": "assigned",
        "customer_name": "Umar",
        "customer_phone": "+998909150171",
        "district": "Chilanzar",
        "address": "Katta Qozirabot MFY",
        "items": [{"product_name": "19 litrlik suv", "quantity": 3}],
        "total_amount": 57000,
        "payment_method": "cash",
        "amount_collected": 0,
        "outstanding_amount": 57000,
        "expected_cash_to_collect": 57000,
        "cod_reserved_prepayment_amount": 0,
    }
    d.update(overrides)
    return d


@pytest.mark.unit
class TestFormatActiveDeliverySummary:
    def test_cash_no_reserve_shows_three_money_lines_no_reserve_no_flags(self):
        out = format_active_delivery_summary(_cash_delivery(), "uz")
        assert "🚚" in out and "#AD_000028_26" in out
        assert "👤 Umar" in out
        assert "📞 +998909150171" in out
        assert "📦 19 litrlik suv ×3" in out
        assert "💰" in out and "57,000" in out          # total
        assert "🧾" in out                                # collected
        assert "💵" in out                                # to-collect
        assert "💳" not in out                            # reserved == 0 → no line
        assert "✅" not in out and "ℹ️" not in out        # flag lines dropped

    def test_cash_with_reserve_shows_reserved_line(self):
        d = _cash_delivery(cod_reserved_prepayment_amount=17000,
                           expected_cash_to_collect=40000)
        out = format_active_delivery_summary(d, "uz")
        assert "💳" in out
        assert "40,000" in out                            # to-collect < outstanding

    def test_non_cash_shows_total_and_no_cash_line_no_collected(self):
        # Payload must actually describe an order with nothing due. The
        # `_cash_delivery` base hardcodes expected_cash_to_collect = 57,000, and
        # overriding only payment_method left this asserting "no cash due" over
        # a payload that said 57,000 was owed — invisible while the money block
        # was gated on payment == 'cash'.
        d = _cash_delivery(
            payment_method="click",
            payment_status="completed",
            amount_collected=57000,
            outstanding_amount=0,
            expected_cash_to_collect=0,
        )
        out = format_active_delivery_summary(d, "uz")
        assert "💰" in out                                # total present
        assert "💵" in out                                # single to-collect(0) line
        assert "🧾" not in out                            # no collected line for non-cash

    def test_include_money_false_omits_money_block(self):
        out = format_active_delivery_summary(_cash_delivery(), "uz", include_money=False)
        assert "👤 Umar" in out and "📦 19 litrlik suv ×3" in out
        assert "💰" not in out and "🧾" not in out and "💵" not in out

    def test_position_prefixes_header_when_int(self):
        out = format_active_delivery_summary(_cash_delivery(), "uz", position=2)
        assert "3. #AD_000028_26" in out

    def test_no_position_has_no_prefix(self):
        out = format_active_delivery_summary(_cash_delivery(), "uz")
        assert "<b>#AD_000028_26" in out

    def test_multiple_items_render_one_line_each(self):
        d = _cash_delivery(items=[
            {"product_name": "19 litrlik suv", "quantity": 3},
            {"product_name": "0.5 litrlik suv", "quantity": 12},
        ])
        out = format_active_delivery_summary(d, "uz")
        assert out.count("📦") == 2
        assert "19 litrlik suv ×3" in out and "0.5 litrlik suv ×12" in out

    def test_missing_customer_name_skips_line(self):
        d = _cash_delivery()
        d.pop("customer_name")
        out = format_active_delivery_summary(d, "uz")
        assert "👤" not in out

    def test_missing_items_skips_item_lines(self):
        d = _cash_delivery(items=[])
        out = format_active_delivery_summary(d, "uz")
        assert "📦" not in out

    def test_unsettled_electronic_shows_full_cash_to_collect(self):
        # Online payment pending → driver collects the FULL amount in cash.
        d = _cash_delivery(payment_method="click", payment_status="pending")
        out = format_active_delivery_summary(d, "uz")
        collect_line = [l for l in out.splitlines() if l.startswith("💵")][0]
        assert "57,000" in collect_line          # full total due as cash
        assert "0 Uzs (" not in collect_line      # NOT the zero/no-cash-note line
        assert "🧾" not in out                     # not treated as a partial-cash order

    def test_settled_electronic_shows_no_cash_to_collect(self):
        # The payload must actually DESCRIBE a settled order. `_cash_delivery`
        # hardcodes outstanding_amount/expected_cash_to_collect = 57,000, and
        # this case used to override only payment_method/payment_status — so it
        # asserted "settled" over a payload that said 57,000 was still owed, and
        # passed only because the money block was gated on payment == 'cash'.
        # Now that the gate is the server-computed figure, state the intent.
        d = _cash_delivery(
            payment_method="click",
            payment_status="completed",
            amount_collected=57000,
            outstanding_amount=0,
            expected_cash_to_collect=0,
        )
        out = format_active_delivery_summary(d, "uz")
        collect_line = [l for l in out.splitlines() if l.startswith("💵")][0]
        assert collect_line.split(":", 1)[1].strip().startswith("0")   # 0 Uzs (no cash note)

    def test_partially_paid_click_shows_the_outstanding_delta(self):
        """Prod order 961: 2 bottles paid by Click, a 3rd added at the door.

        This printed "To collect now: 0 (no cash)" over a real 30,000 debt —
        the driver was actively told there was nothing to collect.
        """
        d = _cash_delivery(
            payment_method="click",
            payment_status="partially_paid",
            total_amount=90000,
            amount_collected=60000,
            outstanding_amount=30000,
            expected_cash_to_collect=30000,
        )
        out = format_active_delivery_summary(d, "uz")
        collect_line = [l for l in out.splitlines() if l.startswith("💵")][0]
        assert "30,000" in collect_line
        assert "0 Uzs (" not in collect_line

    def test_pending_click_still_shows_the_full_amount(self):
        d = _cash_delivery(
            payment_method="click",
            payment_status="pending",
            total_amount=36000,
            amount_collected=0,
            outstanding_amount=36000,
            expected_cash_to_collect=36000,
        )
        out = format_active_delivery_summary(d, "uz")
        collect_line = [l for l in out.splitlines() if l.startswith("💵")][0]
        assert "36,000" in collect_line

    def test_missing_order_number_uses_fallback(self):
        d = _cash_delivery()
        d.pop("order_number")
        out = format_active_delivery_summary(d, "uz")
        # falls back to i18n 'staff.common.not_available' (humanized 'Not available' with no DB)
        assert "Not available" in out
        assert "🚚" in out

    def test_decimal_quantity_normalised(self):
        d = _cash_delivery(items=[{"product_name": "suv", "quantity": 2.0}])
        out = format_active_delivery_summary(d, "uz")
        assert "suv ×2" in out and "×2.0" not in out


@pytest.mark.unit
class TestItemLabelSSOT:
    """`format_delivery_item_labels` is the ONE place that decides how an order
    line reads. Two surfaces render it — the detail card (one per line) and the
    route card's all-stops list (comma-joined) — and they must never drift on
    which field holds the name or how the quantity is formatted.
    """

    def test_detail_card_item_lines_are_exactly_the_shared_labels(self):
        from staff_bot.utils.formatters import format_delivery_item_labels

        delivery = _cash_delivery(items=[
            {"product_name": "19 litrlik suv", "quantity": 3},
            {"name": "Stakan", "quantity": 1.5},
            {"product_name": "", "quantity": 9},          # nameless: dropped
        ])
        labels = format_delivery_item_labels(delivery)
        assert labels == ["19 litrlik suv ×3", "Stakan ×1.5"]

        out = format_active_delivery_summary(delivery, "uz")
        rendered = [ln.strip()[2:].strip() for ln in out.splitlines() if ln.strip().startswith("📦")]
        assert rendered == labels

    def test_labels_are_html_escaped_once(self):
        from staff_bot.utils.formatters import format_delivery_item_labels

        labels = format_delivery_item_labels({"items": [{"product_name": "5 L <a & b>", "quantity": 1}]})
        assert labels == ["5 L &lt;a &amp; b&gt; ×1"]

    def test_missing_or_empty_items_yields_no_labels(self):
        from staff_bot.utils.formatters import format_delivery_item_labels

        assert format_delivery_item_labels({}) == []
        assert format_delivery_item_labels({"items": None}) == []
        assert format_delivery_item_labels({"items": []}) == []
