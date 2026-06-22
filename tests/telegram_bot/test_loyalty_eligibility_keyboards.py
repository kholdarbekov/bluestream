from keyboards import MenuKeyboards, OrderKeyboards


def _callbacks(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def test_main_menu_hides_loyalty_when_ineligible():
    cbs = _callbacks(MenuKeyboards.main_menu("en", show_loyalty=False))
    assert "menu_loyalty" not in cbs
    assert "menu_subscriptions" in cbs  # subscriptions still present


def test_main_menu_shows_loyalty_by_default():
    cbs = _callbacks(MenuKeyboards.main_menu("en"))
    assert "menu_loyalty" in cbs


def test_order_confirmation_hides_reward_when_ineligible():
    cbs = _callbacks(OrderKeyboards.order_confirmation("en", meets_minimum=True, has_reward=False, show_reward=False))
    assert "checkout_choose_reward" not in cbs


def test_order_confirmation_shows_reward_by_default():
    cbs = _callbacks(OrderKeyboards.order_confirmation("en", meets_minimum=True, has_reward=False))
    assert "checkout_choose_reward" in cbs
