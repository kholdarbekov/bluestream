"""The driver's ``current_delivery`` snapshot: built from a ``/delivery/active`` row, or patched.

``BaseHandler._anchor_current_delivery`` rebuilds it when a tap names another stop, and the
"Address changed" alert (spec 2026-10-18-admin-order-address-change, D8) refreshes it from the
fresh row, or patches it when that fetch fails. One builder, so the two can never disagree
about what a snapshot holds.

The row lookup lives here too: `active_row_id` and `find_active_row` are the one reading of
a ``/delivery/active`` body, so every reader finds the same stop.
"""
from typing import Any, Dict, Mapping, Optional


def active_row_id(row: Mapping) -> Optional[int]:
    """The delivery id of a ``/delivery/active`` row, under either of its names."""
    return row.get('delivery_id') or row.get('id')


def find_active_row(data: Any, delivery_id: int) -> Optional[Dict[str, Any]]:
    """The row for ``delivery_id`` in a ``/delivery/active`` body, or None.

    The body is the bare list of rows or ``{"items": [...]}``; an empty body has no rows.
    """
    rows = data if isinstance(data, list) else (data or {}).get('items')
    return next((row for row in rows or [] if active_row_id(row) == delivery_id), None)


def snapshot_from_active_row(row: Mapping, delivery_id: int) -> Dict[str, Any]:
    """A new snapshot of ``row``, the stop's ``/delivery/active`` row."""
    # The row IS the card payload `view_active_delivery` whitelists from; copy it
    # wholesale and add only the derived keys that handler renames, so this can
    # never fall behind that whitelist.
    snapshot = dict(row)
    snapshot['delivery_id'] = delivery_id
    snapshot.setdefault('origin_lat', row.get('current_location_lat'))
    snapshot.setdefault('origin_lng', row.get('current_location_lng'))
    snapshot.setdefault('destination_lat', row.get('destination_latitude'))
    snapshot.setdefault('destination_lng', row.get('destination_longitude'))
    return snapshot


def apply_address_change(snapshot: Dict[str, Any], payload: Mapping) -> None:
    """Move ``snapshot`` to the new address from the "Address changed" payload, in place.

    The fallback for a failed ``/delivery/active`` fetch (D8): only the address fields
    move. The money, bottle and place keys stay as they were, so the at-door prompts still
    read the old place until the next fetch (plan decision "D8 patch fallback"). The
    patched ``delivery_address_id`` only draws later screens; a flow already started keeps
    the id it captured.
    """
    snapshot.update(payload["door"])
    snapshot["destination_lat"] = snapshot["destination_latitude"] = payload["lat"]
    snapshot["destination_lng"] = snapshot["destination_longitude"] = payload["lng"]
    snapshot["delivery_address_id"] = payload["delivery_address_id"]
