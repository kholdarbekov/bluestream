"""Reusable Pydantic field types shared across serializers.

`MoneyFloat` is the single source of truth for the project's "Decimal in,
float out" money convention. Money fields are validated/stored as ``Decimal``
(preserving precision and any ``Field(gt=0, ge=0, ...)`` constraints) but
serialize to a JSON ``float`` on the wire.

This replaces the previous anti-pattern of a ``@field_validator`` returning
``float(v)``: an after-mode validator's return value replaces the field value
*without* re-validation, so the model physically stored a ``float`` in a
``Decimal``-typed field. Every ``model_dump`` then emitted
``PydanticSerializationUnexpectedValue(Expected 'decimal' ... input_type=float)``.
A ``PlainSerializer`` does the coercion at serialization time instead, so the
stored value stays a real ``Decimal`` and the warning disappears.

``when_used="always"`` (not ``"json"``) is required: several callsites — e.g.
``serialize_payment`` — dump in Python mode (``model_dump()``), and they have
always received floats. ``"always"`` keeps both ``model_dump()`` and
``model_dump(mode="json")`` emitting floats, byte-identical to the old behaviour.
"""

from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer

__all__ = ["MoneyFloat"]

# Money: stored as Decimal, serialized as float in both python and json modes.
# Use ``Optional[MoneyFloat]`` for nullable money fields (the Optional wrapper
# short-circuits None before this serializer runs) and ``Dict[str, MoneyFloat]``
# for money-valued maps.
MoneyFloat = Annotated[
    Decimal,
    PlainSerializer(lambda v: float(v), return_type=float, when_used="always"),
]
