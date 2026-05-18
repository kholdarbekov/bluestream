"""Unit tests for generic helper utilities."""

from datetime import datetime, timedelta, timezone
import warnings

import pytest

from business_app.utils import helpers


@pytest.mark.unit
class TestHelpersCore:
    def test_generate_random_string_and_codes(self):
        value = helpers.generate_random_string(24)
        tracking = helpers.generate_tracking_code()
        referral = helpers.generate_referral_code(7, "umar")
        otp = helpers.generate_otp(6)
        invoice = helpers.generate_invoice_number()

        assert len(value) == 24
        assert tracking.startswith("TR")
        assert len(referral) == 8
        # generate_referral_code returns md5 hex .upper() — the result can be
        # all digits (~3% of the time), and str.isupper() is False without any
        # cased character. The semantic invariant is "no lowercase letters".
        assert referral == referral.upper()
        assert otp.isdigit() and len(otp) == 6
        assert invoice.startswith("INV")

    def test_email_phone_validation_and_formatting(self):
        assert helpers.validate_email("user@example.com") is True
        assert helpers.validate_email("bad-email") is False
        assert helpers.validate_phone_number("+998901234567") is True
        assert helpers.format_phone_number("90 123 45 67") == "+998901234567"

    def test_distance_radius_and_time(self, app):
        # ARCH-003: delivery-fee calculation moved to DeliveryService.
        with app.app_context():
            app.config["DELIVERY_RADIUS_KM"] = 20

            distance = helpers.calculate_distance(41.2995, 69.2401, 41.3111, 69.2797)
            within = helpers.is_within_delivery_radius(41.2995, 69.2401, 41.3111, 69.2797)
            eta = helpers.estimate_delivery_time(distance_km=5, traffic_factor=1.2)

        assert distance > 0
        assert within is True
        assert eta == int(30 + (5 * 2 * 1.2))

    def test_currency_text_and_slug_helpers(self):
        assert helpers.format_currency(12000, "UZS").endswith("so'm")
        assert helpers.format_currency(12, "USD").startswith("$")
        assert helpers.parse_currency("12,345 so'm") == 12345
        assert helpers.truncate_text("abcdef", 4) == "a..."
        assert helpers.slugify("Hello, World!") == "hello-world"

    def test_file_and_path_helpers(self, app):
        with app.app_context():
            app.config["ALLOWED_EXTENSIONS"] = {"jpg", "png", "pdf"}

            safe_name = helpers.sanitize_filename("a/b:c?.jpg")
            ext = helpers.get_file_extension("photo.JPG")
            allowed = helpers.is_allowed_file("file.png")
            denied = helpers.is_allowed_file("file.exe")
            path = helpers.generate_file_path(3, "avatar.png", folder="users")

        assert safe_name == "a_b_c_.jpg"
        assert ext == "jpg"
        assert allowed is True
        assert denied is False
        assert path.startswith("users/3/")
        assert path.endswith(".png")


@pytest.mark.unit
class TestHelpersI18nAndTime:
    def test_language_helpers_and_translation_wrapper(self, app, monkeypatch):
        with app.test_request_context("/"):
            helpers.set_language("ru")
            assert helpers.get_current_language() == "ru"

            monkeypatch.setattr("business_app.utils.translations.get_translation", lambda key, language=None, **_: f"{key}:{language}")
            assert helpers.translate_text("hello", language="uz") == "hello:uz"

    def test_format_datetime_variants(self, app):
        dt = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)
        with app.app_context():
            assert helpers.format_datetime(dt, "date", "en") == "01/15/2026"
            assert helpers.format_datetime(dt, "time", "en") == "14:30"
            assert helpers.format_datetime(dt, "datetime", "en") == "01/15/2026 14:30"
            assert "January" in helpers.format_datetime(dt, "full", "en")

    def test_time_slots_and_business_window(self):
        slots = helpers.get_time_slots(start_hour=9, end_hour=12, interval_minutes=60)
        assert slots == ["09:00-10:00", "10:00-11:00", "11:00-12:00"]

        assert helpers.is_business_hours(datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)) is True
        assert helpers.is_business_hours(datetime(2026, 1, 1, 22, 0, tzinfo=timezone.utc)) is False

        after_hours = datetime(2026, 1, 1, 22, 15, tzinfo=timezone.utc)
        before_hours = datetime(2026, 1, 1, 7, 30, tzinfo=timezone.utc)
        assert helpers.get_next_business_day(after_hours).hour == 9
        assert helpers.get_next_business_day(after_hours).day == 2
        assert helpers.get_next_business_day(before_hours).hour == 9
        assert helpers.get_next_business_day(before_hours).day == 1

    def test_size_phone_mask_and_timestamp_helpers(self, app):
        assert helpers.format_file_size(0) == "0 B"
        assert helpers.format_file_size(1024).endswith("KB")
        assert helpers.clean_phone_number("+998 (90) 123-45-67") == "998901234567"
        assert helpers.validate_uzbek_phone("+998901234567") is True
        assert helpers.validate_uzbek_phone("+123456") is False
        assert helpers.mask_phone_number("+998901234567") == "+99890123****"
        assert helpers.mask_email("abcde@example.com") == "a***e@example.com"
        assert helpers.mask_email("bademail") == "bademail"

        dt = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        assert helpers.to_ms(dt) > 0
        assert helpers.to_ms(None) == 0

        start, end = helpers.get_analytics_date_range(3)
        assert (end - start) >= timedelta(days=3)

        with app.app_context():
            app.config["LOYALTY_POINTS_RATIO"] = 1000
            app.config["LOYALTY_REDEMPTION_RATIO"] = 10
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                points = helpers.calculate_loyalty_points(5500)
            assert points == 5
            assert any(issubclass(w.category, DeprecationWarning) for w in caught)
            assert helpers.calculate_discount_from_points(7) == 70
