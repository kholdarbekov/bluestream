"""B4b — the prepaid-credit copy must not promise what the rails cannot deliver.

B4a made a cancelled card/Click order settle as customer PREPAID CREDIT instead
of reversing the money at the gateway. That credit is spendable on CASH/COD
orders ONLY: `CashCollectionService` refuses it for every other rail (nine
`payment_method != PaymentMethod.CASH` early-returns plus a
`Payment.payment_method == PaymentMethod.CASH` filter in
`auto_reserve_against_pending_payments`).

The owner ruled (2026-08-24), after being shown that consequence: accept the
cash-only constraint, but stop the bot promising an auto-apply that will not
happen, and surface the balance to single-account customers.

The CART screen (`telegram_bot/handlers/products.py::show_cart`) is the one
surface that renders this block BEFORE a rail is chosen — the confirmation
screen and the post-order brief are both already gated on
`payment_method == 'cash'`. So the cart copy cannot be made conditional; it has
to be true for both rails, which means naming the condition out loud.

The Russian and Uzbek copy was the sharpest lie: `cod_prepaid_auto_applied_next`
read "Будет зачтено в следующий заказ" / "Keyingi buyurtmaga avtomatik
qo'llaniladi" — "will be credited to your NEXT ORDER", with no rail qualifier at
all, to a customer one tap away from choosing Click.

The category assertion is the one whose absence let
`telegram.payment.marking_codes_unavailable` ship unreachable: the customer bot
loads ONLY `category = 'telegram'` (telegram_bot/i18n.py::load_translations), and
a key seeded anywhere else renders as a humanised English fallback. The expected
category is READ OUT OF THE BOT'S OWN QUERY, never written here — copied from
tests/integration/test_payment_cancel_endpoint_refuses.py.
"""

import re
from pathlib import Path

import pytest

# The one fact, rendered on two screens (cart + orders menu), so it is ONE key.
CASH_ONLY_NOTE = "telegram.payments.prepaid_cash_only"

# The cart-screen block, moved to the canonical seeder by B4b because its copy
# changed. One key, one home.
CART_KEYS = (
    "telegram.cart.cod_prepaid_balance",
    "telegram.cart.cod_prepaid_auto_applied_next",
    "telegram.cart.cod_estimated_payable",
)

# Moved for the other reason: its copy did not lie, but B4b widened its AUDIENCE
# from linked customers only to everyone carrying a balance, and "COD prepaid
# balance" is the wrong label for money that arrived from a cancelled CARD order.
ORDERS_BALANCE_KEY = "telegram.orders.cod_prepaid_balance"

ALL_KEYS = CART_KEYS + (ORDERS_BALANCE_KEY, CASH_ONLY_NOTE)


def _bot_category() -> str:
    """The category the CUSTOMER bot actually SELECTs, parsed from its own SQL."""
    i18n_source = (Path(__file__).resolve().parents[2] / "telegram_bot" / "i18n.py").read_text()
    categories = re.findall(r"category\s*=\s*'([a-z_]+)'", i18n_source)
    assert categories, "could not find the bot's category filter in telegram_bot/i18n.py"
    return categories[0]


@pytest.mark.integration
class TestPrepaidCreditCopyIsRailHonest:
    def test_every_key_is_trilingual_in_the_canonical_seeder(self):
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        for key in ALL_KEYS:
            assert key in BACKEND_TRANSLATIONS, f"{key} is invisible to audit_translation_keys.py"
            assert set(BACKEND_TRANSLATIONS[key]) == {"en", "uz", "ru"}, key
            for language, text in BACKEND_TRANSLATIONS[key].items():
                assert text.strip(), f"{key}/{language} is empty"

    def test_the_keys_are_loadable_by_the_reader_that_consumes_them(self):
        """"The key exists" was never the question — the CATEGORY is.

        `telegram_bot/i18n.py::load_translations` selects one category. A row
        outside it is invisible to the bot, which then renders the humanised
        last key segment ("Prepaid cash only") in all three languages.
        """
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS, _category_for

        bot_category = _bot_category()
        for key in ALL_KEYS:
            assert key in BACKEND_TRANSLATIONS, key
            assert _category_for(key) == bot_category, (
                f"{key} would be seeded under {_category_for(key)!r}, which "
                f"telegram_bot/i18n.py never loads (it reads {bot_category!r})"
            )

        # And the invariant, for EVERY bot-namespace key the canonical seeder
        # owns — not just B4b's. `telegram.payment.marking_codes_unavailable`
        # shipped under category "telegram_bot" and was unreachable for months
        # because nothing swept the whole namespace.
        offenders = [
            key for key in BACKEND_TRANSLATIONS
            if key.startswith("telegram.") and _category_for(key) != bot_category
        ]
        assert not offenders, (
            f"these keys would be seeded outside {bot_category!r}, the only "
            f"category telegram_bot/i18n.py loads: {offenders}"
        )

    def test_one_key_one_home_the_oneoff_seeder_no_longer_owns_them(self):
        """Two seeders owning one key means a reseed silently reverts this copy."""
        from scripts.seed_prepayment_translations import PREPAYMENT_TRANSLATIONS

        for key in ALL_KEYS:
            assert key not in PREPAYMENT_TRANSLATIONS, (
                f"{key} still lives in scripts/seed_prepayment_translations.py; "
                "running it would overwrite the canonical copy"
            )

    def test_the_auto_apply_promise_names_the_rail_in_every_language(self):
        """The lie B4b fixes: an unconditional "applied to your next order".

        A customer holding credit from a cancelled CARD order who then pays by
        Click is charged in full. The copy has to say the condition, in the
        language the customer reads it in — not only in English.
        """
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        copy = BACKEND_TRANSLATIONS["telegram.cart.cod_prepaid_auto_applied_next"]
        assert "cash on delivery" in copy["en"].lower()
        assert "naqd" in copy["uz"].lower()
        assert "наличными" in copy["ru"].lower()

    def test_the_cash_only_note_says_card_and_click_are_charged_in_full(self):
        """Surfacing a balance without saying where it can be spent is half a
        fix — the customer's very next question is "so is my card cheaper?"."""
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        copy = BACKEND_TRANSLATIONS[CASH_ONLY_NOTE]
        assert "click" in copy["en"].lower()
        assert "click" in copy["uz"].lower()
        assert "click" in copy["ru"].lower()

    def test_the_two_balance_labels_stay_word_for_word_identical(self):
        """One number, two screens (cart + orders menu). If the labels drift, a
        customer reading both concludes they are two different pots of money."""
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        assert (
            BACKEND_TRANSLATIONS["telegram.cart.cod_prepaid_balance"]
            == BACKEND_TRANSLATIONS[ORDERS_BALANCE_KEY]
        )

    def test_copy_uses_straight_apostrophes_only(self):
        """This repo keeps copy ASCII-safe; a curly apostrophe elsewhere doubled
        the Eskiz SMS bill by pushing text out of GSM-7."""
        from scripts.seed_backend_translations import BACKEND_TRANSLATIONS

        for key in ALL_KEYS:
            for language in ("en", "uz", "ru"):
                assert "’" not in BACKEND_TRANSLATIONS[key][language], (
                    f"{key}/{language} has a curly apostrophe"
                )
