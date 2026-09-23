"""find_duplicates: by phone, and by similar name within SALES_DEDUPE_RADIUS_M."""
from business_app.models.sales import Outlet, OutletContact
from business_app.models.user import User, UserAddress
from business_app.services.sales.outlet_service import OutletService, names_similar
from business_app.utils.password_security import hash_password
from shared.enums import EntitySubtype, UserRole, UserType

PIN = (41.3111, 69.2797)          # inside TASHKENT_POLYGON
NEAR = (41.3115, 69.2801)         # ~55 m away
FAR = (41.3300, 69.3200)          # ~4 km away


def _customer(db, phone, company=None, subtype=None):
    user = User(phone=phone, password_hash=hash_password("Pw123456!"), first_name="Owner", last_name="One",
                user_type=UserType.ENTITY if company else UserType.INDIVIDUAL, role=UserRole.CUSTOMER,
                company_name=company, entity_subtype=subtype)
    db.session.add(user)
    db.session.commit()
    return user


def test_names_similar_is_transliteration_aware():
    assert names_similar("Bahor market", "BAHOR MARKET (do'kon)")
    assert names_similar("Бахор маркет", "Bahor market")
    assert not names_similar("Bahor", "Navruz market")


def test_phone_match_returns_customer_and_outlet_contacts(db):
    user = _customer(db, "+998901112233", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    outlet = Outlet(name="Navruz", outlet_type="grocery_store")
    outlet.contacts.append(OutletContact(name="X", phone="+998901112244", is_primary=True))
    db.session.add(outlet)
    db.session.commit()

    by_user = OutletService.find_duplicates("Anything", "90 111 22 33", None, None)
    assert [(c["kind"], c["user_id"], c["reason"]) for c in by_user] == [("customer", user.id, "phone")]

    by_contact = OutletService.find_duplicates("Anything", "+998901112244", None, None)
    assert [(c["kind"], c["outlet_id"], c["reason"]) for c in by_contact] == [("outlet", outlet.id, "phone")]


def test_a_widened_radius_actually_widens_east_west(db, app, monkeypatch):
    """The SQL pre-filter is derived from the radius, so raising the radius reaches further.

    A degree of longitude is only ~836 m at this latitude: with a hardcoded 0.01-degree box this
    outlet 1.5 km due EAST is invisible no matter how high SALES_DEDUPE_RADIUS_M is set.
    """
    east_1500m = (41.3111, 69.29764)  # 1502 m due east of PIN, still inside TASHKENT_POLYGON
    outlet = Outlet(name="Bahor market", outlet_type="grocery_store",
                    latitude=east_1500m[0], longitude=east_1500m[1])
    db.session.add(outlet)
    db.session.commit()

    monkeypatch.setitem(app.config, "SALES_DEDUPE_RADIUS_M", 2000)
    found = OutletService.find_duplicates("Bahor market", None, PIN[0], PIN[1])

    assert [(c["outlet_id"], c["reason"]) for c in found] == [(outlet.id, "name_nearby")]
    assert 1400 < found[0]["distance_m"] < 1600


def test_name_match_is_symmetric(db):
    """Dedupe must not depend on which shop the agent entered first."""
    shorter = Outlet(name="Bahor", outlet_type="grocery_store", latitude=NEAR[0], longitude=NEAR[1])
    db.session.add(shorter)
    db.session.commit()

    found = OutletService.find_duplicates("Bahor market", None, PIN[0], PIN[1])

    assert [(c["outlet_id"], c["reason"]) for c in found] == [(shorter.id, "name_nearby")]


def test_name_match_within_radius_only(db):
    near = Outlet(name="Bahor market", outlet_type="grocery_store", latitude=NEAR[0], longitude=NEAR[1])
    far = Outlet(name="Bahor market", outlet_type="grocery_store", latitude=FAR[0], longitude=FAR[1])
    db.session.add_all([near, far])
    user = _customer(db, "+998901112255", company="Bahor do'koni", subtype=EntitySubtype.GROCERY_STORE)
    db.session.add(UserAddress(user_id=user.id, full_address="Chilonzor 5", latitude=NEAR[0], longitude=NEAR[1]))
    db.session.commit()

    found = OutletService.find_duplicates("bahor", None, PIN[0], PIN[1])

    kinds = sorted((c["kind"], c["reason"]) for c in found)
    assert kinds == [("customer", "name_nearby"), ("outlet", "name_nearby")]
    assert all(c["distance_m"] < 150 for c in found)
    assert far.id not in [c.get("outlet_id") for c in found]


def test_branches_of_the_account_the_phone_names_come_back_as_siblings(db):
    """D25 rule 4: a chain's branches carry the chain's number, so every sibling flagged the
    others as a duplicate. They are places of one account — labelled `sibling`, informational.
    The account itself stays a `customer` candidate: that row is the agent's 🔗 Link door.
    An outlet on somebody ELSE's account (`rival`) or on none (`stranger`) is still a plain
    duplicate: the relabel reads "an account THIS lookup named", never "an account at all" --
    otherwise another customer's shop stops blocking `create()` and is drawn as a branch."""
    account = _customer(db, "+998901112244", company="Bahor", subtype=EntitySubtype.GROCERY_STORE)
    branch = Outlet(name="Bahor market, Chilonzor", outlet_type="grocery_store", user_id=account.id)
    branch.contacts.append(OutletContact(name="Olim aka", phone="+998901112244", is_primary=True))
    stranger = Outlet(name="Navruz", outlet_type="grocery_store")
    stranger.contacts.append(OutletContact(name="Boshqa", phone="+998901112244", is_primary=True))
    other = _customer(db, "+998901112299", company="Navruz", subtype=EntitySubtype.GROCERY_STORE)
    rival = Outlet(name="Navruz 2", outlet_type="grocery_store", user_id=other.id)
    rival.contacts.append(OutletContact(name="Boshqa", phone="+998901112244", is_primary=True))
    db.session.add_all([branch, stranger, rival])
    db.session.commit()

    found = OutletService.find_duplicates("Bahor market, Yunusobod", "+998901112244", None, None)

    # The full rows, not a set of kinds: `rival` and `stranger` share a kind, so a set would
    # read the same whether the rival stayed a duplicate or turned into a sibling.
    rows = [(c["kind"], c["reason"], c["outlet_id"], c["user_id"]) for c in found]
    expected = [
        ("customer", "phone", None, account.id),
        ("outlet", "phone", stranger.id, None),
        ("outlet", "phone", rival.id, other.id),
        ("sibling", "same_account_branch", branch.id, account.id),
    ]
    assert sorted(rows, key=str) == sorted(expected, key=str)
