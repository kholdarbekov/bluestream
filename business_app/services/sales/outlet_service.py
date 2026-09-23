"""Outlets: creation with dedupe, pipeline stages, activation approval, assignment.

Every stage change goes through _record_stage so outlet_stage_history is complete.
Approval (Task 7) is resumable step by step because the customer/address helpers
commit inside themselves.
"""

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from flask import current_app
from sqlalchemy import and_, or_

from business_app import db
from business_app.models.sales import (
    CONTACT_ROLES,
    ENTITY_SUBTYPE_BY_OUTLET_TYPE,
    LOST_REASONS,
    OUTLET_CLASSES,
    OUTLET_STAGES,
    OUTLET_TYPES,
    PAYMENT_TERMS,
    Outlet,
    OutletContact,
    OutletStageHistory,
    SalesAgentProfile,
)
from business_app.models.tryout import ProductTryout
from business_app.models.user import User, UserAddress
from business_app.services.sales import notifications
from business_app.services.staff_service import StaffService
from business_app.utils.delivery_window import local_now, parse_window_time, validate_schedule
from business_app.utils.exceptions import ConflictError, ForbiddenError, NotFoundError, ValidationError
from business_app.utils.geo_validation import ensure_within_delivery_zone, is_in_delivery_zone
from business_app.utils.helpers import calculate_distance, format_phone_number
from business_app.utils.timezone_utils import ensure_utc
from shared.constants import TASHKENT_DISTRICTS
from shared.enums import EntitySubtype, OrderStatus, UserRole, UserStatus, UserType
from shared.staff_constants import SALES_EVENT_OUTLET_APPROVED, SALES_EVENT_OUTLET_REJECTED, STAFF_ACTIONS
from shared.user_search import expand_name_variants

PROSPECT_STAGES = ("prospect", "trial", "activation_requested")
# Which stages can appear on TODAY'S WORK LIST. Deliberately NOT
# `replenishment_service.DUE_WHEN_NEVER_VISITED_STAGES`: that one answers "which never-visited
# outlet is due NOW" (a trial store is not, it has its own agent date), while this one answers
# "which outlet may appear in the due scope at all" (a trial store with a date may). Two
# questions, two tuples — merging them would either hide trials from the list or make every
# untouched trial due on the day it was created.
DUE_STAGES = ("active", "at_risk", "dormant", "trial")

# The stage job's scope: the stages whose membership is decided by how recently water reached
# the shop. `trial` is deliberately absent — a trial outlet is promoted by a CONVERTED try-out,
# not by an interval (R5) — and `prospect`/`activation_requested`/`lost` have no rhythm at all.
SERVING_STAGES = ("active", "at_risk", "dormant")

# How many inter-order gaps the median is taken over ("the last 5"), and the floor under that
# median in days. The floor is what keeps a shop that takes a bottle every other day out of
# `at_risk` after a single quiet weekend.
INTERVAL_SAMPLE_SIZE = 5
MIN_INTERVAL_DAYS = 7

_SECONDS_PER_DAY = 86400.0


def _norm(text: Optional[str]) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in (text or "").lower())
    return " ".join(cleaned.split())


def names_similar(a: Optional[str], b: Optional[str]) -> bool:
    """True when every token (>= 3 chars) of some transliteration of ``a`` occurs in ``b``."""
    if not a or not b:
        return False
    b_norm = _norm(b)
    for variant in expand_name_variants(a):
        tokens = [t for t in _norm(variant).split() if len(t) >= 3]
        if tokens and all(t in b_norm for t in tokens):
            return True
    return False


def _names_match(a: Optional[str], b: Optional[str]) -> bool:
    """Symmetric `names_similar`, which is one-directional by design (containment).

    Dedupe must not depend on which shop the agent entered first: "Bahor" against an existing
    "Bahor market" and "Bahor market" against an existing "Bahor" are the same question.
    """
    return names_similar(a, b) or names_similar(b, a)


def _format_optional_phone(raw: Optional[str], error_code: str = "SALES_CONTACT_PHONE_INVALID") -> Optional[str]:
    if raw in (None, ""):
        return None
    formatted = format_phone_number(raw)
    if not formatted:
        raise ValidationError("Invalid phone number format", error_code=error_code)
    return formatted


def _user_display_name(user: User) -> str:
    return user.company_name or user.full_name or user.phone or f"#{user.id}"


# Locale words a geocoder appends to a district NAME: "Chilanzar District",
# "Chilonzor Tumani", "Чиланзарский район". Stripped before the name match is
# retried, never instead of it.
_DISTRICT_SUFFIX_TOKENS = ("district", "tumani", "район", "rayon")

# Every apostrophe a name can be spelled with. Uzbek needs one -- "Ulug'bek",
# "Shayxontohur" -- and nobody agrees which codepoint it is: our own constants
# use U+0027, live Nominatim answers "Mirzo Ulug‘bek Tumani" with U+2018, and
# the Uzbek orthographic character is U+02BB. Folded away on BOTH sides so the
# match is about the letters, never about which quote mark a provider chose.
_APOSTROPHES = "'’‘ʻʼ`"

# Provider spellings that survive folding and still name no district. Kept as a
# tiny explicit table rather than fuzzy matching: a wrong district is worse than
# no district, and an entry here is a decision someone made on real evidence.
_DISTRICT_ALIASES = {
    # Live Nominatim, e.g. 41.32/69.24 -> county "Shayhontohur Tumani". Ours are
    # "Shaykhontohur" (en) and "Shayxontohur" (uz); the provider drops the k/x.
    "shayhontohur": "shaykhontohur",
}


def _fold_district(raw: Optional[str]) -> str:
    """Lowercase and drop every apostrophe variant. The comparison form."""
    text = (raw or "").strip().lower()
    for apostrophe in _APOSTROPHES:
        text = text.replace(apostrophe, "")
    return text.strip()


def _district_display_names() -> Dict[str, str]:
    """Every en/uz/ru display name, in comparison form, mapped to its canonical key."""
    return {
        _fold_district(names.get(language)): key
        for key, names in TASHKENT_DISTRICTS.items()
        for language in ("en", "uz", "ru")
        if names.get(language)
    }


def _canonical_district(raw: Optional[str]) -> Optional[str]:
    """Map free text onto a TASHKENT_DISTRICTS key, or None when nothing matches.

    `addresses.district` is free text, but every outlet write path (`_validate_common`,
    `bulk_assign_by_district`, `SalesAgentProfile.districts`) speaks canonical KEYS. Copying an
    address's district verbatim into an imported outlet therefore mints rows carrying "Chilonzor"
    or "Алмазар" that no district filter and no bulk assignment can ever reach. The inverse of
    `shared.constants.get_district_name`: accepts the key or any of its en/uz/ru display names.

    It also has to accept what a GEOCODER says, because `POST /addresses/reverse-geocode` is what
    the staff bot's "New outlet" pin step forwards: live Nominatim answers "Yashnobod Tumani",
    "Mirzo Ulug‘bek Tumani", "Shayhontohur Tumani". So the text is FOLDED (lowercased, every
    apostrophe variant removed), a trailing locale token is stripped, and the remainder matched --
    exactly, then through `_DISTRICT_ALIASES`, then as a PREFIX, because Russian renders the name
    adjectivally ("чиланзарский" for "чиланзар").

    The prefix rule is reachable ONLY after a suffix strip, so ordinary free text ("Mirobod
    street") still resolves to None, and no two folded district names are prefixes of one another
    (asserted in tests/unit/test_outlet_district_canonical.py), so it cannot resolve one district
    onto another. There is deliberately no fuzzy matching: the Russian adjectival stems that are
    not simple prefixes ("Учтепинский" for "Учтепа") return None, and the caller stores NULL,
    because a district guessed wrong is worse than a district left blank.

    This is the outlet write path's single canonicaliser. It is NOT the only name->key matcher in
    the codebase -- `admin_ui/src/pages/Users.js`, `Tryouts.js` and
    `business_app/static/js/address-wizard.js` each carry their own `includes`-based matching for
    their own pickers. Consolidating those is parked, not done.

    Pure: no session, no config, no I/O.
    """
    text = _fold_district(raw)
    if not text:
        return None
    if text in TASHKENT_DISTRICTS:
        return text
    display = _district_display_names()
    if text in display:
        return display[text]
    if text in _DISTRICT_ALIASES:
        return _DISTRICT_ALIASES[text]

    parts = text.split()
    if len(parts) > 1 and parts[-1] in _DISTRICT_SUFFIX_TOKENS:
        stripped = " ".join(parts[:-1])
        if stripped in TASHKENT_DISTRICTS:
            return stripped
        if stripped in display:
            return display[stripped]
        if stripped in _DISTRICT_ALIASES:
            return _DISTRICT_ALIASES[stripped]
        for name, key in display.items():
            if stripped.startswith(name):
                return key
    return None


def _import_outlet_name(
    account_name: str,
    address: Optional[UserAddress],
    *,
    branch_mode: bool,
    account_titles: List[Optional[str]],
) -> str:
    """The name the backfill gives ONE PLACE of one account (D25 rule 2, R22, R41).

    A CHAIN -- an account holding two or more addresses -- is named "<account>, <branch>", because
    two rows called "Chinor" are two rows nobody can tell apart in a list. The branch half is the
    first NON-BLANK of, in order: the address's `street_address`; its `title`, but only when no
    other address of the account carries the same title (compared stripped, case-sensitively);
    the first 40 characters of its free-text `full_address`. The title comes after the street
    because the customer bot writes canned titles ("Uy", "Ish", "Boshqa", "Try-out") that repeat
    inside one chain. A whitespace-only value counts as absent, the label is trimmed of the stray
    comma a cut mid-address leaves behind, and the whole name is capped at `Outlet.name`'s 200.
    Two addresses that still yield the same label keep it -- `name` is agent-editable.

    `account_titles` is the title of EVERY address the account holds, this one's included. The
    function is pure (no session), so the caller hands it what the uniqueness test reads.

    An account with ONE address keeps the bare `<company or full name>` the earlier
    one-outlet-per-account import already wrote. That is not a convenience: every outlet standing
    on prod today carries the short name, `name` is in `AGENT_EDITABLE_OUTLET_FIELDS` so a re-run
    must never rewrite one, and the 60-character label ceilings in the digest and the due list
    would truncate the long form for the majority of customers who have exactly one shop.

    Stated, not fixed (R22): an account that grows from one address to two reads
    "Chinor" + "Chinor, Yunusobod 4" until an agent renames the first by hand -- two rows for the
    office, against long truncated names for everyone else.
    """
    if address is None or not branch_mode:
        return account_name[:200]
    street = (address.street_address or "").strip()
    title = (address.title or "").strip()
    title_is_unique = sum(1 for other in account_titles if (other or "").strip() == title) == 1
    free_text = (address.full_address or "").strip()[:40]
    branch = (street or (title if title_is_unique else "") or free_text).strip(" ,")
    return (f"{account_name}, {branch}" if branch else account_name)[:200]


class OutletService:
    # ------------------------------------------------------------------ stages
    @staticmethod
    def _record_stage(
        outlet: Outlet,
        from_stage: Optional[str],
        to_stage: str,
        actor_id: Optional[int],
        reason_code: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        if to_stage not in OUTLET_STAGES:
            raise ValidationError(f"Unknown stage {to_stage}", error_code="SALES_OUTLET_STAGE_INVALID")
        db.session.add(
            OutletStageHistory(
                outlet_id=outlet.id,
                from_stage=from_stage,
                to_stage=to_stage,
                reason_code=reason_code,
                note=note,
                actor_user_id=actor_id,
            )
        )
        outlet.stage = to_stage
        # D7 (spec :91): a serving outlet that has never been visited is due NOW. The due date is
        # a published COLUMN -- the due scope only READS it, so NULL means "not due" -- and its
        # only other writer is `VisitService.close`. Without this, every outlet activated before
        # its first visit stayed NULL and the work list it is supposed to head shipped empty.
        # The stage is an INPUT to the rule (`DUE_WHEN_NEVER_VISITED_STAGES`), so republishing it
        # on every stage write is the same fact answered once, not a second copy of the rule --
        # and `recompute_outlet` writes a plain NULL back for the stages that are not due.
        # No commit: this runs inside the caller's transaction, which owns it.
        if to_stage in DUE_STAGES:
            from business_app.services.sales.replenishment_service import ReplenishmentService

            ReplenishmentService.recompute_outlet(outlet)

    @staticmethod
    def transition(
        outlet: Outlet,
        to_stage: str,
        actor_id: Optional[int],
        reason_code: Optional[str] = None,
        note: Optional[str] = None,
    ) -> None:
        OutletService._record_stage(outlet, outlet.stage, to_stage, actor_id, reason_code, note)

    # ------------------------------------------------------------- branch mode
    @staticmethod
    def account_outlet_count(user_id: Optional[int]) -> int:
        """How many outlets this customer account holds (D25 rule 1), not counting `lost` ones.

        One indexed `SELECT count(*)` per call, on `ix_outlets_user_id` — the index migration
        e0f1a2b3c4d5 put where `uq_outlets_user_id` used to be. The card calls it for
        `branch_count`, and `is_branch` calls it -- including from inside `order_scope`, which
        every per-branch history query builds -- so a card, a recompute or a stock check asks it
        several times. It is never asked per row of a list: the list serializers deliberately
        publish no account fields.

        Lost outlets are excluded (R45): a junk import branch marked lost must not keep a real
        shop in branch mode.
        """
        if user_id is None:
            return 0
        return Outlet.query.filter(Outlet.user_id == user_id, Outlet.stage != "lost").count()

    @staticmethod
    def is_branch(outlet: Outlet) -> bool:
        """True when this outlet is one of SEVERAL live (not lost) outlets on its account — "branch
        mode" (D25 rule 5, R45).

        The ONE expression of that question. Every per-branch rule (the four replenishment
        history queries, the card's last orders, the money label) asks it here instead of
        counting outlets itself, so a chain can never be a chain for the consumption rate and a
        single shop for the stage job. A single-outlet account is deliberately NOT in branch
        mode: its legacy orders carry no `delivery_address_id`, and scoping them to an address
        would zero the history the rate is computed from.
        """
        if outlet is None or outlet.user_id is None:
            return False
        return OutletService.account_outlet_count(outlet.user_id) >= 2

    @staticmethod
    def order_scope(outlet: Outlet):
        """The WHERE clause for "the orders that belong to THIS outlet".

        Account-wide for a single-outlet account (its whole history, legacy address-less rows
        included); narrowed to the branch's own delivery address in branch mode. Returned as a
        clause rather than applied here so the four `ReplenishmentService` history queries and
        the card's `last_orders` share one rule instead of five copies of a conjunct.

        A branch with NO address of its own is deliberately not narrowed: `address_id` is
        nullable, and `Order.delivery_address_id == None` renders `delivery_address_id IS NULL`,
        so the conjunct would hand that outlet every legacy address-less order on the account —
        the exact bug this helper exists to remove, inverted. It falls back to the account
        clause until the office gives it an address.
        """
        from business_app.models.order import Order

        clauses = [Order.user_id == outlet.user_id]
        if OutletService.is_branch(outlet) and outlet.address_id is not None:
            clauses.append(Order.delivery_address_id == outlet.address_id)
        return and_(*clauses)

    # ------------------------------------------------------------------ dedupe
    @staticmethod
    def find_duplicates(
        name: Optional[str], phone: Optional[str], latitude: Optional[float], longitude: Optional[float]
    ) -> List[Dict[str, Any]]:
        candidates: Dict[Tuple[str, int], Dict[str, Any]] = {}
        formatted_phone = format_phone_number(phone) if phone else None

        if formatted_phone:
            for user in User.query.filter(User.phone == formatted_phone).all():
                candidates[("user", user.id)] = {
                    "kind": "customer",
                    "user_id": user.id,
                    "outlet_id": None,
                    "name": _user_display_name(user),
                    "phone": user.phone,
                    "distance_m": None,
                    "reason": "phone",
                }
            for contact in OutletContact.query.filter(OutletContact.phone == formatted_phone).all():
                candidates[("outlet", contact.outlet_id)] = {
                    "kind": "outlet",
                    "user_id": contact.outlet.user_id,
                    "outlet_id": contact.outlet_id,
                    "name": contact.outlet.name,
                    "phone": contact.phone,
                    "distance_m": None,
                    "reason": "phone",
                }

        if name and latitude is not None and longitude is not None:
            radius_km = current_app.config["SALES_DEDUPE_RADIUS_M"] / 1000.0
            # SQL pre-filter before the geodesic check. Derived from the configured radius, never
            # hardcoded: a fixed box would silently cap SALES_DEDUPE_RADIUS_M (a degree of longitude
            # is only ~836 m at Tashkent's latitude), so widening the radius would stop widening.
            lat_delta = radius_km / 111.32
            lng_delta = radius_km / (111.32 * max(math.cos(math.radians(latitude)), 0.01))
            lat_lo, lat_hi = latitude - lat_delta, latitude + lat_delta
            lng_lo, lng_hi = longitude - lng_delta, longitude + lng_delta

            nearby_outlets = Outlet.query.filter(
                Outlet.latitude.between(lat_lo, lat_hi), Outlet.longitude.between(lng_lo, lng_hi)
            ).all()
            for outlet in nearby_outlets:
                distance_km = calculate_distance(latitude, longitude, outlet.latitude, outlet.longitude)
                if distance_km <= radius_km and _names_match(name, outlet.name):
                    candidates.setdefault(
                        ("outlet", outlet.id),
                        {
                            "kind": "outlet",
                            "user_id": outlet.user_id,
                            "outlet_id": outlet.id,
                            "name": outlet.name,
                            "phone": outlet.primary_contact.phone if outlet.primary_contact else None,
                            "distance_m": round(distance_km * 1000),
                            "reason": "name_nearby",
                        },
                    )

            addresses = (
                UserAddress.query.join(User, User.id == UserAddress.user_id)
                .filter(UserAddress.latitude.between(lat_lo, lat_hi), UserAddress.longitude.between(lng_lo, lng_hi))
                .all()
            )
            for address in addresses:
                user = address.user
                distance_km = calculate_distance(latitude, longitude, address.latitude, address.longitude)
                if distance_km <= radius_km and _names_match(name, _user_display_name(user)):
                    candidates.setdefault(
                        ("user", user.id),
                        {
                            "kind": "customer",
                            "user_id": user.id,
                            "outlet_id": None,
                            "name": _user_display_name(user),
                            "phone": user.phone,
                            "distance_m": round(distance_km * 1000),
                            "reason": "name_nearby",
                        },
                    )

        # D25 rule 4: an outlet that already belongs to an account THIS lookup named is a
        # branch of it, not a duplicate of the shop being registered — one account, one outlet
        # per address. Labelled here, once, so every renderer (the bot's duplicate screen, the
        # 409's details, the admin drawer) reads the same answer instead of re-deriving it
        # from `user_id`. The account itself keeps `kind: "customer"`: that is the Link row.
        account_ids = {
            candidate["user_id"]
            for candidate in candidates.values()
            if candidate["kind"] == "customer" and candidate["user_id"] is not None
        }
        for candidate in candidates.values():
            if candidate["kind"] == "outlet" and candidate["user_id"] in account_ids:
                candidate["kind"] = "sibling"
                candidate["reason"] = "same_account_branch"

        return list(candidates.values())

    # ------------------------------------------------------------------ create
    @staticmethod
    def _validate_common(payload: Dict[str, Any]) -> Dict[str, Any]:
        outlet_type = payload.get("outlet_type")
        if outlet_type not in OUTLET_TYPES:
            raise ValidationError("Invalid outlet_type", error_code="SALES_OUTLET_TYPE_INVALID")
        name = (payload.get("name") or "").strip()
        if not name:
            raise ValidationError("name is required", error_code="SALES_OUTLET_NAME_REQUIRED")
        latitude, longitude = payload.get("latitude"), payload.get("longitude")
        if (latitude is None) != (longitude is None):
            raise ValidationError(
                "latitude and longitude must be given together", error_code="SALES_OUTLET_PIN_REQUIRED"
            )
        if latitude is not None:
            try:
                ensure_within_delivery_zone(float(latitude), float(longitude))
            except ValidationError as exc:
                raise ValidationError(exc.message, error_code="SALES_OUTLET_OUTSIDE_ZONE") from exc
        # Canonicalised HERE, in the one place every outlet write passes through, rather than by
        # each caller: the staff bot forwards what the reverse-geocoder said, the admin UI sends a
        # key, and an import copies an address's free text. A second copy of this rule in the bot
        # would be a rule with two homes.
        #
        # An unresolvable value is NOT a refusal. On this path the district is a machine-derived,
        # optional CLASSIFICATION -- nobody types it, and the outlet's real location is the pin
        # that was already validated against the delivery polygon above. Killing a completed field
        # walk-in at the final Confirm tap, over a hint the sales agent never entered and cannot
        # correct, costs a customer; a NULL an admin fixes in the Outlets drawer costs a click.
        # That repair is real, not aspirational: `UpdateOutletPayload.district` +
        # `OutletService.update` back the district Select in admin_ui/src/pages/Outlets.js.
        # (`bulk_assign_by_district` and `staff_service._validate_districts` still raise
        # SALES_DISTRICT_INVALID: there a human TYPED the district and a typo must be told.)
        raw_district = (payload.get("district") or "").strip()
        district = _canonical_district(raw_district)
        if raw_district and district is None:
            current_app.logger.info(
                "Outlet district %r matched no TASHKENT_DISTRICTS entry; storing NULL", raw_district
            )
        outlet_class = payload.get("outlet_class") or None
        if outlet_class and outlet_class not in OUTLET_CLASSES:
            raise ValidationError("class must be A, B or C", error_code="SALES_OUTLET_CLASS_INVALID")
        return {
            "outlet_type": outlet_type,
            "name": name[:200],
            "channel": (payload.get("channel") or None),
            "latitude": float(latitude) if latitude is not None else None,
            "longitude": float(longitude) if longitude is not None else None,
            "address_text": (payload.get("address_text") or "").strip() or None,
            "district": district,
            "outlet_class": outlet_class,
            "preferred_language": (payload.get("preferred_language") or "uz")[:5],
            "notes": (payload.get("notes") or "").strip() or None,
        }

    @staticmethod
    def _contact_fields(raw: Optional[Dict[str, Any]], fallback_name: str) -> Optional[Dict[str, Any]]:
        """Normalize one contact payload, or None when it carries neither a name nor a phone.

        `outlet_contacts.name` is NOT NULL, so a phone-only payload borrows the outlet name
        rather than being written as a null.
        """
        raw = raw or {}
        phone = _format_optional_phone(raw.get("phone"))
        name = (raw.get("name") or "").strip()
        if not name and not phone:
            return None
        role = raw.get("role") or "owner"
        if role not in CONTACT_ROLES:
            raise ValidationError("Invalid contact role", error_code="SALES_CONTACT_ROLE_INVALID")
        return {
            "name": (name or fallback_name)[:100],
            "phone": phone,
            "role": role,
            "presence_window": (raw.get("presence_window") or None),
        }

    @staticmethod
    def create(
        agent_user_id: int, payload: Dict[str, Any], *, force: bool = False, link_user_id: Optional[int] = None
    ) -> Outlet:
        fields = OutletService._validate_common(payload)
        contact = OutletService._contact_fields(payload.get("contact"), fields["name"])
        contact_phone = contact["phone"] if contact else None

        link_user: Optional[User] = None
        candidates: List[Dict[str, Any]] = []
        if link_user_id is not None:
            link_user = User.query.get(link_user_id)
            if link_user is None:
                raise NotFoundError("Customer not found", error_code="STAFF_USER_NOT_FOUND")
            # No per-ACCOUNT refusal since e0f1a2b3c4d5: an account owns one outlet per ADDRESS
            # (D25 rule 3). What IS refused (R39) is the account already having an outlet at this
            # pin's place — Link skips `find_duplicates`, so `_same_place_outlets` is this path's
            # own duplicate check. `force` overrides it exactly as it overrides the plain dedupe
            # 409 (R40), and the hits are kept in `dedupe_candidates` below the same way. Anything
            # else links, and `_adopt_or_create_address` adopts the account's free address nearest
            # the pin within SALES_DEDUPE_RADIUS_M or creates the branch's own;
            # `uq_outlets_address_id` is the backstop.
            OutletService._assert_linkable(fields["outlet_type"], link_user)
            candidates = OutletService._same_place_outlets(link_user.id, fields["latitude"], fields["longitude"])
            if candidates and not force:
                raise ConflictError(
                    "Possible duplicate outlet",
                    details={"candidates": candidates},
                    error_code="SALES_OUTLET_DUPLICATE",
                )
        else:
            candidates = OutletService.find_duplicates(
                fields["name"], contact_phone, fields["latitude"], fields["longitude"]
            )
            # Siblings never block: they answer "this chain already has branches", which is
            # expected. What still stops the create is a genuine duplicate — or the ACCOUNT
            # itself, whose `customer` row is the 🔗 Link the agent is meant to tap. The full
            # list travels in the details either way, siblings included, so the screen can
            # show the chain it belongs to. This filter is a STATEMENT of the rule, not a live
            # branch: a candidate only becomes a sibling because the same lookup already named
            # its account as a `customer` candidate, and that row blocks — so keep it as the
            # one place the rule is written, and do not treat it as covered by a test.
            blocking = [candidate for candidate in candidates if candidate["kind"] != "sibling"]
            if blocking and not force:
                raise ConflictError(
                    "Possible duplicate outlet",
                    details={"candidates": candidates},
                    error_code="SALES_OUTLET_DUPLICATE",
                )

        outlet = Outlet(
            **fields,
            stage="prospect",
            assigned_agent_user_id=agent_user_id,
            onboarded_by_user_id=agent_user_id,
            dedupe_candidates=candidates if force else [],
        )
        db.session.add(outlet)
        db.session.flush()
        if contact:
            db.session.add(OutletContact(outlet_id=outlet.id, is_primary=True, **contact))
            db.session.flush()

        # The birth row is written BEFORE any linking. _create_customer commits this session, so an
        # outlet that reaches the database at stage=prospect must already carry the history row that
        # explains it — otherwise a failure mid-activation leaves a durable row with no history.
        OutletService._record_stage(outlet, None, "prospect", agent_user_id, reason_code="created")

        try:
            if link_user is not None:
                OutletService._link_customer(outlet, link_user, agent_user_id)
            elif fields["outlet_type"] == "individual" and contact_phone:
                OutletService._activate_individual(outlet, contact["name"], contact_phone, agent_user_id)
        except Exception:
            # Non-request callers (Celery, scripts) keep this session; never leave a half-built
            # outlet in it for the next unit of work to commit by accident.
            db.session.rollback()
            raise

        db.session.commit()
        StaffService._log_activity(
            user_id=agent_user_id,
            action=STAFF_ACTIONS["OUTLET_CREATED"],
            entity_type="outlet",
            entity_id=outlet.id,
            metadata_={
                "outlet_type": outlet.outlet_type,
                "stage": outlet.stage,
                "forced": bool(force),
                "linked_user_id": link_user_id,
            },
        )
        return outlet

    @staticmethod
    def add_contact(outlet: Outlet, payload: Dict[str, Any]) -> OutletContact:
        fields = OutletService._contact_fields(payload, outlet.name)
        if fields is None:
            raise ValidationError("Contact needs a name or a phone", error_code="SALES_CONTACT_PHONE_INVALID")
        contact = OutletContact(outlet_id=outlet.id, is_primary=not outlet.contacts, **fields)
        db.session.add(contact)
        db.session.commit()
        return contact

    @staticmethod
    def get_contact(outlet: Outlet, contact_id: int) -> OutletContact:
        contact = OutletContact.query.filter_by(id=contact_id, outlet_id=outlet.id).first()
        if contact is None:
            raise NotFoundError("Contact not found", error_code="SALES_CONTACT_NOT_FOUND")
        return contact

    @staticmethod
    def update_contact(outlet: Outlet, contact: OutletContact, payload: Dict[str, Any]) -> OutletContact:
        """Edit one contact (D30). Normalised by `_contact_fields`, exactly as a new one is.

        Never touches the linked customer account: the contacts are the agent's address book,
        and the customer's phone is their login. Before approval, `account_candidate` follows the
        primary phone on its own -- it is read from the contacts on every card.
        """
        if payload.get("is_primary") is False and contact.is_primary:
            raise ValidationError(
                "An outlet keeps one primary contact; make another contact primary instead",
                error_code="SALES_CONTACT_PRIMARY_REQUIRED",
            )
        merged = {
            key: payload[key] if key in payload else getattr(contact, key)
            for key in ("name", "phone", "role", "presence_window")
        }
        fields = OutletService._contact_fields(merged, outlet.name)
        if fields is None:
            raise ValidationError("Contact needs a name or a phone", error_code="SALES_CONTACT_PHONE_INVALID")
        for key, value in fields.items():
            setattr(contact, key, value)
        if payload.get("is_primary"):
            for other in outlet.contacts:
                other.is_primary = other.id == contact.id
        db.session.commit()
        return contact

    @staticmethod
    def delete_contact(outlet: Outlet, contact: OutletContact) -> None:
        """Remove one contact (D30). Deleting the primary promotes the OLDEST remaining contact
        (`outlet.contacts` is ordered by id). The last one may go too; activation then refuses with
        SALES_ACTIVATION_PHONE_REQUIRED, the rule that already guards a phoneless outlet."""
        was_primary = contact.is_primary
        outlet.contacts.remove(contact)  # delete-orphan: the row goes with it
        if was_primary and outlet.contacts:
            outlet.contacts[0].is_primary = True
        db.session.commit()

    # ------------------------------------------------------------------ linking
    @staticmethod
    def _assert_linkable(outlet_type: str, user: User) -> None:
        """One phone never yields two accounts: an existing customer may only be linked when its
        type matches the outlet type. Anything else is SALES_APPROVAL_PHONE_TAKEN."""
        role_value = user.role.value if hasattr(user.role, "value") else user.role
        expected_subtype = ENTITY_SUBTYPE_BY_OUTLET_TYPE.get(outlet_type)
        ok = role_value == UserRole.CUSTOMER.value and (
            (expected_subtype is None and user.user_type == UserType.INDIVIDUAL)
            or (expected_subtype is not None and user.entity_subtype == expected_subtype)
        )
        if not ok:
            raise ConflictError(
                "Phone already belongs to a customer of another type", error_code="SALES_APPROVAL_PHONE_TAKEN"
            )

    @staticmethod
    def _default_address(user: User) -> Optional[UserAddress]:
        return (
            UserAddress.query.filter_by(user_id=user.id)
            .order_by(UserAddress.is_default.desc(), UserAddress.id.asc())
            .first()
        )

    @staticmethod
    def _km_within_dedupe_radius(
        latitude: float, longitude: float, other_latitude: Optional[float], other_longitude: Optional[float]
    ) -> Optional[float]:
        """Kilometres from the pin to the other point when both are the same PLACE, else None.

        "Same place" is `find_duplicates`' own test — within SALES_DEDUPE_RADIUS_M by the same
        `calculate_distance` — so the link path's refusal and its address adoption cannot
        disagree with the dedupe screen about where a shop is. A point with no coordinates is
        nowhere.
        """
        if other_latitude is None or other_longitude is None:
            return None
        distance_km = calculate_distance(latitude, longitude, other_latitude, other_longitude)
        return distance_km if distance_km <= current_app.config["SALES_DEDUPE_RADIUS_M"] / 1000.0 else None

    @staticmethod
    def _same_place_outlets(
        user_id: int, latitude: Optional[float], longitude: Optional[float]
    ) -> List[Dict[str, Any]]:
        """This account's outlets at the pin's place, as `find_duplicates` candidates (R39).

        The 🔗 Link path skips `find_duplicates` — the agent has already picked the account — so
        this is its whole duplicate check: an outlet of the SAME account within
        SALES_DEDUPE_RADIUS_M is this shop, already linked, not a new branch. The account's
        outlets anywhere else are branches and never block (R8). A pin-less create (API-only;
        the bot always sends a pin) has no place to compare and skips the check.
        """
        if latitude is None or longitude is None:
            return []
        found = []
        for outlet in Outlet.query.filter(Outlet.user_id == user_id).order_by(Outlet.id.asc()):
            distance_km = OutletService._km_within_dedupe_radius(latitude, longitude, outlet.latitude, outlet.longitude)
            if distance_km is not None:
                found.append(
                    {
                        "kind": "outlet",
                        "user_id": outlet.user_id,
                        "outlet_id": outlet.id,
                        "name": outlet.name,
                        "phone": outlet.primary_contact.phone if outlet.primary_contact else None,
                        "distance_m": round(distance_km * 1000),
                        "reason": "same_place",
                    }
                )
        return found

    @staticmethod
    def _adopt_or_create_address(outlet: Outlet, user: User) -> None:
        """Give this outlet the address of `user`'s account that IS its place, or a new one (R39).

        One outlet per ADDRESS is the invariant (`uq_outlets_address_id`); one outlet per
        ACCOUNT is not, since e0f1a2b3c4d5. With a pin, the pin decides: the account's address
        NEAREST the pin within SALES_DEDUPE_RADIUS_M that no outlet holds is adopted, and when
        there is none the branch gets a row of its own from the pin (`_create_address`,
        non-default whenever the account already has a default). The default is never adopted
        merely for being free — a chain's office or first shop is not this branch, and adopting
        it would send orders on behalf and branch history to the wrong place. Only a pin-less
        outlet falls back to the default while it is free, else a row of its own. Pin-less is
        API-only or a converted try-out: the bot always sends a pin and `approve` refuses
        pin-less activation (`SALES_ACTIVATION_PIN_REQUIRED`), so the coordinate-less address a
        pin-less link can still mint stays confined to those doors. Written once and shared with
        `_activate_converted_tryouts`, because "which address is this shop" must not be answered
        two ways.
        """
        if outlet.address_id is not None:
            return
        if outlet.latitude is not None and outlet.longitude is not None:
            addresses = UserAddress.query.filter(UserAddress.user_id == user.id).all()
            held = {
                address_id
                for (address_id,) in db.session.query(Outlet.address_id).filter(
                    Outlet.address_id.in_([address.id for address in addresses])
                )
            }
            nearby = []
            for address in addresses:
                if address.id in held:
                    continue
                distance_km = OutletService._km_within_dedupe_radius(
                    outlet.latitude, outlet.longitude, address.latitude, address.longitude
                )
                if distance_km is not None:
                    nearby.append((distance_km, address.id))
            if nearby:
                outlet.address_id = min(nearby)[1]
                return
        else:
            address = OutletService._default_address(user)
            if address is not None and Outlet.query.filter_by(address_id=address.id).first() is None:
                outlet.address_id = address.id
                if address.latitude is not None:
                    outlet.latitude, outlet.longitude = address.latitude, address.longitude
                return
        outlet.address_id = OutletService._create_address(outlet, user).id

    @staticmethod
    def _link_customer(outlet: Outlet, user: User, actor_id: Optional[int]) -> None:
        outlet.user_id = user.id
        OutletService._adopt_or_create_address(outlet, user)
        status_value = user.status.value if hasattr(user.status, "value") else user.status
        if status_value == UserStatus.ACTIVE.value:
            OutletService.transition(outlet, "active", actor_id, reason_code="linked")
            outlet.approved_at = datetime.now(UTC)

    @staticmethod
    def _create_customer(outlet: Outlet, contact_name: str, phone: str, actor_id: int) -> User:
        from business_app.services.auth_service import AuthService

        user_type = "individual" if outlet.outlet_type == "individual" else "entity"
        subtype = ENTITY_SUBTYPE_BY_OUTLET_TYPE.get(outlet.outlet_type)
        first_name, _, last_name = (contact_name or outlet.name).strip().partition(" ")
        try:
            user = AuthService().create_user_by_admin(
                phone=phone,
                first_name=first_name[:100],
                created_by_admin_id=actor_id,
                last_name=last_name.strip()[:100] or None,
                company_name=outlet.name if user_type == "entity" else None,
                tax_id=outlet.tax_id or None,
                user_type=user_type,
                entity_subtype=subtype.value if subtype else None,
                registration_source="sales_agent",
            )
        except ConflictError as exc:
            raise ConflictError("Phone already belongs to a customer", error_code="SALES_APPROVAL_PHONE_TAKEN") from exc
        if outlet.preferred_language:
            user.preferred_language = outlet.preferred_language
        return user

    @staticmethod
    def _create_address(outlet: Outlet, user: User) -> UserAddress:
        address = UserAddress(
            user_id=user.id,
            title=outlet.name[:100],
            full_address=outlet.address_text or outlet.name,
            district=outlet.district,
            latitude=outlet.latitude,
            longitude=outlet.longitude,
            is_default=OutletService._default_address(user) is None,
            is_business=outlet.outlet_type != "individual",
        )
        db.session.add(address)
        db.session.flush()
        return address

    @staticmethod
    def _activate_individual(outlet: Outlet, contact_name: str, phone: str, actor_id: int) -> None:
        user = OutletService._create_customer(outlet, contact_name, phone, actor_id)  # commits the user
        outlet.user_id = user.id
        if outlet.latitude is not None:
            outlet.address_id = OutletService._create_address(outlet, user).id
        OutletService.transition(outlet, "active", actor_id, reason_code="activated")
        outlet.approved_at = datetime.now(UTC)
        outlet.approved_by_user_id = actor_id

    # ------------------------------------------------------------------ access
    @staticmethod
    def get(outlet_id: int) -> Outlet:
        outlet = Outlet.query.get(outlet_id)
        if outlet is None:
            raise NotFoundError("Outlet not found", error_code="SALES_OUTLET_NOT_FOUND")
        return outlet

    @staticmethod
    def get_for_agent(agent_user_id: int, outlet_id: int) -> Outlet:
        outlet = OutletService.get(outlet_id)
        if outlet.assigned_agent_user_id == agent_user_id or outlet.onboarded_by_user_id == agent_user_id:
            return outlet
        profile = SalesAgentProfile.query.filter_by(user_id=agent_user_id).first()
        districts = list(profile.districts or []) if profile else []
        if outlet.assigned_agent_user_id is None and outlet.stage in PROSPECT_STAGES and outlet.district in districts:
            return outlet
        raise ForbiddenError("Outlet is not assigned to you", error_code="SALES_OUTLET_NOT_ASSIGNED")

    @staticmethod
    def agent_outlet_filter(agent_user_id: int):
        """SQL predicate: the outlets THIS agent can open.

        Assigned OR onboarded-by, written once. An agent who registered a shop keeps seeing it
        after a manager reassigns the territory, and the due list, the morning digest and the
        *Nearby* list must agree about that — a digest naming an outlet whose card answers 403
        is a message the agent cannot act on.
        """
        return or_(Outlet.assigned_agent_user_id == agent_user_id, Outlet.onboarded_by_user_id == agent_user_id)

    @staticmethod
    def _name_filter(search: str):
        clauses = []
        for variant in expand_name_variants(search.strip()):
            tokens = [t for t in _norm(variant).split() if t]
            if tokens:
                clauses.append(and_(*[Outlet.name.ilike(f"%{t}%") for t in tokens]))
        return or_(*clauses) if clauses else None

    @staticmethod
    def list_for_agent(
        agent_user_id: int,
        scope: str,
        *,
        search: Optional[str] = None,
        page: int = 1,
        per_page: int = 20,
        now: Optional[datetime] = None,
    ) -> Tuple[List[Outlet], int]:
        """`now` is the instant the caller is answering for.

        `GET /sales/outlets` passes nothing and reads the wall clock, as before. The morning
        digest passes the ONE moment its whole payload is built from, so the membership of
        "due today" and the `overdue_days` printed beside each row cannot straddle a day
        boundary between two queries of the same message.
        """
        query = Outlet.query.filter(OutletService.agent_outlet_filter(agent_user_id))
        order_by = Outlet.name.asc()
        if scope == "prospects":
            query = query.filter(Outlet.stage.in_(PROSPECT_STAGES))
        elif scope == "due":
            # D7: "is this outlet due" is a question the backend already ANSWERED, into
            # `outlets.next_visit_due_at`. This scope only reads it, so the due list, the card's
            # overdue line and the nightly recompute can never disagree.
            #
            # NULL is not due: a never-scheduled outlet belongs in *Prospects*, not at the top of
            # today's work list. And the boundary is the end of the agent's LOCAL day, not UTC --
            # an outlet due at 20:00 Tashkent is due TODAY, and a UTC-midnight comparison hides it
            # until tomorrow morning, every single evening (the `_driver_day_start_utc` lesson).
            query = query.filter(*OutletService._due_clauses(now))
            order_by = Outlet.next_visit_due_at.asc()
        else:
            query = query.filter(Outlet.stage != "lost")
        if search:
            clause = OutletService._name_filter(search)
            if clause is not None:
                query = query.filter(clause)
        pagination = query.order_by(order_by).paginate(page=page, per_page=min(per_page, 100), error_out=False)
        return list(pagination.items), pagination.total

    @staticmethod
    def _due_clauses(now: Optional[datetime] = None):
        """The due scope's membership, as clauses — used by the list AND by the count.

        Written once because the morning digest's "+N more", the nightly plan
        snapshot and the hub's *Due today & overdue* row are all the same number:
        a second spelling is how a manager's plan-vs-fact denominator ends up
        disagreeing with the list the agent is holding.
        """
        from business_app.services.sales.replenishment_service import ReplenishmentService

        return (
            Outlet.stage.in_(DUE_STAGES),
            Outlet.next_visit_due_at.isnot(None),
            Outlet.next_visit_due_at <= ReplenishmentService.local_day_end_utc(now),
        )

    @staticmethod
    def due_counts(agent_user_id: int, *, now: datetime) -> Tuple[int, int]:
        """`(due incl. overdue, overdue)` for one agent at one instant.

        The first number is the due scope's own total — the digest reads it for
        "+N more" and the 01:20 job stores it as the day's plan. The second is the
        same split the digest, the due-list button and the outlet card print:
        `overdue_days(due_at, now) >= 1`, which is exactly `due_at <= now - 1 day`
        (`serializers/sales_serializers.py:285` computes `(now - due_at).days`).
        It is spelled in SQL here because a COUNT cannot call the helper, and
        `tests/unit/test_agent_day_plan_snapshot.py` asserts the two agree on the
        same fixture rather than trusting the equivalence.
        """
        moment = ensure_utc(now)
        query = Outlet.query.filter(
            OutletService.agent_outlet_filter(agent_user_id), *OutletService._due_clauses(moment)
        )
        return query.count(), query.filter(Outlet.next_visit_due_at <= moment - timedelta(days=1)).count()

    @staticmethod
    def nearby(
        agent_user_id: int, *, lat: Optional[float], lng: Optional[float], limit: int
    ) -> List[Tuple[Outlet, float]]:
        """The agent's own outlets, nearest first, measured from the pin they just sent (R1).

        Distance is a RULE, computed here and published as `distance_m` (D15). It uses the
        same `calculate_distance` the check-in geofence and the dedupe radius use, so the
        three surfaces can never disagree about how far apart two pins are.

        There is deliberately NO radius. An agent standing in a dense district must see the
        shop they are standing next to, and a radius is precisely what would hide it; the
        screen is bounded by a COUNT (`SALES_NEARBY_LIMIT`) instead, so the list is short
        without the answer being narrow.

        Unpinned outlets are ABSENT, not last: "how far away is it" is a question they have
        no answer to, and a null sorted in somewhere would read as a measurement. `lost`
        outlets are absent for the same reason the `all` scope drops them -- they are not
        work. Every other stage is present, because a prospect the agent happens to be
        standing outside of is exactly what this screen exists for.

        The visibility clause is `OutletService.agent_outlet_filter` — the SQL expression of
        "the outlets this agent can open", extracted in Task 3 and shared with the due list
        and the morning digest; `get_for_agent` holds the ORM twin, which carries a third arm
        this filter does not (backlog). It is called rather than re-spelled: a second
        `assigned OR onboarded_by` here is how a Nearby row appears for a shop whose card
        answers 403, or disappears from under an agent who registered it. Only the RANKING is
        local, because `list_for_agent` also paginates and orders in SQL and merging the two
        would put a Python sort behind a LIMIT/OFFSET, which silently truncates by name
        before it ever measures anything.
        """
        if lat is None or lng is None or not (-90.0 <= float(lat) <= 90.0) or not (-180.0 <= float(lng) <= 180.0):
            # Refused HERE, not at the route: `request.args.get(type=float)` answers None for
            # a missing pin AND for "abc", and geopy raises ValueError on an out-of-range one
            # -- a 500 with a traceback where the bot expects a code it can render. One code
            # for every way a pin can be unusable.
            raise ValidationError(
                "A valid pin is required for the nearby list",
                details={"lat": lat, "lng": lng},
                error_code="SALES_NEARBY_PIN_REQUIRED",
            )
        candidates = Outlet.query.filter(
            OutletService.agent_outlet_filter(agent_user_id),
            Outlet.stage != "lost",
            Outlet.latitude.isnot(None),
            Outlet.longitude.isnot(None),
        ).all()
        measured = [
            (outlet, calculate_distance(float(lat), float(lng), outlet.latitude, outlet.longitude) * 1000.0)
            for outlet in candidates
        ]
        # Id breaks the tie, so two outlets sharing a pin (the shop and the flat above it)
        # come back in the same order on every request: a list that reshuffles between taps
        # is a list the agent cannot tap twice.
        measured.sort(key=lambda pair: (pair[1], pair[0].id))
        return measured[: max(0, int(limit))]

    @staticmethod
    def update(outlet: Outlet, payload: Dict[str, Any], actor_id: int) -> Outlet:
        # FIRST, before a single `setattr`: this method writes name, channel, class, the
        # nullable loop, preferred_language, district, tax_id and payment_terms straight onto
        # the instance, so a refusal raised further down left every one of those assignments
        # sitting on the session — and the next successful write committed the edit the server
        # had already answered 400 to (L50). Validating up here is what makes the refusal total.
        cadence_before = (outlet.outlet_class, outlet.cadence_days_override)
        window = {}
        for key in ("delivery_window_start", "delivery_window_end"):
            if key in payload:
                # parse_window_time is the repo's SSOT for "HH:MM" -> time (business_app/utils/
                # delivery_window.py). Parsing it here by hand would be a second definition of the
                # same shape, and a blind split(":") turns "25:99" into a 500 instead of a 400.
                try:
                    window[key] = parse_window_time(payload[key])
                except (AttributeError, TypeError, ValueError) as exc:
                    raise ValidationError(f"{key} must be HH:MM", error_code="SALES_WINDOW_INVALID") from exc
        if window:
            # The RESULTING pair is what has to hold — an edit may carry one edge and meet the
            # other on the row — and the rule is `validate_schedule`'s, asked with no date so that
            # only its window-order clause can speak. A second hand-written `start < end` here
            # would be the same rule twice. An inverted window is storable nonsense that ruling 34
            # then offers as the default on every order the agent places at this store, where the
            # same validator refuses it and silently drops the store's own hours.
            start = window.get("delivery_window_start", outlet.delivery_window_start)
            end = window.get("delivery_window_end", outlet.delivery_window_end)
            errors = validate_schedule(None, start, end, now_local=local_now())
            if errors:
                raise ValidationError("; ".join(errors), validation_errors=errors, error_code="SALES_WINDOW_INVALID")
            for key, value in window.items():
                setattr(outlet, key, value)
        if "name" in payload:
            outlet.name = (payload["name"] or "").strip()[:200] or outlet.name
        if "channel" in payload:
            outlet.channel = payload["channel"] or None
        if "outlet_class" in payload:
            if payload["outlet_class"] and payload["outlet_class"] not in OUTLET_CLASSES:
                raise ValidationError("class must be A, B or C", error_code="SALES_OUTLET_CLASS_INVALID")
            outlet.outlet_class = payload["outlet_class"] or None
        for key in (
            "cadence_days_override",
            "opening_hours",
            "preferred_visit_window",
            "legal_form",
            "competitor_note",
            "status_warning",
            "notes",
        ):
            if key in payload:
                setattr(outlet, key, payload[key] or None)
        # NOT the nullable loop above: outlets.preferred_language is NOT NULL with a default, so a
        # payload that carries an explicit null/blank means "leave it alone", not "write a null".
        if payload.get("preferred_language"):
            outlet.preferred_language = payload["preferred_language"][:5]
        if "district" in payload:
            # The inverse ruling to `_validate_common`'s, on purpose. There the district is a
            # geocoder's guess about a pin the agent already placed, so an unresolvable one is
            # stored as NULL rather than blocking a finished visit. Here it is an admin's own
            # choice from the geo-config list, so an unresolvable one is a typo -- and a silently
            # NULLed district drops the outlet out of every district filter and out of
            # `bulk_assign_by_district`, which is exactly the state this edit exists to repair.
            raw_district = (payload["district"] or "").strip()
            district = _canonical_district(raw_district)
            if raw_district and district is None:
                raise ValidationError(f"Unknown district {raw_district}", error_code="SALES_DISTRICT_INVALID")
            outlet.district = district
        if "tax_id" in payload:
            outlet.tax_id = (payload["tax_id"] or "").strip().upper() or None
        if "payment_terms" in payload:
            if payload["payment_terms"] not in PAYMENT_TERMS:
                raise ValidationError(
                    "payment_terms must be cash or business_account", error_code="SALES_PAYMENT_TERMS_INVALID"
                )
            outlet.payment_terms = payload["payment_terms"]
        # D29 (owner-confirmed): class and the override are the two inputs of `cadence_days`, so a
        # change to either republishes the due date now rather than at the 01:00 job. The job's
        # own stage gate, not `_set_stage`'s DUE_STAGES: the job republishes a prospect too, so a
        # narrower gate here would only hold today's edit back until tonight.
        from business_app.services.sales.replenishment_service import DUE_DATE_FROZEN_STAGES, ReplenishmentService

        cadence_changed = (outlet.outlet_class, outlet.cadence_days_override) != cadence_before
        if cadence_changed and outlet.stage not in DUE_DATE_FROZEN_STAGES:
            ReplenishmentService.recompute_outlet(outlet)
        db.session.commit()
        return outlet

    # ------------------------------------------------------------------ activation
    @staticmethod
    def request_activation(outlet: Outlet, actor_id: int) -> Outlet:
        if outlet.stage not in ("prospect", "trial"):
            raise ValidationError("Outlet is not a prospect", error_code="SALES_OUTLET_STAGE_INVALID")
        contact = outlet.primary_contact
        if contact is None or not contact.phone:
            raise ValidationError(
                "A contact phone is required to request activation", error_code="SALES_ACTIVATION_PHONE_REQUIRED"
            )
        if outlet.latitude is None:
            raise ValidationError("A pin is required to request activation", error_code="SALES_ACTIVATION_PIN_REQUIRED")
        outlet.activation_requested_at = datetime.now(UTC)
        outlet.rejected_reason = None
        OutletService.transition(outlet, "activation_requested", actor_id, reason_code="requested")
        db.session.commit()
        notifications.notify_activation_requested(outlet)
        StaffService._log_activity(
            user_id=actor_id,
            action=STAFF_ACTIONS["OUTLET_ACTIVATION_REQUESTED"],
            entity_type="outlet",
            entity_id=outlet.id,
        )
        return outlet

    @staticmethod
    def account_candidate(outlet: Outlet) -> Optional[Dict[str, Any]]:
        """The existing customer ACCOUNT this outlet's contact phone already belongs to, or None.

        ONE definition, read in exactly three places: `approve`'s refusal (the 409 that carries it
        as `details.account`), `activation_request_rows` and `card` -- so no renderer ever looks a
        phone up for itself and offers an Attach the backend would refuse (D25 rule 3). The attach
        decision itself does NOT read it: `approve(attach=True)` reads the phone-user lookup,
        because an Attach onto a customer that holds no outlet yet is still a link (R24).

        Narrow on purpose (R24): an account only counts when it ALREADY HOLDS A LIVE (not lost) OUTLET. That is
        exactly the case approval already refused, and it is the only case where "this shop joins
        that account as a branch" is a true sentence. A phone that belongs to a customer with no
        outlet at all -- the orphan `users` row a failed individual activation leaves behind -- is
        linked silently by `approve`, as it always was, and no surface offers an Attach for it.
        None once the outlet HAS an account: there is nothing left to attach it to.
        """
        if outlet.user_id is not None:
            return None
        contact = outlet.primary_contact
        phone = contact.phone if contact else None
        if not phone:
            return None
        user = User.query.filter_by(phone=phone).first()
        if user is None:
            return None
        try:
            OutletService._assert_linkable(outlet.outlet_type, user)
        except ConflictError:
            # A customer of another type is not an account this outlet may join. `approve` still
            # refuses that phone -- `_assert_linkable` raises SALES_APPROVAL_PHONE_TAKEN below --
            # but there is no account to offer, so no surface draws an Attach button for it.
            return None
        outlet_count = OutletService.account_outlet_count(user.id)
        if outlet_count < 1:
            # A customer with no outlet is not a chain. Linking it is the silent recovery path
            # approval has always taken, and nothing about it is an operator decision.
            return None
        return {
            "user_id": user.id,
            "name": _user_display_name(user),
            "outlet_count": outlet_count,
        }

    @staticmethod
    def list_activation_requests() -> List[Outlet]:
        return (
            Outlet.query.filter(Outlet.stage == "activation_requested")
            .order_by(Outlet.activation_requested_at.asc())
            .all()
        )

    @staticmethod
    def activation_request_rows() -> List[Dict[str, Any]]:
        """The operator's queue as its renderers read it: the outlet row plus the account its
        contact phone already belongs to (R6).

        The candidate is resolved HERE because the bot's review card is rendered out of this
        LIST and must never look a phone up for itself -- a plain Approve on a row that has a
        candidate is the 409, so the button the operator sees has to come from the same answer.
        One phone query per pending row, on a list that is short by construction.
        """
        from business_app.serializers.sales_serializers import serialize_outlet

        return [
            {**serialize_outlet(outlet), "account_candidate": OutletService.account_candidate(outlet)}
            for outlet in OutletService.list_activation_requests()
        ]

    @staticmethod
    def approve(outlet_id: int, actor_id: int, contract_number: Optional[str] = None, attach: bool = False) -> Outlet:
        """Resumable: each step is skipped when its result already exists (spec D2).

        `attach=True` is the operator's explicit "this shop belongs to the account its phone
        already names" (D25 rule 3): the account is JOINED instead of created. Never implicit --
        the default keeps every existing caller meaning "create a new account, or refuse". Any
        LINK to an existing user (the attach, or the silent link of a customer with no outlet)
        takes its address through `_adopt_or_create_address` (R42); only an account created here
        gets its address straight from the pin.
        """
        from business_app.utils.service_factory import get_corporate_contract_service

        outlet = OutletService.get(outlet_id)
        if outlet.stage == "active":
            return outlet
        if outlet.stage not in ("activation_requested", "prospect", "trial"):
            raise ValidationError("Outlet is not awaiting activation", error_code="SALES_OUTLET_STAGE_INVALID")
        contact = outlet.primary_contact
        phone = contact.phone if contact else None

        step = "customer"
        created = False
        try:
            if outlet.user_id is None:
                if not phone:
                    raise ValidationError(
                        "A contact phone is required to activate", error_code="SALES_ACTIVATION_PHONE_REQUIRED"
                    )
                # D25 rule 3: a phone whose customer ALREADY HOLDS A LIVE OUTLET (R45) is a chain, not a
                # mistake -- and that is exactly the case this step already refused. Joining it
                # is a DECISION, so the refusal now carries the account the caller would be
                # joining, and every surface draws its Attach from that one answer instead of
                # looking the phone up again. The narrowness is the point (R24): a phone that
                # belongs to a customer with NO outlet keeps being linked silently, below.
                candidate = OutletService.account_candidate(outlet)
                if candidate is not None and not attach:
                    raise ConflictError(
                        "Phone already belongs to a customer account",
                        details={"account": candidate},
                        error_code="SALES_APPROVAL_PHONE_TAKEN",
                    )
                # An orphan users row can already exist from a failed individual activation, so a
                # matching customer is LINKED rather than refused; a type mismatch is
                # SALES_APPROVAL_PHONE_TAKEN, raised by `_assert_linkable` itself.
                existing = User.query.filter_by(phone=phone).first()
                if attach and existing is None:
                    # `attach` is a claim about an account that EXISTS. The refusal is about the
                    # missing account, not about the missing candidate: an attach on a phone held
                    # by a 0-outlet customer still links it (there is an account to join), and an
                    # attach on a phone held by a customer of another TYPE falls through to
                    # `_assert_linkable`'s SALES_APPROVAL_PHONE_TAKEN, which is the truthful code.
                    raise ValidationError(
                        "No existing customer account matches this outlet's contact phone",
                        error_code="SALES_ATTACH_NO_ACCOUNT",
                    )
                if existing is not None:
                    OutletService._assert_linkable(outlet.outlet_type, existing)
                    user = existing
                else:
                    user = OutletService._create_customer(outlet, contact.name, phone, actor_id)
                    created = True
                outlet.user_id = user.id
                db.session.commit()
            user = User.query.get(outlet.user_id)

            step = "address"
            if outlet.address_id is None:
                if outlet.latitude is None:
                    raise ValidationError("A pin is required to activate", error_code="SALES_ACTIVATION_PIN_REQUIRED")
                if created:
                    # An account minted a moment ago has no address to adopt: its first row is
                    # written from the pin.
                    outlet.address_id = OutletService._create_address(outlet, user).id
                else:
                    # R42: every LINK to an existing user -- the attach, the silent link of a
                    # customer with no outlet, or a resumed run whose customer step already
                    # committed -- takes the account's FREE address at the pin when there is one,
                    # else its own row: the same "which address is this shop" answer the Link
                    # path gives (R39), so a link never mints a second row for a known place.
                    OutletService._adopt_or_create_address(outlet, user)
                db.session.commit()

            step = "contract"
            if outlet.outlet_type == "grocery_store":
                contracts = get_corporate_contract_service()
                if contracts.get_active_amount_contract_for_user(user.id) is None:
                    number = contract_number or f"SA-{outlet.id}-{datetime.now(UTC):%Y%m%d}"
                    contracts.create_contract(
                        {
                            "user_id": user.id,
                            "contract_number": number,
                            "name": f"{outlet.name} (sales agent onboarding)",
                            "notes": "Created at sales-agent onboarding",
                        },
                        actor_user_id=actor_id,
                    )
                    db.session.commit()
        except (ValidationError, ConflictError, NotFoundError) as exc:
            db.session.rollback()
            if getattr(exc, "error_code", None) in (
                "SALES_APPROVAL_PHONE_TAKEN",
                "SALES_ATTACH_NO_ACCOUNT",
                "SALES_ACTIVATION_PHONE_REQUIRED",
                "SALES_ACTIVATION_PIN_REQUIRED",
            ):
                raise
            raise ValidationError(
                f"Approval failed at step '{step}': {exc.message}",
                details={"step": step},
                error_code="SALES_APPROVAL_STEP_FAILED",
            ) from exc

        outlet = OutletService.get(outlet_id)
        outlet.approved_at = datetime.now(UTC)
        outlet.approved_by_user_id = actor_id
        outlet.rejected_reason = None
        OutletService.transition(outlet, "active", actor_id, reason_code="attached" if attach else "approved")
        db.session.commit()
        notifications.notify_agent_event(outlet, SALES_EVENT_OUTLET_APPROVED)
        StaffService._log_activity(
            user_id=actor_id,
            action=STAFF_ACTIONS["OUTLET_APPROVED"],
            entity_type="outlet",
            entity_id=outlet.id,
            metadata_={"user_id": outlet.user_id},
        )
        return outlet

    @staticmethod
    def reject(outlet_id: int, actor_id: int, reason: str) -> Outlet:
        outlet = OutletService.get(outlet_id)
        if outlet.stage != "activation_requested":
            raise ValidationError("Outlet is not awaiting activation", error_code="SALES_OUTLET_STAGE_INVALID")
        outlet.rejected_reason = (reason or "").strip() or None
        OutletService.transition(outlet, "prospect", actor_id, reason_code="rejected", note=outlet.rejected_reason)
        db.session.commit()
        notifications.notify_agent_event(outlet, SALES_EVENT_OUTLET_REJECTED)
        StaffService._log_activity(
            user_id=actor_id,
            action=STAFF_ACTIONS["OUTLET_REJECTED"],
            entity_type="outlet",
            entity_id=outlet.id,
            metadata_={"reason": outlet.rejected_reason},
        )
        return outlet

    # ------------------------------------------------------------------ assignment
    @staticmethod
    def _assert_agent(agent_user_id: int) -> None:
        if SalesAgentProfile.query.filter_by(user_id=agent_user_id).first() is None:
            raise ValidationError("User is not a sales agent", error_code="SALES_AGENT_PROFILE_REQUIRED")

    @staticmethod
    def assign(outlet_id: int, agent_user_id: Optional[int], actor_id: int) -> Outlet:
        outlet = OutletService.get(outlet_id)
        if agent_user_id is not None:
            OutletService._assert_agent(agent_user_id)
        outlet.assigned_agent_user_id = agent_user_id
        db.session.commit()
        return outlet

    @staticmethod
    def bulk_assign_by_district(district: str, agent_user_id: int, actor_id: int) -> int:
        if district not in TASHKENT_DISTRICTS:
            raise ValidationError(f"Unknown district {district}", error_code="SALES_DISTRICT_INVALID")
        OutletService._assert_agent(agent_user_id)
        count = Outlet.query.filter(Outlet.district == district, Outlet.stage != "lost").update(
            {Outlet.assigned_agent_user_id: agent_user_id}, synchronize_session=False
        )
        db.session.commit()
        return int(count)

    @staticmethod
    def import_existing_customers(actor_id: int) -> int:
        """One outlet per ADDRESS of every active grocery/workplace customer (class C, active).

        An outlet is one PLACE; a customer ACCOUNT may own several (D25). A chain is one account
        with several delivery addresses, so this walks addresses, not accounts. An address that
        already carries an outlet is skipped -- which is also what makes a RE-RUN back-fill the
        branches of the chains imported under the old one-outlet-per-account rule, instead of
        finding the account taken and doing nothing. An account with no address at all still
        gets its single address-less outlet, guarded by "this account has no outlet yet".

        Naming is `_import_outlet_name`'s (R22, R41): a chain's outlets are "<account>, <branch>",
        a one-address account keeps the bare account name the earlier one-outlet-per-account
        import wrote. Names are never rewritten on a re-run.

        Known and stated, not fixed: an account whose ONLY outlet is the address-less one this
        method wrote before it had any address gets a SECOND outlet the day an address appears.
        The address-less row then reads the account's whole history (`order_scope` refuses to
        narrow an outlet with no address) while the new row reads its address — two rows for one
        shop until the office marks one lost, which is R4's stated answer for a junk branch.
        Adopting the address-less outlet onto the first free address instead would be the fix;
        it is not ruled, and doing it silently here would also move that outlet's pin and text.

        Returns the number of OUTLETS created, which is the figure the admin toolbar reports.
        """
        users = User.query.filter(
            User.user_type == UserType.ENTITY,
            User.entity_subtype.in_([EntitySubtype.GROCERY_STORE, EntitySubtype.WORKPLACE]),
            User.status == UserStatus.ACTIVE,
        ).all()
        created = 0
        for user in users:
            account_name = user.company_name or user.full_name or user.phone
            # Same order as `_default_address`, so the head shop is imported first and the
            # branch rows land in a stable, explainable sequence.
            addresses = (
                UserAddress.query.filter_by(user_id=user.id)
                .order_by(UserAddress.is_default.desc(), UserAddress.id.asc())
                .all()
            )
            if not addresses:
                if Outlet.query.filter_by(user_id=user.id).first() is None:
                    OutletService._import_outlet(
                        user, None, account_name, actor_id, branch_mode=False, account_titles=[]
                    )
                    created += 1
                continue
            # R22: only a CHAIN's outlets carry the ", <branch>" half. Decided once per account,
            # from the addresses it has right now -- a one-shop customer keeps the bare name the
            # earlier one-outlet-per-account import gave it, which is also the name already
            # standing on prod. R41 reads every title of the account to tell a unique one apart.
            branch_mode = len(addresses) >= 2
            account_titles = [address.title for address in addresses]
            for address in addresses:
                if Outlet.query.filter_by(address_id=address.id).first() is not None:
                    continue
                OutletService._import_outlet(
                    user, address, account_name, actor_id, branch_mode=branch_mode, account_titles=account_titles
                )
                created += 1
        db.session.commit()
        return created

    @staticmethod
    def _import_outlet(
        user: User,
        address: Optional[UserAddress],
        account_name: str,
        actor_id: int,
        *,
        branch_mode: bool,
        account_titles: List[Optional[str]],
    ) -> Outlet:
        """One imported outlet: active, class C, pinned at its address."""
        latitude = address.latitude if address else None
        longitude = address.longitude if address else None
        # An `addresses` row written before the delivery-zone backstop existed can still hold an
        # out-of-zone pin, and register_delivery_zone_listeners(Outlet) refuses it on INSERT.
        # Drop the PIN only, and keep going: raising here cost every remaining store its outlet,
        # while the address link, its text and its district are all perfectly usable without it.
        if latitude is not None and longitude is not None and not is_in_delivery_zone(latitude, longitude):
            current_app.logger.info(
                "Importing outlet for user %s without a pin: address %s is outside the delivery zone",
                user.id,
                address.id,
            )
            latitude = longitude = None
        outlet = Outlet(
            name=_import_outlet_name(account_name, address, branch_mode=branch_mode, account_titles=account_titles),
            outlet_type=user.entity_subtype.value,
            stage="active",
            outlet_class="C",
            user_id=user.id,
            address_id=address.id if address else None,
            latitude=latitude,
            longitude=longitude,
            address_text=address.full_address if address else None,
            district=_canonical_district(address.district) if address else None,
            preferred_language=user.preferred_language or "uz",
            approved_at=datetime.now(UTC),
            approved_by_user_id=actor_id,
        )
        db.session.add(outlet)
        db.session.flush()
        if user.phone:
            # Every branch carries the ACCOUNT's contact: one chain, one number to call. The same
            # phone on N outlets is why dedupe has to read siblings as siblings, not duplicates.
            db.session.add(
                OutletContact(
                    outlet_id=outlet.id,
                    name=(user.full_name or user.company_name or "Owner")[:100],
                    phone=user.phone,
                    role="owner",
                    is_primary=True,
                )
            )
        OutletService._record_stage(outlet, None, "active", actor_id, reason_code="imported")
        return outlet

    # ------------------------------------------------------------- nightly jobs
    @staticmethod
    def _stage_entered_at(outlet: Outlet) -> datetime:
        """When this outlet entered the stage it is in now.

        The reactivation anchor, read per OUTLET rather than from a global job-state row.
        Every stage write goes through `_record_stage`, so the newest `outlet_stage_history`
        row IS the moment the job (or a human) put the outlet here — which makes "a delivery
        since the last run" survive a run that was skipped, retried or delayed, where a
        24-hour window would silently drop it. `created_at` is that table's only timestamp;
        an outlet with no history at all falls back to its own.
        """
        row = (
            OutletStageHistory.query.filter_by(outlet_id=outlet.id)
            .order_by(OutletStageHistory.created_at.desc(), OutletStageHistory.id.desc())
            .first()
        )
        return ensure_utc(row.created_at if row is not None else outlet.created_at)

    @staticmethod
    def _typical_interval_days(outlet: Outlet, instants: List[datetime]) -> float:
        """How often this shop normally buys, in days.

        The median of its last `INTERVAL_SAMPLE_SIZE` inter-order gaps, floored at
        `MIN_INTERVAL_DAYS`. Fewer than two deliveries is not a rhythm, so the class cadence
        stands in (R4) — `cadence_days` is the one place that answers "how often should we
        be at this shop", so the fallback is not a second opinion about it.
        """
        from business_app.services.sales.replenishment_service import ReplenishmentService

        if len(instants) < 2:
            return float(ReplenishmentService.cadence_days(outlet))
        gaps = [
            (instants[index] - instants[index + 1]).total_seconds() / _SECONDS_PER_DAY
            for index in range(len(instants) - 1)
        ]
        return max(float(median(gaps)), float(MIN_INTERVAL_DAYS))

    @staticmethod
    def _activate_converted_tryouts() -> int:
        """A try-out that converted turns its outlet into a serving one (D8, R5).

        The user link is written whenever the outlet has none. It used to be written only
        when no OTHER outlet already held that customer, because `uq_outlets_user_id` made a
        second one impossible; since e0f1a2b3c4d5 an account may hold several outlets (D25),
        and that guard's only remaining effect was to leave a chain's converted trial shop
        account-less for ever. The address is what makes it a branch, so whenever the outlet has
        none — whether or not the link was written this run — it takes the one
        `_adopt_or_create_address` picks for its account (R39); an activated shop with no
        address is refused by every money route (`VisitService._assert_orderable`).
        No commit: `update_stages` owns the transaction.
        """
        rows = (
            db.session.query(Outlet, ProductTryout.converted_user_id)
            .join(ProductTryout, ProductTryout.outlet_id == Outlet.id)
            .filter(Outlet.stage == "trial", ProductTryout.converted_user_id.isnot(None))
            .order_by(Outlet.id.asc(), ProductTryout.id.asc())
            .all()
        )
        activated = 0
        seen = set()
        for outlet, converted_user_id in rows:
            if outlet.id in seen:
                continue
            seen.add(outlet.id)
            if outlet.user_id is None:
                outlet.user_id = converted_user_id
            if outlet.address_id is None:
                customer = User.query.get(outlet.user_id)
                if customer is not None:
                    OutletService._adopt_or_create_address(outlet, customer)
            OutletService.transition(outlet, "active", None, reason_code="job_tryout_converted")
            activated += 1
        return activated

    @staticmethod
    def update_stages(now: Optional[datetime] = None) -> Dict[str, int]:
        """The 01:10 job (spec, *Stages, due dates and alerts*).

        `active -> at_risk` when the shop has been quieter than `SALES_AT_RISK_RATIO` times
        its own typical interval; `at_risk -> dormant` once it has been at_risk for longer
        than `SALES_DORMANT_DAYS`; `at_risk|dormant -> active` on a delivery since it went
        quiet; and a converted try-out activates its trial outlet. Every row is written with
        `actor_user_id=None` and a `reason_code` naming the job, so the outlet's history
        reads truthfully next to the human transitions.

        The DORMANT clock runs from the at-risk transition, not from the last delivered
        order. An absolute "days since delivery" clock collides with the at-risk threshold
        for the commonest outlet there is: class is NULL on every outlet phase 1 imported,
        NULL means cadence C (30 d), and `SALES_AT_RISK_RATIO` 1.5 x 30 is exactly
        `SALES_DORMANT_DAYS` 45 — so such a shop would be at_risk for a single night and
        dormant by the next run, i.e. the at-risk alert the agent is supposed to act on would
        never be visible. Anchored on the transition, the window is the same length whatever
        the class, and `SALES_DORMANT_DAYS` reads as what it now is: how long a shop may stay
        at risk before we stop planning visits to it.

        ONE move per outlet per run: an `active` shop that trips the at-risk threshold goes
        `at_risk` tonight and cannot also go dormant in the same run. Two rows for one night
        would claim the shop was briefly at risk when nobody ever saw it that way.

        "How recently did water reach this shop" is asked once, of
        `ReplenishmentService.delivered_instants` — the module that already owns every
        DELIVERED-history question — so the stage rule and the consumption rate can never
        disagree about when an order landed.
        """
        from business_app.services.sales.replenishment_service import ReplenishmentService

        moment = ensure_utc(now) if now is not None else datetime.now(UTC)
        ratio = float(current_app.config["SALES_AT_RISK_RATIO"])
        dormant_days = float(current_app.config["SALES_DORMANT_DAYS"])
        counts = {"scanned": 0, "at_risk": 0, "dormant": 0, "reactivated": 0, "activated_from_trial": 0}

        outlets = (
            Outlet.query.filter(Outlet.stage.in_(SERVING_STAGES), Outlet.user_id.isnot(None))
            .order_by(Outlet.id.asc())
            .all()
        )
        for outlet in outlets:
            counts["scanned"] += 1
            instants = ReplenishmentService.delivered_instants(outlet, limit=INTERVAL_SAMPLE_SIZE + 1)
            if not instants:
                # Nothing has ever been delivered here, so "days since the last delivery" has
                # no value — not a large one. Leave the stage alone.
                continue
            elapsed_days = (moment - instants[0]).total_seconds() / _SECONDS_PER_DAY
            threshold_days = ratio * OutletService._typical_interval_days(outlet, instants)
            stage_entered_at = OutletService._stage_entered_at(outlet)
            at_risk_days = (moment - stage_entered_at).total_seconds() / _SECONDS_PER_DAY
            if outlet.stage in ("at_risk", "dormant") and instants[0] > stage_entered_at:
                OutletService.transition(outlet, "active", None, reason_code="job_reactivated")
                counts["reactivated"] += 1
            elif outlet.stage == "active" and elapsed_days > threshold_days:
                OutletService.transition(outlet, "at_risk", None, reason_code="job_at_risk")
                counts["at_risk"] += 1
            elif outlet.stage == "at_risk" and at_risk_days > dormant_days:
                # Measured from the at-risk transition, so the window cannot collapse to one
                # night for a class-NULL outlet (see the docstring above).
                OutletService.transition(outlet, "dormant", None, reason_code="job_dormant")
                counts["dormant"] += 1

        counts["activated_from_trial"] = OutletService._activate_converted_tryouts()
        db.session.commit()
        return counts

    @staticmethod
    def mark_lost(outlet_id: int, actor_id: int, reason: str, note: Optional[str]) -> Outlet:
        outlet = OutletService.get(outlet_id)
        if reason not in LOST_REASONS:
            raise ValidationError("Unknown lost reason", error_code="SALES_LOST_REASON_INVALID")
        outlet.lost_reason = reason
        outlet.lost_note = (note or "").strip() or None
        OutletService.transition(outlet, "lost", actor_id, reason_code=reason, note=outlet.lost_note)
        db.session.commit()
        return outlet

    # ------------------------------------------------------------------ try-out from the field
    @staticmethod
    def create_tryout_from_field(outlet: Outlet, payload: Dict[str, Any], agent_user_id: int) -> "ProductTryout":
        """Leave a try-out at the counter (D8, spec *Try-out from the field*).

        `TryoutService.create_tryout` is the ONE try-out creation path -- the admin page and the
        driver bot already go through it -- so this method only TRANSLATES an outlet into the
        payload that path already accepts. `complete_handoff=False` with no driver leaves the
        hand-off task OPEN in the driver pool, which is what keeps D1 true: the agent books, a
        driver carries the bottles and the bottle ledger is written when that driver completes
        the hand-off, never here.

        Two transactions, not one: `create_tryout` commits inside itself (as every other try-out
        caller already lives with), so the outlet link and the stage move are written straight
        after it returns. The order is deliberate -- the try-out is the durable artefact, and an
        outlet left at `prospect` because the second commit never happened is repaired by
        repeating the tap, while a stage moved with no try-out behind it is a lie about a shop.
        (That retry is NOT idempotent: it links the outlet but creates a second try-out and a
        second OPEN hand-off task; the first try-out survives unlinked for phase 3's feed.)
        """
        from business_app.services.tryout_service import TryoutService

        contact = outlet.primary_contact
        if contact is None or not contact.phone:
            raise ValidationError(
                "The outlet needs a contact phone before a try-out",
                details={"outlet_id": outlet.id},
                error_code="SALES_TRYOUT_PHONE_REQUIRED",
            )

        items = [
            {"product_id": int(item["product_id"]), "quantity": int(item["quantity"])}
            for item in (payload.get("items") or [])
        ]
        if not items:
            raise ValidationError("Pick at least one product", error_code="SALES_TRYOUT_ITEMS_INVALID")
        # The eligibility rule itself is `TryoutService._validate_and_build_items` and is NOT
        # restated here -- a second copy is how a product the admin page refuses ends up on a
        # shelf. It is called AHEAD of `create_tryout` only to re-raise its answer under the code
        # the bot branches on: raised from inside `create_tryout`, the same refusal arrives as an
        # uncoded 400 (inactive/ineligible) or a 404 (unknown id), which the bot can only render
        # as "Something went wrong" on a screen that could still fix the basket. Reaching across
        # a service for a leading-underscore helper follows this file's own
        # `StaffService._log_activity` precedent: the alternative here is not a cleaner call, it
        # is a duplicated rule.
        try:
            TryoutService._validate_and_build_items(items)
        except (NotFoundError, ValidationError) as exc:
            raise ValidationError(exc.message, error_code="SALES_TRYOUT_ITEMS_INVALID") from exc

        tryout = TryoutService.create_tryout(
            {
                "trial_contact": {
                    "first_name": contact.name,
                    "phone": contact.phone,
                    # The SHOP, so the try-out list and the driver's task card name the place a
                    # driver is going to and not just whoever answers the phone there.
                    "company_name": outlet.name,
                    "preferred_language": outlet.preferred_language,
                },
                "address": {
                    "label": outlet.name[:100],
                    # `outlets.address_text` is nullable and `trial_contact_addresses.full_address`
                    # is NOT NULL, so the shop name is the fallback. A pinless prospect falls
                    # through to `_populate_missing_coordinates`, the same geocode the admin
                    # try-out form already relies on -- no new refusal is invented here for a
                    # case the existing path handles.
                    "full_address": outlet.address_text or outlet.name,
                    "district": outlet.district,
                    "latitude": outlet.latitude,
                    "longitude": outlet.longitude,
                },
                "items": items,
                "notes": payload.get("notes"),
                # D1 again: no driver, no completed hand-off. The task stays in the open pool.
                "complete_handoff": False,
            },
            agent_user_id,
            source="sales_agent",
        )

        tryout.outlet_id = outlet.id
        # R9: the sample is what makes a PROSPECT a trial. An outlet that is already `trial`,
        # `active`, `at_risk`, `dormant` or `lost` keeps the stage it earned -- and since `trial`
        # is a DUE stage, a spurious move would also rewrite `next_visit_due_at` off a rule the
        # agent's own visit date already settled.
        if outlet.stage in ("prospect", "activation_requested"):
            OutletService.transition(outlet, "trial", agent_user_id, reason_code="tryout")
        db.session.commit()
        StaffService._log_activity(
            user_id=agent_user_id,
            action=STAFF_ACTIONS["AGENT_TRYOUT_CREATED"],
            entity_type="tryout",
            entity_id=tryout.id,
            metadata_={"outlet_id": outlet.id, "stage": outlet.stage, "item_count": len(items)},
        )
        return tryout

    # ------------------------------------------------------------------ admin list + card
    @staticmethod
    def unvisited_filter(days: int, *, now: datetime):
        """SQL predicate: "nobody has been to this ACTIVE outlet in `days` days".

        One expression for two consumers — the admin Outlets `unvisited_days` filter and the
        agent's morning digest (D7: "unvisited-for-N-days alerts go to agents ... and
        admins"). Two copies would let a manager and the agent standing outside the shop read
        different answers off the same estate an hour apart.

        A never-visited outlet counts: a NULL `last_visit_at` is not "visited today". The
        stage is part of the question rather than a caller's extra clause — an at_risk or
        dormant outlet is the stage job's alarm, and a prospect has no cadence to be late on.
        """
        cutoff = now - timedelta(days=int(days))
        return and_(
            or_(Outlet.last_visit_at.is_(None), Outlet.last_visit_at < cutoff),
            Outlet.stage == "active",
        )

    @staticmethod
    def _admin_query(filters: Dict[str, Any]):
        """The admin filter block, shared by `list_admin` and `list_pins`.

        The map and the table must answer the same question: a pin the list refuses to show, or a
        row the map hides, is a filter bug the operator cannot see. Keeping ONE builder is what
        makes that true -- a second copy of these clauses would drift the moment a filter is added.
        """
        query = Outlet.query
        if filters.get("search"):
            clause = OutletService._name_filter(filters["search"])
            phone_digits = "".join(ch for ch in filters["search"] if ch.isdigit())
            phone_clause = (
                Outlet.id.in_(
                    db.session.query(OutletContact.outlet_id).filter(OutletContact.phone.ilike(f"%{phone_digits}%"))
                )
                if len(phone_digits) >= 4
                else None
            )
            clauses = [c for c in (clause, phone_clause) if c is not None]
            if clauses:
                query = query.filter(or_(*clauses))
        for key, column in (
            ("stage", Outlet.stage),
            ("outlet_type", Outlet.outlet_type),
            ("district", Outlet.district),
            ("agent_user_id", Outlet.assigned_agent_user_id),
        ):
            if filters.get(key) not in (None, ""):
                query = query.filter(column == filters[key])
        if filters.get("outlet_class"):
            query = query.filter(Outlet.outlet_class == filters["outlet_class"])
        if filters.get("unvisited_days"):
            query = query.filter(OutletService.unvisited_filter(filters["unvisited_days"], now=datetime.now(UTC)))
        return query

    @staticmethod
    def list_admin(filters: Dict[str, Any], page: int, per_page: int) -> Tuple[List[Outlet], int, Dict[str, int]]:
        query = OutletService._admin_query(filters)
        pagination = query.order_by(Outlet.updated_at.desc()).paginate(
            page=page, per_page=min(per_page, 100), error_out=False
        )
        summary = {stage: 0 for stage in OUTLET_STAGES}
        for stage, count in db.session.query(Outlet.stage, db.func.count(Outlet.id)).group_by(Outlet.stage).all():
            summary[stage] = int(count)
        return list(pagination.items), pagination.total, summary

    @staticmethod
    def list_pins(filters: Dict[str, Any]) -> List[Outlet]:
        """Every matching outlet that has a pin -- deliberately UNPAGINATED.

        The map is not a page of results: a truncated pin set is indistinguishable from an estate
        that simply has no outlets there, so paginating it would silently lie about coverage. The
        filters are the only thing that narrows it.
        """
        query = OutletService._admin_query(filters).filter(Outlet.latitude.isnot(None), Outlet.longitude.isnot(None))
        return list(query.order_by(Outlet.id).all())

    @staticmethod
    def card(
        outlet: Outlet,
        *,
        agent_user_id: Optional[int] = None,
        open_visit_ids: Optional[Dict[int, int]] = None,
    ) -> Dict[str, Any]:
        """The full outlet card. `agent_user_id` is the VIEWER, not the assignee: only a sales
        agent can have an open visit, so the admin drawer (`admin_sales.get_outlet_admin`) calls
        this without one and gets `open_visit_id: None`.

        `open_visit_ids` is `VisitService.open_visit_ids_by_outlet`'s mapping when the caller
        already holds it; without one the card asks for it below. Either way the answer comes
        from that one function."""
        from business_app.models.order import Order
        from business_app.serializers.sales_serializers import _iso, serialize_order_brief, serialize_outlet
        from business_app.services.bottle_tracking_service import BottleTrackingService
        from business_app.utils.payment_projection import net_open_receivable_amount

        data = serialize_outlet(outlet)
        open_receivable: Optional[Decimal] = None
        last_orders: List[Dict[str, Any]] = []
        if outlet.user_id:
            open_receivable = Decimal("0.00")
            orders = Order.query.filter(Order.user_id == outlet.user_id).order_by(Order.created_at.desc()).all()
            for order in orders:
                # net_open_receivable_amount is only the "still owes money" HALF of the rule; the
                # SSOT (business_app/utils/payment_projection.py) requires every caller to add its
                # own DELIVERED conjunct. A COD payment is born PENDING with amount_collected=0, so
                # without it a confirmed-but-undelivered — or cancelled — order reads as debt the
                # store does not owe. `last_orders` below is deliberately unfiltered BY STATUS;
                # it is scoped to the BRANCH instead (see below).
                status_value = order.status.value if hasattr(order.status, "value") else order.status
                if status_value != OrderStatus.DELIVERED.value:
                    continue
                payment = getattr(order, "payment", None)
                if payment is not None:
                    open_receivable += net_open_receivable_amount(payment)
            # `serialize_order_brief` is the ONE shaper of "an order as a sales surface shows it"
            # (it also builds the visit's `order` block and the SALES_VISIT_ORDER_EXISTS details),
            # and `_iso` normalises the instant the same way every other timestamp on this card is
            # normalised -- a raw `.isoformat()` publishes a naive string under SQLite and an
            # offset-bearing one under PostgreSQL, i.e. one field with two wire formats.
            # The money above is the ACCOUNT's and stays that way (D25 rule 6): one wallet
            # behind every branch, labelled as such by the renderers. The HISTORY is this
            # BRANCH's, through the same `order_scope` the four replenishment queries read —
            # a second expression of "whose orders are these" is how a card and a rate end up
            # disagreeing about the same shop. On a single-outlet account the clause is
            # `Order.user_id` alone, so this is the query that was here before.
            branch_orders = (
                Order.query.filter(OutletService.order_scope(outlet)).order_by(Order.created_at.desc()).limit(3).all()
            )
            last_orders = [{**serialize_order_brief(o), "created_at": _iso(o.created_at)} for o in branch_orders]
        # NULL, not zero, when the figure does not apply: an outlet with no customer account has
        # no wallet, and one with no address row has no bottle ledger. A 0.0 there reads as a
        # settled bill and an empty crate on a store that has never ordered. Decided once, HERE, so
        # the staff-bot card and the admin drawer both gate on the field the backend published
        # instead of each re-deriving "does this outlet even have one" from a different column.
        data["open_receivable"] = float(open_receivable) if open_receivable is not None else None
        data["bottle_balance"] = (
            float(BottleTrackingService.get_place_balance(outlet.address_id)) if outlet.address_id else None
        )
        data["last_orders"] = last_orders
        # Account-level vs branch-level, decided HERE and only LABELLED by the renderers
        # (D25 rules 6-7): the receivable above is the ACCOUNT's -- one wallet for the whole
        # chain -- while the bottle balance is this branch's own place ledger. `is_branch` is the
        # DECISION, published so the staff card and the admin drawer read the same answer instead
        # of each re-deriving it from a count (two expressions of one rule, with two different
        # null defaults, is the bug CLAUDE.md's "full scope" note is about); `branch_count` is the
        # NUMBER that answer prints; `open_receivable_scope` says what the money figure MEANS,
        # which is why it is a constant rather than a nullable. None, not 0, for the two account
        # figures when there is no account yet -- the same rule the two figures above follow --
        # while `is_branch` is a plain False, because a prospect is not "unknown", it is not a
        # branch. List rows carry none of this: one query per CARD is not one query per row.
        if outlet.user_id is None:
            data["account_name"] = data["branch_count"] = None
        else:
            data["account_name"] = _user_display_name(outlet.user)
            data["branch_count"] = OutletService.account_outlet_count(outlet.user_id)
        data["is_branch"] = OutletService.is_branch(outlet)
        data["open_receivable_scope"] = "account"
        data["account_candidate"] = OutletService.account_candidate(outlet)

        # --- what the visit loop needs (phase 2a) -------------------------------------------
        # Every field below is a RULE, computed once HERE and published (D15). The staff-bot card
        # prints `rate_per_day` / `suggested_now` / `overdue_days` verbatim and gates its
        # Start-visit vs Resume-visit button on `open_visit_id`; it derives none of them.
        from business_app.models.sales_visits import Visit
        from business_app.serializers.sales_serializers import overdue_days
        from business_app.services.sales.replenishment_service import ReplenishmentService
        from business_app.services.sales.visit_service import VisitService

        now = datetime.now(UTC)
        last_visit = (
            Visit.query.filter(Visit.outlet_id == outlet.id, Visit.status == "completed")
            .order_by(Visit.started_at.desc(), Visit.id.desc())
            .first()
        )
        data["last_visit"] = (
            {
                "id": last_visit.id,
                "outcome": last_visit.outcome,
                "notes": last_visit.notes,
                "ended_at": _iso(last_visit.ended_at),
            }
            if last_visit is not None
            else None
        )

        rate: Optional[Decimal] = None
        suggested: Optional[int] = None
        product = ReplenishmentService.primary_returnable_product()
        if product is not None:
            rate, _rate_source = ReplenishmentService.rate_per_day(outlet, product, now=now)
            check = ReplenishmentService.latest_stock_check(outlet, product)
            suggested = ReplenishmentService.suggested_qty(
                product,
                rate,
                check.on_hand_qty if check is not None else 0,
                ReplenishmentService.cadence_days(outlet),
                last_qty=ReplenishmentService.last_delivered_qty(outlet, product),
            )
        data["rate_per_day"] = float(rate) if rate is not None else None
        data["suggested_now"] = suggested
        # Ruling 18: the SAME function the due-list row uses, so the card's "3 days overdue" line
        # and the list button's "+3d" label can never be two different subtractions.
        data["overdue_days"] = overdue_days(outlet.next_visit_due_at, now)
        # `is not None`, not truthiness: a viewer id is an identity, and "0 is falsy" is how
        # an identity test quietly becomes a value test.
        if open_visit_ids is None:
            open_visit_ids = VisitService.open_visit_ids_by_outlet(agent_user_id) if agent_user_id is not None else {}
        data["open_visit_id"] = open_visit_ids.get(outlet.id)
        return data
