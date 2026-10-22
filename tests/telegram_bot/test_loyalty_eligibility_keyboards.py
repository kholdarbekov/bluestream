from keyboards import OrderKeyboards


def _callbacks(markup):
    return [btn.callback_data for row in markup.inline_keyboard for btn in row]


def test_order_confirmation_hides_reward_when_ineligible():
    cbs = _callbacks(OrderKeyboards.order_confirmation("en", meets_minimum=True, has_reward=False, show_reward=False))
    assert "checkout_choose_reward" not in cbs


def test_order_confirmation_shows_reward_by_default():
    cbs = _callbacks(OrderKeyboards.order_confirmation("en", meets_minimum=True, has_reward=False))
    assert "checkout_choose_reward" in cbs
