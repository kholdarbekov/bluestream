from shared import business_config


def test_business_config_defines_cash_edit_window_hours():
    assert business_config.CASH_EDIT_WINDOW_HOURS == 72


def test_flask_config_exposes_cash_edit_window_hours(app):
    assert app.config["CASH_EDIT_WINDOW_HOURS"] == 72
