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
    LOST_REASONS,
    OUTLET_CLASSES,
    OUTLET_STAGES,
    OUTLET_TYPES,
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
ENTITY_SUBTYPE_BY_OUTLET_TYPE = {
    "grocery_store": EntitySubtype.GROCERY_STORE,
    "workplace": EntitySubtype.WORKPLACE,
}


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
            if Outlet.query.filter_by(user_id=link_user.id).first() is not None:
                raise ConflictError("Customer already has an outlet", error_code="SALES_OUTLET_USER_LINKED")
            OutletService._assert_linkable(fields["outlet_type"], link_user)
        else:
            candidates = OutletService.find_duplicates(
                fields["name"], contact_phone, fields["latitude"], fields["longitude"]
            )
            if candidates and not force:
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
    def _link_customer(outlet: Outlet, user: User, actor_id: Optional[int]) -> None:
        outlet.user_id = user.id
        address = OutletService._default_address(user)
        if address is not None and Outlet.query.filter_by(address_id=address.id).first() is None:
            outlet.address_id = address.id
            if outlet.latitude is None and address.latitude is not None:
                outlet.latitude, outlet.longitude = address.latitude, address.longitude
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
            if payload["payment_terms"] not in ("cash", "business_account"):
                raise ValidationError(
                    "payment_terms must be cash or business_account", error_code="SALES_PAYMENT_TERMS_INVALID"
                )
            outlet.payment_terms = payload["payment_terms"]
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
    def list_activation_requests() -> List[Outlet]:
        return (
            Outlet.query.filter(Outlet.stage == "activation_requested")
            .order_by(Outlet.activation_requested_at.asc())
            .all()
        )

    @staticmethod
    def approve(outlet_id: int, actor_id: int, contract_number: Optional[str] = None) -> Outlet:
        """Resumable: each step is skipped when its result already exists (spec D2)."""
        from business_app.utils.service_factory import get_corporate_contract_service

        outlet = OutletService.get(outlet_id)
        if outlet.stage == "active":
            return outlet
        if outlet.stage not in ("activation_requested", "prospect", "trial"):
            raise ValidationError("Outlet is not awaiting activation", error_code="SALES_OUTLET_STAGE_INVALID")
        contact = outlet.primary_contact
        phone = contact.phone if contact else None

        step = "customer"
        try:
            if outlet.user_id is None:
                if not phone:
                    raise ValidationError(
                        "A contact phone is required to activate", error_code="SALES_ACTIVATION_PHONE_REQUIRED"
                    )
                # An orphan users row can already exist from a failed individual activation, so a
                # matching customer is LINKED rather than refused; only a type mismatch or a
                # customer that already belongs to another outlet is SALES_APPROVAL_PHONE_TAKEN.
                existing = User.query.filter_by(phone=phone).first()
                if existing is not None:
                    if Outlet.query.filter(Outlet.user_id == existing.id, Outlet.id != outlet.id).first() is not None:
                        raise ConflictError(
                            "Phone already belongs to another outlet's customer",
                            error_code="SALES_APPROVAL_PHONE_TAKEN",
                        )
                    OutletService._assert_linkable(outlet.outlet_type, existing)
                    user = existing
                else:
                    user = OutletService._create_customer(outlet, contact.name, phone, actor_id)
                outlet.user_id = user.id
                db.session.commit()
            user = User.query.get(outlet.user_id)

            step = "address"
            if outlet.address_id is None:
                if outlet.latitude is None:
                    raise ValidationError("A pin is required to activate", error_code="SALES_ACTIVATION_PIN_REQUIRED")
                outlet.address_id = OutletService._create_address(outlet, user).id
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
        OutletService.transition(outlet, "active", actor_id, reason_code="approved")
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
        """One outlet per active grocery/workplace customer that has none yet (class C, active)."""
        users = User.query.filter(
            User.user_type == UserType.ENTITY,
            User.entity_subtype.in_([EntitySubtype.GROCERY_STORE, EntitySubtype.WORKPLACE]),
            User.status == UserStatus.ACTIVE,
        ).all()
        created = 0
        for user in users:
            if Outlet.query.filter_by(user_id=user.id).first() is not None:
                continue
            address = OutletService._default_address(user)
            if address is not None and Outlet.query.filter_by(address_id=address.id).first() is not None:
                address = None
            latitude = address.latitude if address else None
            longitude = address.longitude if address else None
            # A user_addresses row written before the delivery-zone backstop existed can still hold
            # an out-of-zone pin, and register_delivery_zone_listeners(Outlet) refuses it on INSERT.
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
                name=(user.company_name or user.full_name or user.phone)[:200],
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
            created += 1
        db.session.commit()
        return created

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

        The user link is written only when the outlet has none AND no other outlet already
        holds that customer — `uq_outlets_user_id` is a unique, and a conversion that cannot
        be linked must still activate the shop rather than abort the whole nightly run.
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
                taken = Outlet.query.filter(Outlet.user_id == converted_user_id, Outlet.id != outlet.id).first()
                if taken is None:
                    outlet.user_id = converted_user_id
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
                # store does not owe. `last_orders` below is deliberately unfiltered.
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
            last_orders = [{**serialize_order_brief(o), "created_at": _iso(o.created_at)} for o in orders[:3]]
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
