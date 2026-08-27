"""Tier-upgrade / points-expired notifications must not borrow the earned template.

Production regression (order TG_000452_26, user 10, 2026-08-26 10:48:24 UTC):

    10:48:24.340  Awarded 288 points for order TG_000452_26
    10:48:24.344  Sending loyalty notification for user 10, event: tier_upgrade
    10:48:24.401  Sending loyalty notification for user 10, event: earned

``send_loyalty_notification`` ignored ``event_type`` and always resolved to
``NotificationType.LOYALTY_REWARD``, whose only template is the "AquaCoins
qo'shildi" earned copy. A tier_upgrade event carries no ``points``/``balance``,
so ``_render_template`` left them verbatim and the customer received a second,
broken "AquaCoins qo'shildi! ... Siz {points} AquaCoins ..." message.
"""

from unittest.mock import patch

import pytest

from business_app import db as _db
from business_app.models.loyalty import LoyaltyProgram, LoyaltyTierConfig
from business_app.services.loyalty_service import LoyaltyService
from business_app.services.notification_service import (
    DEFAULT_TEMPLATES,
    NotificationService,
)
from business_app.utils.constants import LoyaltyActionType, NotificationChannel, NotificationType


@pytest.fixture
def loyalty_program(db):
    program = LoyaltyProgram(
        name="Default Program",
        description="Default loyalty program",
        is_active=True,
        is_default=True,
        uzs_per_point=250,
        points_expiry_days=365,
        signup_bonus=100,
        referral_bonus=50,
        birthday_bonus=25,
    )
    db.session.add(program)
    db.session.commit()
    return program


@pytest.fixture
def tiers(db, loyalty_program):
    """Bronze (0+) / Silver (500+) with the trilingual names the admin UI stores."""
    bronze = LoyaltyTierConfig(
        program_id=loyalty_program.id, name="Bronze", display_order=0,
        min_points=0, max_points=499, points_multiplier=1.0, is_active=True,
    )
    silver = LoyaltyTierConfig(
        program_id=loyalty_program.id, name="Silver", display_order=1,
        min_points=500, max_points=None, points_multiplier=1.0, is_active=True,
    )
    db.session.add_all([bronze, silver])
    db.session.commit()
    bronze.set_translations({"name": {"uz": "Bronza", "ru": "Бронза", "en": "Bronze"}})
    silver.set_translations({"name": {"uz": "Kumush", "ru": "Серебро", "en": "Silver"}})
    db.session.commit()
    return {"Bronze": bronze, "Silver": silver}


# ---------------------------------------------------------------------------
# 1. event_type -> NotificationType routing (the root cause)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tier_upgrade_event_does_not_render_the_coins_earned_template(db, sample_user, tiers):
    """The exact production defect: tier_upgrade must not resolve to LOYALTY_REWARD."""
    service = NotificationService()
    with patch.object(NotificationService, "send_notification", autospec=True) as mock_send:
        mock_send.return_value = {"telegram": {"success": True}}
        service.send_loyalty_notification(
            sample_user.id,
            "tier_upgrade",
            {"tier": "Silver", "tier_config_id": tiers["Silver"].id, "balance": 3108},
        )

    assert mock_send.call_count == 1
    _self, _user_id, notif_type, _channels, _template_data = mock_send.call_args.args
    assert notif_type == NotificationType.LOYALTY_TIER_UPGRADE
    assert notif_type != NotificationType.LOYALTY_REWARD


@pytest.mark.unit
def test_points_expired_event_does_not_render_the_coins_earned_template(db, sample_user):
    service = NotificationService()
    with patch.object(NotificationService, "send_notification", autospec=True) as mock_send:
        mock_send.return_value = {"telegram": {"success": True}}
        service.send_loyalty_notification(
            sample_user.id, "points_expired", {"points": 40, "balance": 60}
        )

    assert mock_send.call_count == 1
    _self, _user_id, notif_type, _channels, _template_data = mock_send.call_args.args
    assert notif_type == NotificationType.LOYALTY_POINTS_EXPIRED


@pytest.mark.unit
def test_unmapped_loyalty_event_sends_nothing_instead_of_falling_back(db, sample_user):
    """A future event type must not silently borrow the earned template."""
    service = NotificationService()
    with patch.object(NotificationService, "send_notification", autospec=True) as mock_send:
        result = service.send_loyalty_notification(sample_user.id, "brand_new_event", {})

    assert mock_send.call_count == 0
    assert result.get("skipped") is True
    assert result.get("reason") == "unmapped_loyalty_event"


@pytest.mark.unit
def test_explicit_notification_type_still_wins_over_the_event_map(db, sample_user):
    """Back-compat: callers that pass a NotificationType keep controlling routing."""
    service = NotificationService()
    with patch.object(NotificationService, "send_notification", autospec=True) as mock_send:
        mock_send.return_value = {"telegram": {"success": True}}
        service.send_loyalty_notification(
            sample_user.id, "earned", {"points": 30, "balance": 30}, NotificationType.LOYALTY_REWARD
        )

    _self, _user_id, notif_type, _channels, _template_data = mock_send.call_args.args
    assert notif_type == NotificationType.LOYALTY_REWARD


# ---------------------------------------------------------------------------
# 2. The new templates
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "notification_type,channel",
    [
        ("loyalty_tier_upgrade", "telegram"),
        ("loyalty_tier_upgrade", "email"),
        ("loyalty_points_expired", "telegram"),
        ("loyalty_points_expired", "email"),
    ],
)
def test_new_loyalty_templates_are_registered_trilingually(notification_type, channel):
    assert (notification_type, channel) in DEFAULT_TEMPLATES
    cfg = DEFAULT_TEMPLATES[(notification_type, channel)]
    for lang in ("uz", "ru", "en"):
        assert cfg["translations"][lang]["content"].strip()


@pytest.mark.unit
def test_tier_upgrade_telegram_template_renders_with_no_leftover_placeholder():
    service = NotificationService()
    tmpl = service._build_default_notification_template("loyalty_tier_upgrade", "telegram")
    assert tmpl is not None
    for lang in ("uz", "ru", "en"):
        rendered = service._render_template(
            tmpl.get_translated("content", lang),
            {"tier_label": "Kumush", "balance": 3108},
            lang,
        )
        assert service._unrendered_placeholders(rendered) == []
        assert "Kumush" in rendered
        assert "3108" in rendered


@pytest.mark.unit
def test_points_expired_telegram_template_renders_with_no_leftover_placeholder():
    service = NotificationService()
    tmpl = service._build_default_notification_template("loyalty_points_expired", "telegram")
    assert tmpl is not None
    for lang in ("uz", "ru", "en"):
        rendered = service._render_template(
            tmpl.get_translated("content", lang), {"points": 40, "balance": 60}, lang
        )
        assert service._unrendered_placeholders(rendered) == []


# ---------------------------------------------------------------------------
# 3. Tier name localization comes from LoyaltyTierConfig (the translation SSOT)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "language,expected", [("uz", "Kumush"), ("ru", "Серебро"), ("en", "Silver")]
)
def test_tier_label_is_read_from_the_tier_config_translations(
    db, sample_user, tiers, language, expected
):
    sample_user.preferred_language = language
    db.session.commit()

    service = NotificationService()
    with patch.object(NotificationService, "send_notification", autospec=True) as mock_send:
        mock_send.return_value = {"telegram": {"success": True}}
        service.send_loyalty_notification(
            sample_user.id,
            "tier_upgrade",
            {"tier": "Silver", "tier_config_id": tiers["Silver"].id, "balance": 3108},
        )

    _self, _user_id, _notif_type, _channels, template_data = mock_send.call_args.args
    assert template_data["tier_label"] == expected


@pytest.mark.unit
def test_tier_label_falls_back_to_the_raw_name_for_an_unknown_config(db, sample_user):
    """An admin-created tier with no translations must still produce a readable message."""
    service = NotificationService()
    with patch.object(NotificationService, "send_notification", autospec=True) as mock_send:
        mock_send.return_value = {"telegram": {"success": True}}
        service.send_loyalty_notification(
            sample_user.id, "tier_upgrade", {"tier": "Diamond", "balance": 9000}
        )

    _self, _user_id, _notif_type, _channels, template_data = mock_send.call_args.args
    assert template_data["tier_label"] == "Diamond"


# ---------------------------------------------------------------------------
# 4. Placeholder-leak backstop
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unrendered_placeholders_detects_leaks_but_ignores_css_and_html():
    service = NotificationService()
    assert service._unrendered_placeholders("Siz {points} AquaCoins") == ["points"]
    assert service._unrendered_placeholders("Balans: {balance} / {points}") == ["balance", "points"]
    assert service._unrendered_placeholders("Siz 288 AquaCoins") == []
    # CSS blocks and JSON-ish braces are not placeholders.
    assert service._unrendered_placeholders("body { font-family: sans-serif; }") == []
    assert service._unrendered_placeholders('{"a": 1}') == []


@pytest.mark.unit
def test_loyalty_telegram_message_with_a_leaked_placeholder_is_never_sent(db, sample_user):
    """The backstop that would have spared the customer the `{points}` message."""
    sample_user.telegram_id = "190254690"
    sample_user.preferred_language = "uz"
    db.session.commit()

    service = NotificationService()
    service.telegram_bot_token = "test-token"
    with patch("business_app.services.notification_service.requests.post") as mock_post:
        result = service._send_telegram_notification(
            sample_user,
            NotificationType.LOYALTY_REWARD,
            {"reason_label": "AquaCoins mukofoti"},  # no points, no balance
            "uz",
        )

    assert mock_post.call_count == 0
    assert result["success"] is False
    assert result["skipped"] is True
    assert result["reason"] == "unrendered_placeholders"
    assert set(result["placeholders"]) == {"points", "balance"}


@pytest.mark.unit
def test_fully_rendered_loyalty_telegram_message_is_still_sent(db, sample_user):
    """The backstop must not suppress a correct message."""
    sample_user.telegram_id = "190254690"
    sample_user.preferred_language = "uz"
    db.session.commit()

    service = NotificationService()
    service.telegram_bot_token = "test-token"
    with patch("business_app.services.notification_service.requests.post") as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"ok": True, "result": {"message_id": 1}}
        service._send_telegram_notification(
            sample_user,
            NotificationType.LOYALTY_REWARD,
            {"reason_label": "Xarid", "points": 288, "balance": 3108},
            "uz",
        )

    assert mock_post.call_count == 1
    text = mock_post.call_args.kwargs["json"]["text"]
    assert "288" in text and "3108" in text
    assert "{" not in text


# ---------------------------------------------------------------------------
# 5. Tier dispatch is post-commit, like awards
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tier_upgrade_notification_waits_for_the_commit(db, sample_user, tiers):
    service = LoyaltyService()
    with patch.object(LoyaltyService, "_send_tier_upgrade_notification") as mock_tier, patch.object(
        LoyaltyService, "_send_points_notification"
    ):
        service.award_points(
            sample_user.id, 600, "Purchase", LoyaltyActionType.PURCHASE, commit=False
        )
        assert mock_tier.call_count == 0  # crossed Silver, but nothing committed yet
        db.session.commit()

    assert mock_tier.call_count == 1
    kwargs = mock_tier.call_args.kwargs
    assert kwargs["tier"] == "Silver"
    assert kwargs["tier_config_id"] == tiers["Silver"].id
    assert kwargs["balance"] == 600


@pytest.mark.unit
def test_rolled_back_tier_upgrade_notifies_nobody(db, sample_user, tiers):
    service = LoyaltyService()
    with patch.object(LoyaltyService, "_send_tier_upgrade_notification") as mock_tier, patch.object(
        LoyaltyService, "_send_points_notification"
    ):
        service.award_points(
            sample_user.id, 600, "Purchase", LoyaltyActionType.PURCHASE, commit=False
        )
        db.session.rollback()
        db.session.commit()

    assert mock_tier.call_count == 0


@pytest.mark.unit
def test_award_crossing_a_tier_dispatches_earned_before_tier_upgrade(db, sample_user, tiers):
    """Narrative order: 'you earned N coins', then 'you reached Silver'."""
    calls = []
    service = LoyaltyService()
    with patch.object(
        LoyaltyService, "_send_points_notification", side_effect=lambda *a, **k: calls.append("earned")
    ), patch.object(
        LoyaltyService, "_send_tier_upgrade_notification", side_effect=lambda *a, **k: calls.append("tier")
    ):
        service.award_points(
            sample_user.id, 600, "Purchase", LoyaltyActionType.PURCHASE, commit=True
        )

    assert calls == ["earned", "tier"]


@pytest.mark.unit
def test_award_without_a_tier_change_sends_only_the_earned_message(db, sample_user, tiers):
    service = LoyaltyService()
    with patch.object(LoyaltyService, "_send_tier_upgrade_notification") as mock_tier, patch.object(
        LoyaltyService, "_send_points_notification"
    ) as mock_earned:
        service.award_points(
            sample_user.id, 10, "Purchase", LoyaltyActionType.PURCHASE, commit=True
        )

    assert mock_earned.call_count == 1
    assert mock_tier.call_count == 0


# ---------------------------------------------------------------------------
# 6. End-to-end: the production scenario produces two correct messages
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tier_crossing_award_produces_two_clean_telegram_messages(db, sample_user, tiers):
    """Reproduces TG_000452_26: neither message may contain a raw placeholder.

    Drives award -> post-commit dispatch -> event routing -> template_data, then
    renders each message through its REAL template. (conftest stubs
    NotificationService.send_notification suite-wide, so the HTTP call itself is
    not reachable from here; the rendering below is the step that actually
    produced the broken customer message in production.)
    """
    sample_user.telegram_id = "190254690"
    sample_user.preferred_language = "uz"
    sample_user.is_bot_active = True
    db.session.commit()

    # Record what award_points enqueues, then run those tasks AFTER the commit
    # completes — Celery executes them out of band in a fresh session, and the
    # after_commit listener itself may not emit SQL.
    enqueued = []
    service = LoyaltyService()
    with patch(
        "business_app.tasks.notification_tasks.send_loyalty_notification_task.delay",
        side_effect=lambda *args: enqueued.append(args),
    ):
        service.award_points(
            sample_user.id, 600, "Purchase", LoyaltyActionType.PURCHASE, commit=True
        )

    assert [args[1] for args in enqueued] == ["earned", "tier_upgrade"]

    notifier = NotificationService()
    with patch.object(NotificationService, "send_notification", autospec=True) as mock_send:
        mock_send.return_value = {"telegram": {"success": True}}
        for args in enqueued:
            notification_type_str = args[3] if len(args) > 3 else None
            notifier.send_loyalty_notification(
                args[0],
                args[1],
                args[2],
                NotificationType(notification_type_str) if notification_type_str else None,
            )
        dispatched = [(c.args[2], c.args[4]) for c in mock_send.call_args_list]

    assert len(dispatched) == 2
    rendered = []
    for notif_type, template_data in dispatched:
        tmpl = notifier._build_default_notification_template(notif_type.value, "telegram")
        assert tmpl is not None, notif_type
        text = notifier._render_template(
            tmpl.get_translated("content", "uz"), template_data, "uz"
        )
        assert notifier._unrendered_placeholders(text) == [], text
        rendered.append(text)

    earned, tier = rendered
    assert "600" in earned and "Xarid" in earned
    assert "Kumush" in tier


@pytest.mark.unit
def test_tier_upgrade_goes_to_one_channel_not_both(db, sample_user, tiers):
    """Telegram-else-email, same policy as coin awards — never a duplicate email."""
    sample_user.telegram_id = "190254690"
    sample_user.is_bot_active = True
    db.session.commit()

    service = NotificationService()
    with patch.object(NotificationService, "send_notification", autospec=True) as mock_send:
        mock_send.return_value = {"telegram": {"success": True}}
        service.send_loyalty_notification(
            sample_user.id,
            "tier_upgrade",
            {"tier": "Silver", "tier_config_id": tiers["Silver"].id, "balance": 600},
        )

    _self, _user_id, _notif_type, channels, _template_data = mock_send.call_args.args
    assert channels == [NotificationChannel.TELEGRAM]
