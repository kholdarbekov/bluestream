"""Every bottle request body that carries a FLOAT refuses non-finite values.

Python's own ``json`` parser accepts the BARE ``NaN`` / ``Infinity`` /
``-Infinity`` literals and pydantic's default ``float`` carries them straight
through, so a body without ``allow_inf_nan: False`` hands a non-finite quantity
to the service — and Postgres ``numeric`` ACCEPTS ``'NaN'``, which poisons a
place's stored balance permanently (``reconcile_balance`` re-writes the same
poison, because the ledger sum is non-finite too).

The three admin place-write bodies were given that refusal, with a comment in
``business_app/serializers/bottle_serializers.py`` explaining exactly why.
``BottleCollectionRequest`` — the driver's doorstep pickup, the same
``quantity: float`` shape — was left on a plain ``model_config`` and so had no
such refusal.

Two claims here, and the second is the one that keeps this from rotting: the
collection body refuses the three literals, AND *no* body in the module that
declares a float can be added without the refusal. The sweep is what catches
the next one, rather than a reader having to notice a missing line.
"""

import json
import typing

import pytest
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from business_app.serializers import bottle_serializers
from business_app.serializers.bottle_serializers import BottleCollectionRequest

pytestmark = pytest.mark.unit

NON_FINITE_LITERALS = ["NaN", "Infinity", "-Infinity"]


def _request_models():
    """Every pydantic request body declared in the bottle serializer module."""
    for name in dir(bottle_serializers):
        obj = getattr(bottle_serializers, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
            if obj.__module__ == bottle_serializers.__name__:
                yield name, obj


def _declares_a_float(model: type) -> bool:
    for field in model.model_fields.values():
        annotation = field.annotation
        candidates = typing.get_args(annotation) or (annotation,)
        if any(c is float for c in candidates):
            return True
    return False


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_collection_quantity_is_refused(literal):
    """The body as it arrives off the wire: `json.loads` really does produce
    these from the bare literals, so the model is what has to say no."""
    payload = json.loads(
        '{"customer_id": 1, "address_id": 2, "quantity": %s}' % literal
    )

    with pytest.raises(PydanticValidationError):
        BottleCollectionRequest(**payload)


def test_a_finite_collection_quantity_is_still_accepted():
    """The refusal is about finiteness only — an ordinary pickup still parses."""
    body = BottleCollectionRequest(customer_id=1, address_id=2, quantity=3.5)

    assert body.quantity == 3.5


def test_no_float_carrying_bottle_request_body_is_missing_the_refusal():
    """The sweep. A new body with a `float` inherits the fence automatically
    only if it reads the shared config; this fails the day one does not."""
    missing = [
        name
        for name, model in _request_models()
        if _declares_a_float(model) and model.model_config.get("allow_inf_nan") is not False
    ]

    assert not missing, (
        f"these bottle request bodies carry a float without `allow_inf_nan: False`: {missing}"
    )


def test_the_sweep_is_actually_looking_at_something():
    """A guard on the guard: if the module ever stops exposing float bodies the
    sweep above would pass by inspecting nothing at all."""
    floats = [name for name, model in _request_models() if _declares_a_float(model)]

    assert "BottleCollectionRequest" in floats
    assert len(floats) >= 4
