"""
Delivery-zone enforcement — single source of truth.

``shared.constants.TASHKENT_POLYGON`` defines the official delivery coverage
area, and ``shared.constants.is_within_tashkent`` is the canonical point-in-zone
test. Every backend path that persists or updates an address coordinate funnels
through :func:`ensure_within_delivery_zone` so a coordinate outside the polygon
can never be stored.

Enforcement is layered:
- Service / API layers call :func:`ensure_within_delivery_zone` (or
  ``is_within_tashkent`` directly) to reject early with a clean, localized 400.
- Coordinate-bearing models register :func:`register_delivery_zone_listeners`
  as a ``before_insert`` / ``before_update`` backstop — ``UserAddress`` (see
  [business_app/models/user.py](../models/user.py)) and ``Outlet`` (see
  [business_app/models/sales.py](../models/sales.py)) — so no write path,
  present or future, can bypass the zone check.
"""

from typing import Optional

from shared.constants import is_within_tashkent
from business_app.utils.exceptions import ValidationError

# Reuses the same translation key already served by the reverse-geocode endpoint
# so the message is consistent across every channel.
_OUTSIDE_AREA_KEY = "api.addresses.error.coordinates_outside_supported_area"
_OUTSIDE_AREA_FALLBACK = "The selected location is outside our delivery area (Tashkent)."


def is_in_delivery_zone(latitude, longitude) -> bool:
    """Return ``True`` if the coordinate pair lies inside the delivery polygon."""
    return is_within_tashkent(float(latitude), float(longitude))


def ensure_within_delivery_zone(latitude: Optional[float], longitude: Optional[float]) -> None:
    """Raise :class:`ValidationError` when a *complete* coordinate pair is out of zone.

    No-op when either coordinate is missing — text-only addresses (no GPS) are
    out of scope for polygon validation and are handled separately.

    Args:
        latitude: Latitude, or ``None``.
        longitude: Longitude, or ``None``.

    Raises:
        ValidationError: If both coordinates are present and the point falls
            outside ``TASHKENT_POLYGON`` (or the values are not numeric).
    """
    if latitude is None or longitude is None:
        return

    try:
        lat = float(latitude)
        lng = float(longitude)
    except (TypeError, ValueError):
        raise ValidationError(_outside_area_message())

    if not is_within_tashkent(lat, lng):
        raise ValidationError(_outside_area_message())


def register_delivery_zone_listeners(model_cls) -> None:
    """Attach the SSOT backstop to any model carrying ``latitude``/``longitude``.

    Never persist a coordinate outside ``TASHKENT_POLYGON``: refuse on insert,
    and on update whenever either coordinate actually changed — so legacy
    out-of-zone rows can still be edited for unrelated fields (title,
    is_default, ...). Service / API layers already reject out-of-zone
    coordinates early with a localized 400; this last-line guard makes the
    invariant impossible to bypass from any present or future write path.
    Coordinate-less rows (text-only addresses, un-pinned outlets) are skipped.

    One rule, one implementation: ``UserAddress`` and ``Outlet`` both register
    through here rather than restating the listener trio per model.
    """
    from sqlalchemy import event
    from sqlalchemy import inspect as sa_inspect

    def _enforce(target) -> None:
        if target.latitude is None or target.longitude is None:
            return
        ensure_within_delivery_zone(target.latitude, target.longitude)

    @event.listens_for(model_cls, "before_insert")
    def _zone_before_insert(mapper, connection, target):
        _enforce(target)

    @event.listens_for(model_cls, "before_update")
    def _zone_before_update(mapper, connection, target):
        state = sa_inspect(target)
        if state.attrs.latitude.history.has_changes() or state.attrs.longitude.history.has_changes():
            _enforce(target)


def _outside_area_message() -> str:
    # Imported lazily so this module stays import-safe for the model layer
    # (translations may touch the DB / app context).
    from business_app.utils.translations import get_translation

    message = get_translation(_OUTSIDE_AREA_KEY)
    if not message or message == _OUTSIDE_AREA_KEY:
        return _OUTSIDE_AREA_FALLBACK
    return message
