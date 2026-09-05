"""The ♻️ badge on the try-out product picker must reflect the backend's answer.

Two bugs met on one line. It read `product['tracks_returnable_bottles']` at the
TOP level of the payload, but `serialize_product` nests that under `inventory`
— so the badge silently never rendered for anyone. And the raw flag is only
half the fact: a product carrying `tracks=True` with a zero per-unit rate books
no bottles at all, so badging it "returnable" tells the operator something the
ledger will not do.

Both are now answered by the one field the backend publishes,
`inventory.is_returnable_bottle` (see `Product.is_returnable_bottle`).
"""

import pytest

from staff_bot.keyboards.tryouts import TryoutKeyboards


def _product(product_id, name, inventory):
    """The shape `serialize_product` actually returns (see its `inventory` block)."""
    return {"id": product_id, "name": name, "inventory": inventory}


def _labels(markup):
    return [button.text for row in markup.inline_keyboard for button in row]


@pytest.mark.unit
class TestTryoutReturnableBadge:
    def test_returnable_product_is_badged(self):
        markup = TryoutKeyboards.product_list(
            "en",
            [_product(2, "19 litrlik suv", {"is_returnable_bottle": True})],
        )

        assert any("19 litrlik suv ♻️" in label for label in _labels(markup))

    def test_non_returnable_product_is_not_badged(self):
        markup = TryoutKeyboards.product_list(
            "en",
            [_product(3, "10 Litr suv", {"is_returnable_bottle": False})],
        )

        labels = _labels(markup)
        assert any("10 Litr suv" in label for label in labels)
        assert not any("♻️" in label for label in labels)

    def test_a_mixed_picker_badges_only_the_returnable_line(self):
        markup = TryoutKeyboards.product_list(
            "en",
            [
                _product(2, "19 litrlik suv", {"is_returnable_bottle": True}),
                _product(3, "10 Litr suv", {"is_returnable_bottle": False}),
            ],
        )

        badged = [label for label in _labels(markup) if "♻️" in label]
        assert len(badged) == 1
        assert "19 litrlik suv" in badged[0]

    def test_the_flag_alone_does_not_badge_a_product_that_books_nothing(self):
        """`tracks=True, per_unit=0` books zero bottles — it is not returnable."""
        markup = TryoutKeyboards.product_list(
            "en",
            [
                _product(
                    3,
                    "10 Litr suv",
                    {
                        "tracks_returnable_bottles": True,
                        "returnable_bottles_per_unit": 0,
                        "is_returnable_bottle": False,
                    },
                )
            ],
        )

        assert not any("♻️" in label for label in _labels(markup))

    def test_a_flattened_payload_still_badges(self):
        """Fallback for any endpoint that ships the field un-nested."""
        markup = TryoutKeyboards.product_list(
            "en",
            [{"id": 2, "name": "19 litrlik suv", "is_returnable_bottle": True}],
        )

        assert any("♻️" in label for label in _labels(markup))

    def test_quantity_suffix_still_renders_alongside_the_badge(self):
        markup = TryoutKeyboards.product_list(
            "en",
            [_product(2, "19 litrlik suv", {"is_returnable_bottle": True})],
            selected_quantities={2: 3},
        )

        assert any("♻️ · x3" in label for label in _labels(markup))
