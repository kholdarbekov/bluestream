"""Aqua Club in the cabinet (/my-loyalty) after the redesign (website redesign step 3, group "account", unit
"account-loyalty", 2026-11-14).

The page and the API it calls are driven the way a signed-in customer's browser drives them: the httpOnly
`access_token_cookie` the storefront login sets, in uz, ru and en, with the rows the seeds ship (the backend seed's
`landing.*` / `loyalty_guide.*` / `api.loyalty.*` rows and the group's `scripts/seed_web_ui_account_translations.py`).

- Who may use the program is LoyaltyService.is_user_loyalty_eligible: an account it does not cover sees one
  "not available" state and no invented balance, tier or history (the old page faked a Bronze member), and nothing is
  created for it.
- Every program number on the page is the live program's (get_loyalty_handbook_context, the /loyalty-guide source),
  never a typed digit: change the program row and the page follows.
- The tier discount's payment condition is the backend's sentence (api.loyalty.tier_discount_condition), printed once
  under the levels whose percentages it qualifies, and not at all when no level carries a discount.
- The history chips filter: loyalty.js asks GET /api/v1/loyalty/points/history?group=<chip> (the old page asked
  /loyalty/history, which ignores `type`, through a select whose change event never reached it). A chip is a group of
  display categories (loyalty_serializers.HISTORY_GROUPS), so each row sits under the chip its label belongs to (a
  streak bonus recorded as EARNED is a bonus). Each row carries the `display_category` the page labels it by, instead
  of the English description, and the `order_number` of its order; the bot reads the same published fields.
- POST /loyalty/earn-points is gone (owner decision 1): no website script calls it.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from business_app.models.loyalty import LoyaltyPoints, LoyaltyProgram, LoyaltyStreakRule, LoyaltyTierConfig, LoyaltyTransaction
from business_app.models.translation import Translation
from business_app.models.user import User
from business_app.utils.constants import LoyaltyTransactionType
from business_app.utils.password_security import hash_password
from scripts.seed_backend_translations import BACKEND_TRANSLATIONS
from scripts.seed_web_ui_account_translations import CATEGORY, TRANSLATIONS as ACCOUNT
from scripts.seed_web_ui_cabinet_translations import TRANSLATIONS as CABINET
from shared.enums import UserRole, UserType
from tests.integration.public_page_builders import seed_copy
from tests.integration.test_web_cabinet_frame import _browser, _get

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
LANGS = ("uz", "ru", "en")
HISTORY = "/api/v1/loyalty/points/history"
# The chips the page offers, as loyalty.js sends them (`group=` omitted for "All").
CHIP_GROUPS = ("", "earned", "bonus", "redeemed", "expired")


@pytest.fixture
def account_copy(app, db):
    """The storefront rows the backend seed ships plus the cabinet's and the account group's own rows."""
    seed_copy(db, ("landing.", "web.display_labels", "cabinet.", "loyalty_guide.", "api.loyalty."))
    for seed in (CABINET, ACCOUNT):
        for language, rows in seed.items():
            for key, value in rows.items():
                db.session.add(Translation(key=key, language=language, value=value, category=CATEGORY, is_active=True))
    db.session.commit()
    app.jinja_env.cache.clear()
    yield
    app.jinja_env.cache.clear()


@pytest.fixture
def program(db):
    """A program whose numbers no template could guess: 300 so'm per AquaCoin, 700 / 350 for a referral, 150 on a
    birthday; three levels, two of them with a discount; one order-streak rule."""
    program = LoyaltyProgram(
        name="Aqua Club", is_active=True, is_default=True, uzs_per_point=300, signup_bonus=120, referral_bonus=700,
        birthday_bonus=150, points_expiry_days=365, min_redemption_points=100,
    )
    db.session.add(program)
    db.session.flush()
    db.session.add_all([
        LoyaltyTierConfig(program_id=program.id, name="Bronze", display_order=0, min_points=0, max_points=1499,
                          points_multiplier=1.0, discount_percentage=0, is_active=True),
        LoyaltyTierConfig(program_id=program.id, name="Silver", display_order=1, min_points=1500, max_points=4999,
                          points_multiplier=1.15, discount_percentage=2, is_active=True),
        LoyaltyTierConfig(program_id=program.id, name="Gold", display_order=2, min_points=5000, max_points=None,
                          points_multiplier=1.3, discount_percentage=3, is_active=True),
        LoyaltyStreakRule(program_id=program.id, name="4 orders in 30 days", required_orders=4, window_days=30,
                          bonus_points=400, is_active=True, display_order=0),
    ])
    db.session.commit()
    return program


def _txn(user, kind, points, action, days_ago):
    return LoyaltyTransaction(
        user_id=user.id,
        transaction_type=kind,
        points=points,
        description=f"{action} (English text the page must not print)",
        remaining_points=points if points > 0 else None,
        extra_data={"action_type": action},
        created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )


@pytest.fixture
def member(db, sample_user, program):
    """sample_user (an individual, so covered) at Silver: 2 200 qualifying AquaCoins, 1 900 to spend."""
    db.session.add(LoyaltyPoints(user_id=sample_user.id, program_id=program.id, total_earned=2200, total_redeemed=250,
                                 total_expired=50, current_balance=1900, current_tier="Silver"))
    db.session.add_all([
        _txn(sample_user, LoyaltyTransactionType.EARNED, 1800, "purchase", 40),
        _txn(sample_user, LoyaltyTransactionType.BONUS, 400, "referral", 30),
        _txn(sample_user, LoyaltyTransactionType.REDEEMED, -250, "redeem", 20),
        _txn(sample_user, LoyaltyTransactionType.EXPIRED, -50, "expiry", 10),
    ])
    db.session.commit()
    return sample_user


@pytest.fixture
def uncovered_company(db):
    """A business account with no contract that includes Aqua Club."""
    user = User(
        email="company@example.com", phone="+998907770011", password_hash=hash_password("TestPassword123!"),
        first_name="Firm", last_name="Owner", user_type=UserType.ENTITY, role=UserRole.CUSTOMER, is_verified=True,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _text(node):
    return " ".join(node.get_text(" ").split())


@pytest.mark.parametrize("lang", LANGS)
def test_a_member_reads_their_balance_level_and_the_live_program_in_every_language(app, db, account_copy, member, lang):
    soup, html = _get(app, db, member, "/my-loyalty", lang)
    nbsp = " "

    assert soup.select_one('[data-loyalty-state="member"]') is not None
    assert "web.account." not in html and "loyalty_guide." not in _text(soup.body)
    assert soup.select_one("#currentPoints").get_text() == f"1{nbsp}900"
    # The level and the way up, from the points summary the bot reads (GET /loyalty/points).
    assert _text(soup.select_one("#currentTier")) == "Silver"
    assert _text(soup.select_one("#tierProgressText")) == _text(BeautifulSoup(
        ACCOUNT[lang]["web.account.loyalty.to_next"].format(pts=f"2{nbsp}800", tier="Gold"), "html.parser"))
    # Ways to earn: the program row's numbers, not the old page's typed 250 / 500 / 50 / 200.
    ways = _text(soup.select_one(".ae-acl__ways"))
    assert f"300 {BACKEND_TRANSLATIONS['loyalty_guide.unit.uzs'][lang]}" in ways
    assert "700" in ways and "350" in ways
    assert ACCOUNT[lang]["web.account.loyalty.birthday_text"].format(pts="150") in ways
    for invented in ("2x", "+50", "UZS"):
        assert invented not in ways or (invented == "UZS" and lang == "en")
    # The streak rule with the member's progress; the level table marks the member's row only.
    assert _text(soup.select_one(".ae-acl__rule-name")) == "4 orders in 30 days"
    assert [row["data-tier"] for row in soup.select("tr.is-current")] == ["silver"]
    assert [row["data-tier"] for row in soup.select("#membershipTiersContainer tbody tr")] == ["bronze", "silver", "gold"]
    # The page's script and the hooks it fills.
    for hook in ("#rewardsGrid", "#pointsHistoryList", "#historyFilter", '[data-action="generate-referral"]',
                 "#referralCodeText", "#referralCount", "#referralPoints", '[data-action="history-more"]'):
        assert soup.select_one(hook) is not None, hook
    assert soup.select_one('script[src*="js/pages/loyalty.js"]') is not None
    page_data = json.loads(soup.select_one("script#page-data").string)
    assert set(page_data["loyalty"]["txn"]) >= {"order_earn", "referral", "redeem", "expired", "bonus", "other"}


def test_the_page_follows_the_program_row_not_a_copy(app, db, account_copy, member, program):
    program.uzs_per_point = 400
    program.birthday_bonus = 90
    db.session.commit()

    soup, _ = _get(app, db, member, "/my-loyalty", "en")

    ways = _text(soup.select_one(".ae-acl__ways"))
    assert "every 400 UZS" in ways and "90 AquaCoins arrive on your birthday" in ways
    assert "300" not in ways


@pytest.mark.parametrize("lang", LANGS)
def test_the_discount_condition_is_the_backend_sentence_printed_once_under_the_levels(app, db, account_copy, member, lang):
    soup, html = _get(app, db, member, "/my-loyalty", lang)
    sentence = BACKEND_TRANSLATIONS["api.loyalty.tier_discount_condition"][lang]

    note = soup.select_one("#membershipTiersContainer #acl-discount-note")
    assert note is not None and sentence in _text(note)
    assert html.count(sentence.split(":")[0]) == 1
    discounts = [_text(row.select("td")[-1]) for row in soup.select("#membershipTiersContainer tbody tr")]
    assert discounts[1].startswith("2%") and discounts[2].startswith("3%") and "%" not in discounts[0]
    # loyalty.js holds no copy of the rule.
    script = (ROOT / "business_app/static/js/pages/loyalty.js").read_text(encoding="utf-8")
    assert "tier_discount_condition" not in script and "cash" not in script.lower()


def test_no_discount_no_condition(app, db, account_copy, member):
    LoyaltyTierConfig.query.update({LoyaltyTierConfig.discount_percentage: 0})
    db.session.commit()

    soup, html = _get(app, db, member, "/my-loyalty", "ru")

    assert soup.select_one("#acl-discount-note") is None
    assert BACKEND_TRANSLATIONS["api.loyalty.tier_discount_condition"]["ru"].split(":")[0] not in html
    assert soup.select("#membershipTiersContainer sup") == []


@pytest.mark.parametrize("lang", LANGS)
def test_an_account_the_program_does_not_cover_sees_one_honest_state_and_nothing_is_made_for_it(
    app, db, account_copy, program, uncovered_company, lang
):
    soup, html = _get(app, db, uncovered_company, "/my-loyalty", lang)

    state = soup.select_one('[data-loyalty-state="unavailable"]')
    assert state is not None
    assert _text(state.select_one(".ae-empty__title")) == ACCOUNT[lang]["web.account.loyalty.unavailable_title"]
    assert _text(state.select_one(".ae-empty__text")) == ACCOUNT[lang]["web.account.loyalty.unavailable_text"]
    assert state.select_one('a[href="/loyalty-guide"]') is not None
    # No balance, level, history, rewards, referral or script: nothing is invented and nothing is asked for.
    for hook in ("#currentPoints", "#currentTier", "#pointsHistoryList", "#rewardsGrid", '[data-action="generate-referral"]'):
        assert soup.select_one(hook) is None, hook
    assert soup.select_one('script[src*="js/pages/loyalty.js"]') is None
    assert "web.account." not in html
    assert LoyaltyPoints.query.filter_by(user_id=uncovered_company.id).count() == 0


def test_every_history_chip_returns_only_its_kind(app, db, account_copy, member):
    # LoyaltyService.award_points records a streak bonus as EARNED; its label is "streak", so it is a bonus.
    db.session.add(_txn(member, LoyaltyTransactionType.EARNED, 300, "streak_bonus", 5))
    db.session.commit()
    soup, _ = _get(app, db, member, "/my-loyalty", "ru")
    offered = tuple(chip["data-group"] for chip in soup.select("#historyFilter [data-group]"))
    assert offered == CHIP_GROUPS

    browser = _browser(app, member)
    seen = {}
    for chip in offered:
        query = "?page=1&per_page=20" + (f"&group={chip}" if chip else "")
        response = browser.get(HISTORY + query)
        assert response.status_code == 200, response.get_data(as_text=True)[:300]
        body = response.get_json()
        seen[chip] = [(item["transaction_type"], item["display_category"]) for item in body["data"]["items"]]
        assert body["meta"]["total"] == len(seen[chip])

    assert len(seen[""]) == 5
    assert seen["earned"] == [("earned", "order_earn")]
    assert seen["bonus"] == [("earned", "streak"), ("bonus", "referral")]
    assert seen["redeemed"] == [("redeemed", "redeem")]
    assert seen["expired"] == [("expired", "expired")]
    # Every category the API publishes has a label in the page data, in the page's language.
    labels = json.loads(soup.select_one("script#page-data").string)["loyalty"]["txn"]
    assert all(category in labels for _, category in seen[""])
    assert labels["order_earn"] == ACCOUNT["ru"]["web.account.loyalty.txn.order_earn"]


def test_a_chip_pages_through_its_own_rows(app, db, account_copy, member):
    db.session.add_all([_txn(member, LoyaltyTransactionType.BONUS, 100 + n, "birthday_bonus", n) for n in range(1, 4)])
    db.session.commit()
    browser = _browser(app, member)

    first = browser.get(HISTORY + "?page=1&per_page=3&group=bonus").get_json()
    second = browser.get(HISTORY + "?page=2&per_page=3&group=bonus").get_json()

    assert first["meta"]["total"] == 4 and first["meta"]["pages"] == 2 and first["meta"]["has_next"] is True
    assert [item["display_category"] for item in first["data"]["items"]] == ["birthday"] * 3
    assert [item["display_category"] for item in second["data"]["items"]] == ["referral"]


def test_an_unknown_chip_is_refused(app, db, account_copy, member):
    response = _browser(app, member).get(HISTORY + "?page=1&per_page=20&group=everything")

    assert response.status_code == 400


@pytest.mark.parametrize(
    "transaction_type, action, category",
    [
        ("bonus", "referral", "referral"),
        ("bonus", "welcome_bonus", "welcome"),
        ("bonus", "birthday_bonus", "birthday"),
        ("earned", "streak_bonus", "streak"),
        ("earned", "purchase", "order_earn"),
        ("earned", "surprise_reward", "surprise"),
        ("adjustment", "reward_refund", "refund"),
        ("adjustment", "order_edit_award", "adjustment"),
        ("adjustment", "order_edit_reversal", "adjustment"),
        ("redeemed", None, "redeem"),
        ("earned", None, "order_earn"),
        ("bonus", None, "bonus"),
        ("expired", None, "expired"),
        ("adjustment", None, "adjustment"),
        ("weird", None, "other"),
    ],
)
def test_the_display_category_mapping_is_the_serializers(transaction_type, action, category):
    """The one action/type -> category mapping (the website and the customer bot both label rows by it)."""
    from business_app.serializers.loyalty_serializers import HISTORY_GROUPS, transaction_display_category

    assert transaction_display_category({"transaction_type": transaction_type, "action_type": action}) == category
    # Every category a chip lists is one the mapping can produce.
    assert set().union(*HISTORY_GROUPS.values()) <= {"order_earn", "bonus", "referral", "welcome", "birthday",
                                                     "streak", "surprise", "redeem", "expired"}


def test_both_history_endpoints_publish_the_fields_the_clients_label_rows_by(app, db, account_copy, member, sample_order):
    """GET /loyalty/points/history (the website) and GET /loyalty/history (the customer bot) publish the category, the
    redeemed reward's name and the order's number, so neither client parses the English description."""
    db.session.add_all([
        LoyaltyTransaction(user_id=member.id, transaction_type=LoyaltyTransactionType.REDEEMED, points=-400,
                           description="Redeemed reward: 19 litrlik suv", order_id=sample_order.id,
                           created_at=datetime.now(timezone.utc)),
        LoyaltyTransaction(user_id=member.id, transaction_type=LoyaltyTransactionType.ADJUSTMENT, points=400,
                           remaining_points=400, description=f"Refund of redeemed reward (order #{sample_order.id})",
                           order_id=sample_order.id, extra_data={"action_type": "reward_refund"},
                           created_at=datetime.now(timezone.utc) + timedelta(seconds=1)),
    ])
    db.session.commit()
    browser = _browser(app, member)

    for path in (HISTORY + "?page=1&per_page=20", "/api/v1/loyalty/history?page=1&per_page=20"):
        body = browser.get(path).get_json()
        # The rows of this order, by category (the member fixture has an order-less redeem row of its own).
        rows = {item["display_category"]: item for item in body["data"]["items"] if item["order_id"] == sample_order.id}
        rows["referral"] = next(item for item in body["data"]["items"] if item["display_category"] == "referral")
        assert rows["refund"]["order_id"] == sample_order.id and rows["refund"]["order_number"] == "ORD-TEST-001"
        assert rows["refund"]["reward_name"] is None
        assert rows["redeem"]["reward_name"] == "19 litrlik suv" and rows["redeem"]["order_number"] == "ORD-TEST-001"
        assert rows["referral"]["order_number"] is None and rows["referral"]["reward_name"] is None


def test_the_status_line_names_the_tier_window_not_the_points_expiry(app, db, account_copy, member, program):
    from business_app.services.loyalty_service import LoyaltyService

    program.points_expiry_days = 200
    db.session.commit()
    for lang in LANGS:
        soup, _ = _get(app, db, member, "/my-loyalty", lang)
        expected = BACKEND_TRANSLATIONS["loyalty_guide.rolling.title"][lang].format(
            days=LoyaltyService.TIER_QUALIFYING_WINDOW_DAYS)
        assert _text(soup.select_one(".ae-acl__fine")) == expected
        assert "200" not in _text(soup.select_one(".ae-acl__fine"))


def test_a_streak_rules_minimum_is_its_own_sentence(app, db, account_copy, member, program):
    rule = LoyaltyStreakRule.query.filter_by(program_id=program.id).first()
    rule.min_order_amount = 60000
    db.session.commit()
    nbsp = "\u00a0"
    for lang in LANGS:
        soup, _ = _get(app, db, member, "/my-loyalty", lang)
        metas = [_text(node) for node in soup.select(".ae-acl__rule-meta")]
        expected = ACCOUNT[lang]["web.account.loyalty.streak_min"].format(
            uzs=f"60{nbsp}000", unit=BACKEND_TRANSLATIONS["loyalty_guide.unit.uzs"][lang])
        assert _text(BeautifulSoup(expected, "html.parser")) in metas
        assert expected[0].isupper() or expected[0].isdigit()


def test_show_more_pages_through_the_history(app, db, account_copy, member):
    browser = _browser(app, member)

    first = browser.get(HISTORY + "?page=1&per_page=3").get_json()
    second = browser.get(HISTORY + "?page=2&per_page=3").get_json()

    assert first["meta"] | {"page": 1} == {"page": 1, "per_page": 3, "total": 4, "pages": 2, "has_next": True, "has_prev": False}
    assert len(first["data"]["items"]) == 3 and len(second["data"]["items"]) == 1
    assert second["meta"]["has_next"] is False
    ids = [item["id"] for item in first["data"]["items"] + second["data"]["items"]]
    assert len(set(ids)) == 4


def test_the_referral_card_shares_the_backends_link(app, db, account_copy, member):
    body = _browser(app, member).get("/api/v1/loyalty/referral").get_json()

    assert body["data"]["referral_code"]
    assert body["data"]["referral_link"].endswith(f"/register?ref={body['data']['referral_code']}")
    script = (ROOT / "business_app/static/js/pages/loyalty.js").read_text(encoding="utf-8")
    assert "data.referral_link" in script and "location.origin" not in script


def test_the_loyalty_api_refuses_the_uncovered_account_the_page_never_asks_for(app, db, program, uncovered_company):
    response = _browser(app, uncovered_company).get(HISTORY + "?page=1&per_page=20")

    assert response.status_code == 403
    assert response.get_json()["data"]["code"] == "loyalty_not_available"


def test_no_website_script_awards_aquacoins():
    """POST /loyalty/earn-points was removed (owner decision 1); nothing on the website may call it."""
    callers = [
        path.relative_to(ROOT).as_posix()
        for folder in ("business_app/static/js", "business_app/templates")
        for path in (ROOT / folder).rglob("*")
        if path.suffix in {".js", ".html"} and re.search(r"earn-points|earn_points", path.read_text(encoding="utf-8"))
    ]
    assert callers == []


# The page as admin_ui/src/__tests__/web/accountLoyalty.test.js boots it: the member's `.ae-acl` markup and the
# page-data island, rendered here by the real template (ru). Regenerate with `UPDATE_ACCOUNT_LOYALTY_FIXTURES=1`.
SHELL_FIXTURE = ROOT / "tests" / "fixtures" / "web_account_loyalty" / "shell.json"
UPDATE = os.environ.get("UPDATE_ACCOUNT_LOYALTY_FIXTURES") == "1"


def test_the_jsdom_shell_is_the_page_the_server_renders(app, db, account_copy, member):
    soup, _ = _get(app, db, member, "/my-loyalty", "ru")
    shell = {
        "language": "ru",
        "html": str(soup.select_one(".ae-acl")),
        "island": json.loads(soup.select_one("script#page-data").string),
    }
    if UPDATE:
        SHELL_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        SHELL_FIXTURE.write_text(json.dumps(shell, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert SHELL_FIXTURE.exists(), f"{SHELL_FIXTURE} is missing: run with UPDATE_ACCOUNT_LOYALTY_FIXTURES=1"
    assert json.loads(SHELL_FIXTURE.read_text(encoding="utf-8")) == shell, (
        "shell.json is stale: regenerate it with UPDATE_ACCOUNT_LOYALTY_FIXTURES=1 and re-run accountLoyalty.test.js"
    )
