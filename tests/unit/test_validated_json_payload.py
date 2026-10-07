"""`validated_json_payload`: the one body validator the sales and sales-pay blueprints share.

It replaces two byte-identical `_validated_payload` copies in `admin_sales.py` and `staff_sales.py`
(spec §5.1). Its rule is `exclude_unset`, NOT `exclude_none`:
- a key the client never sent is dropped;
- a null it deliberately sent survives, so emptying an input in the admin UI clears the column.

The `exclude_none` helpers in `staff_tryouts.py`, `admin_tryouts.py` and `admin_bottles.py` are a
different rule and stay where they are.

The route-level proof that nothing changed for the sales routes is the existing
null-clears-the-column pins, in tests/integration/test_admin_sales_outlets_api.py and
tests/integration/test_staff_sales_outlets_api.py, run in Step 6.
"""

from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from business_app.utils.request_helpers import validated_json_payload

ROOT = Path(__file__).resolve().parents[2]


class _Body(BaseModel):
    """A body schema of the shape every sales payload has: optional fields, one bound, no extras."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    notes: Optional[str] = None
    count: int = Field(default=1, ge=1)


def test_a_key_the_client_never_sent_is_dropped_and_a_null_it_sent_survives(app):
    with app.test_request_context(json={"name": "Oasis", "notes": None}):
        assert validated_json_payload(_Body) == {"name": "Oasis", "notes": None}


def test_an_empty_object_validates_as_no_keys(app):
    with app.test_request_context(json={}):
        assert validated_json_payload(_Body) == {}


def test_a_refused_body_is_the_400_every_sales_route_returns(app):
    with app.test_request_context(json={"count": 0, "colour": "red"}):
        response, status = validated_json_payload(_Body)
        body = response.get_json()

    assert status == 400
    assert (body["success"], body["message"]) == (False, "Validation failed")
    assert sorted(body["errors"]) == [
        "colour: Extra inputs are not permitted",
        "count: Input should be greater than or equal to 1",
    ]
    assert "data" not in body  # no error_code: a malformed body is not a coded refusal


def test_the_sales_blueprints_share_the_one_helper():
    for name in ("admin_sales.py", "staff_sales.py"):
        text = (ROOT / "business_app" / "api" / name).read_text(encoding="utf-8")
        assert "def _validated_payload(" not in text, name
        assert "validated_json_payload" in text, name
