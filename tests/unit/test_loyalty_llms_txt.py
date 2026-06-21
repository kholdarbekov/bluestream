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
