"""llms.txt must surface the Aqua Club loyalty programme to AI assistants."""

from pathlib import Path

import pytest


@pytest.mark.unit
def test_llms_txt_mentions_loyalty_surfaces():
    import business_app

    text = (Path(business_app.__file__).parent / "static" / "llms.txt").read_text(encoding="utf-8")
    assert "/loyalty-guide" in text
    assert "/api/public/loyalty.json" in text
    assert "Aqua Club" in text
    assert "AquaCoins" in text


@pytest.mark.unit
def test_llms_txt_does_not_qualify_the_tier_discount_by_payment_rail():
    """OWNER DECISION (2026-09-03), deliberate — do not "fix" this by adding the
    condition back.

    llms.txt is what an AI assistant quotes to someone who is not a customer
    yet. The owner's call is that a lead researching Aqua Element should read
    the loyalty programme as a plain benefit; qualifying it with a payment-rail
    exclusion at that stage disappoints the lead before they ever order.

    The condition IS disclosed where it changes a decision someone is actually
    making: the /loyalty-guide tier-card footnote, the bot's payment picker
    (the discount only appears on the cash button), the bot confirmation screen,
    and the web checkout line. This file sits earlier than any of those.

    The trade-off was raised with the owner and reaffirmed: an assistant may
    state the discount without the COD condition, and a customer who then picks
    Click sees no tier discount at checkout. That is accepted.
    """
    import business_app

    text = (Path(business_app.__file__).parent / "static" / "llms.txt").read_text(encoding="utf-8")
    section = text.split("## Aqua Club loyalty programme", 1)[1].split("\n## ", 1)[0]

    for rail_term in ("cash-on-delivery", "cash on delivery", "Click", "Payme"):
        assert rail_term not in section, (
            f"the loyalty section names {rail_term!r}, which re-qualifies the tier "
            "discount by payment rail — see this test's docstring before changing it"
        )
