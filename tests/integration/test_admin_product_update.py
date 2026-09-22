"""Admin product update endpoint: stock-quantity contract and SKU round-trip.

Drives PUT /api/v1/admin/products/<id> the way the admin UI's edit modal does
(admin_ui/src/pages/Products.js -> adminService.updateProduct), because the
whole defect class here lives in the gap between what the UI sends and what the
handler decides to honour.

The rule under test: for a product that requires marking codes, stock_quantity
is DERIVED from the available marking-code pool
(ProductFiscalService.sync_stock_from_marking_codes) and must not be settable by
hand. That rule already had two expressions -- an explicit 400 in
update_product_stock and a silent drop-plus-200 in update_product -- so both are
pinned here, in one file.
"""

from business_app import db
from business_app.models.product import Product, ProductFiscalProfile


def _make_marking_code_product(app, product_id, **profile_kwargs):
    """Attach a requires_marking_codes fiscal profile to an existing product."""
    with app.app_context():
        profile = ProductFiscalProfile(
            product_id=product_id,
            fiscalization_enabled=True,
            requires_marking_codes=True,
            spic="SPIC-STOCK-TEST",
            **profile_kwargs,
        )
        db.session.add(profile)
        db.session.commit()


def _read_product(app, product_id):
    """Re-read the row's state as plain values.

    Returned inside the app context on purpose: requires_marking_codes lazy-loads
    fiscal_profile, which raises DetachedInstanceError once the session is gone.
    """
    with app.app_context():
        product = Product.query.get(product_id)
        return {
            "name": product.name,
            "sku": product.sku,
            "stock_quantity": product.stock_quantity,
            "requires_marking_codes": product.requires_marking_codes,
            "in_sales_stock_check": product.in_sales_stock_check,
        }


# ---------------------------------------------------------------------------
# PUT /admin/products/<id> -- the endpoint the edit modal actually calls
# ---------------------------------------------------------------------------


def test_stock_update_persists_for_plain_product(app, admin_auth_headers, sample_product):
    """A product without marking codes accepts a hand-typed stock quantity."""
    product_id = sample_product.id

    resp = app.test_client().put(
        f"/api/v1/admin/products/{product_id}",
        json={"stock_quantity": 42},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["product"]["stock_quantity"] == 42
    assert _read_product(app, product_id)["stock_quantity"] == 42


def test_stock_update_rejected_for_marking_code_product(app, admin_auth_headers, sample_product):
    """Changing stock on a marking-code product must fail loudly, not silently."""
    product_id = sample_product.id
    original_stock = sample_product.stock_quantity
    _make_marking_code_product(app, product_id)

    resp = app.test_client().put(
        f"/api/v1/admin/products/{product_id}",
        json={"stock_quantity": 42},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 400, resp.get_json()
    assert "marking code" in " ".join(resp.get_json()["errors"]).lower()
    assert _read_product(app, product_id)["stock_quantity"] == original_stock


def test_marking_code_product_still_accepts_other_edits(app, admin_auth_headers, sample_product):
    """Echoing back the unchanged derived stock must not block editing other fields.

    The edit modal always submits stock_quantity, so rejecting on mere presence
    of the key would make marking-code products entirely uneditable.
    """
    product_id = sample_product.id
    original_stock = sample_product.stock_quantity
    _make_marking_code_product(app, product_id)

    resp = app.test_client().put(
        f"/api/v1/admin/products/{product_id}",
        json={"stock_quantity": original_stock, "name": "Renamed While Marked"},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    product = _read_product(app, product_id)
    assert product["name"] == "Renamed While Marked"
    assert product["stock_quantity"] == original_stock


def test_disabling_marking_codes_and_setting_stock_in_one_request(app, admin_auth_headers, sample_product):
    """Turning marking codes off frees the stock field in the same submit.

    The guard must read the requires_marking_codes value the request is
    establishing, not the one it is replacing -- otherwise the admin is told
    stock is derived by the very request that stops deriving it.
    """
    product_id = sample_product.id
    _make_marking_code_product(app, product_id)

    resp = app.test_client().put(
        f"/api/v1/admin/products/{product_id}",
        json={"stock_quantity": 7, "requires_marking_codes": False},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    product = _read_product(app, product_id)
    assert product["requires_marking_codes"] is False
    assert product["stock_quantity"] == 7


def test_sku_round_trips_through_update(app, admin_auth_headers, sample_product):
    """The edit modal reads sku out of this payload, so it must be served and stored."""
    product_id = sample_product.id

    resp = app.test_client().put(
        f"/api/v1/admin/products/{product_id}",
        json={"sku": "AQUA-19"},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["product"]["sku"] == "AQUA-19"

    listing = app.test_client().get("/api/v1/admin/products", headers=admin_auth_headers)
    assert listing.status_code == 200, listing.get_json()
    row = next(item for item in listing.get_json()["data"]["items"] if item["id"] == product_id)
    assert row["sku"] == "AQUA-19"


# ---------------------------------------------------------------------------
# PUT /admin/products/<id>/stock -- the same rule's other expression
# ---------------------------------------------------------------------------


def test_stock_endpoint_persists_for_plain_product(app, admin_auth_headers, sample_product):
    product_id = sample_product.id

    resp = app.test_client().put(
        f"/api/v1/admin/products/{product_id}/stock",
        json={"stock_quantity": 55, "reason": "stocktake"},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    assert _read_product(app, product_id)["stock_quantity"] == 55


def test_stock_endpoint_rejects_marking_code_product(app, admin_auth_headers, sample_product):
    product_id = sample_product.id
    original_stock = sample_product.stock_quantity
    _make_marking_code_product(app, product_id)

    resp = app.test_client().put(
        f"/api/v1/admin/products/{product_id}/stock",
        json={"stock_quantity": 55, "reason": "stocktake"},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 400, resp.get_json()
    assert "marking code" in " ".join(resp.get_json()["errors"]).lower()
    assert _read_product(app, product_id)["stock_quantity"] == original_stock


# ---------------------------------------------------------------------------
# GET /admin/products -- the listing must publish the pool, not the stale column
# ---------------------------------------------------------------------------


def test_admin_listing_publishes_the_derived_count_for_marking_code_products(
    app, admin_auth_headers, sample_product
):
    """The column can be stale; the pool cannot. Publish the pool."""
    from business_app.models.product import ProductMarkingCode
    from shared.enums import MarkingCodeStatus

    product_id = sample_product.id
    _make_marking_code_product(app, product_id)
    for index in range(2):
        db.session.add(
            ProductMarkingCode(
                product_id=product_id,
                code=f"ADMIN-PROJ-{index}",
                status=MarkingCodeStatus.AVAILABLE,
            )
        )
    sample_product.stock_quantity = 486  # stale, like prod
    db.session.commit()

    resp = app.test_client().get("/api/v1/admin/products", headers=admin_auth_headers)

    assert resp.status_code == 200, resp.get_json()
    row = next(item for item in resp.get_json()["data"]["items"] if item["id"] == product_id)
    assert row["stock_quantity"] == 2


# ---------------------------------------------------------------------------
# The round trip: what the listing PUBLISHES is what the modal PUTs back
# ---------------------------------------------------------------------------
#
# The listing now publishes the derived pool count while the column keeps its
# stale value, and antd submits disabled fields -- so a plain price edit carries
# the POOL count in stock_quantity. A guard that compares the payload against the
# stored column therefore 400s every edit of a marking-code product. These pin the
# GET -> PUT round trip against a deliberately STALE column, which is the only
# arrangement in which the published value and the column can disagree.


def _seed_stale_marking_code_product(app, product, available_codes, stale_column, prefix):
    """Marking-code product whose column (`stale_column`) != pool (`available_codes`).

    Mutates `product` on the AMBIENT session (not through a nested app context)
    so the request that follows sees the new column value.
    """
    from business_app.models.product import ProductMarkingCode
    from shared.enums import MarkingCodeStatus

    _make_marking_code_product(app, product.id)
    for index in range(available_codes):
        db.session.add(
            ProductMarkingCode(
                product_id=product.id,
                code=f"{prefix}-{index}",
                status=MarkingCodeStatus.AVAILABLE,
            )
        )
    product.stock_quantity = stale_column
    db.session.commit()


def test_admin_row_round_trips_back_through_update_unchanged(app, admin_auth_headers, sample_product):
    """GET the row, PUT it straight back: the modal's own no-op save must succeed."""
    product_id = sample_product.id
    _seed_stale_marking_code_product(app, sample_product, available_codes=2, stale_column=486, prefix="RT")

    client = app.test_client()
    listing = client.get("/api/v1/admin/products", headers=admin_auth_headers)
    assert listing.status_code == 200, listing.get_json()
    row = next(item for item in listing.get_json()["data"]["items"] if item["id"] == product_id)
    assert row["stock_quantity"] == 2, "precondition: the listing publishes the pool, not the column"

    resp = client.put(f"/api/v1/admin/products/{product_id}", json=row, headers=admin_auth_headers)

    assert resp.status_code == 200, resp.get_json()
    # (c) the pool owns the column; this endpoint must not have written it.
    assert _read_product(app, product_id)["stock_quantity"] == 486


def test_price_edit_echoing_the_published_stock_succeeds_and_writes_no_stock(
    app, admin_auth_headers, sample_product
):
    """The real prod payload: only the price changed, stock_quantity is the echoed pool count."""
    product_id = sample_product.id
    _seed_stale_marking_code_product(app, sample_product, available_codes=2, stale_column=486, prefix="ECHO")

    resp = app.test_client().put(
        f"/api/v1/admin/products/{product_id}",
        json={"stock_quantity": 2, "price": 17000},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    assert _read_product(app, product_id)["stock_quantity"] == 486


def test_hand_typed_stock_change_still_refused_when_the_column_is_stale(
    app, admin_auth_headers, sample_product
):
    """(b) A genuine stock CHANGE stays a loud 400, never a silent drop."""
    product_id = sample_product.id
    _seed_stale_marking_code_product(app, sample_product, available_codes=2, stale_column=486, prefix="CHG")

    resp = app.test_client().put(
        f"/api/v1/admin/products/{product_id}",
        json={"stock_quantity": 999, "price": 17000},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 400, resp.get_json()
    assert "marking code" in " ".join(resp.get_json()["errors"]).lower()
    assert _read_product(app, product_id)["stock_quantity"] == 486


# ---------------------------------------------------------------------------
# in_sales_stock_check -- the column the sales agent's visit stock list IS
# ---------------------------------------------------------------------------
#
# ReplenishmentService.stock_check_products() filters on this column, so an
# admin turning the switch on is the ONLY way a product reaches the agent's
# stock screen. Task 1 taught every read surface to publish the flag; a write
# path that publishes a field and then drops it on save is precisely the bug
# class this file was opened for, so both real payloads are driven here: the
# create modal's full object and the edit modal's echo of the listing row.


def test_create_product_persists_the_sales_stock_check_flag(app, admin_auth_headers, sample_category):
    """The create modal's payload, switch ON, has to reach the column."""
    resp = app.test_client().post(
        "/api/v1/admin/products",
        json={
            "name": "Agent Route 19L",
            "description": "Counted by the sales agent on every visit",
            "category_id": sample_category.id,
            "sku": "AGENT-19",
            "price": 16000,
            "stock_quantity": 40,
            "min_order_quantity": 1,
            "volume": 19.0,
            "status": "active",
            "is_featured": False,
            "is_tryout_eligible": False,
            "tracks_returnable_bottles": True,
            "returnable_bottles_per_unit": 1,
            "in_sales_stock_check": True,
            "images": [],
        },
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    created = resp.get_json()["data"]["product"]
    # Distinct values per switch on purpose: a flag that merely echoed a
    # neighbouring boolean would satisfy a single-key assertion.
    assert created["in_sales_stock_check"] is True
    assert created["is_tryout_eligible"] is False
    assert created["tracks_returnable_bottles"] is True
    assert _read_product(app, created["id"])["in_sales_stock_check"] is True


def test_create_product_defaults_the_sales_stock_check_flag_off(app, admin_auth_headers, sample_category):
    """A payload that never mentions the flag leaves the product off the agent's list."""
    resp = app.test_client().post(
        "/api/v1/admin/products",
        json={
            "name": "Shelf Only 5L",
            "category_id": sample_category.id,
            "price": 6000,
        },
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    created = resp.get_json()["data"]["product"]
    assert created["in_sales_stock_check"] is False
    assert _read_product(app, created["id"])["in_sales_stock_check"] is False


def test_update_publishes_the_sales_stock_check_flag_on_every_read_surface(
    app, admin_auth_headers, sample_product
):
    """PUT the flag on; the admin listing AND the public catalogue must both say so.

    Three surfaces, one fact: the write path here, the admin row the modal
    re-reads, and the public inventory block. They are asserted together so a
    future change cannot move one without the others.
    """
    product_id = sample_product.id
    client = app.test_client()

    resp = client.put(
        f"/api/v1/admin/products/{product_id}",
        json={"in_sales_stock_check": True},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["product"]["in_sales_stock_check"] is True
    assert _read_product(app, product_id)["in_sales_stock_check"] is True

    listing = client.get("/api/v1/admin/products", headers=admin_auth_headers)
    assert listing.status_code == 200, listing.get_json()
    admin_row = next(item for item in listing.get_json()["data"]["items"] if item["id"] == product_id)
    assert admin_row["in_sales_stock_check"] is True

    public = client.get("/api/v1/products/")
    assert public.status_code == 200, public.get_json()
    public_row = next(item for item in public.get_json()["data"]["items"] if item["id"] == product_id)
    assert public_row["inventory"]["in_sales_stock_check"] is True


def test_the_admin_row_publishes_agent_stock_list_membership_not_just_the_column(
    app, admin_auth_headers, sample_product
):
    """M13: the admin listing answers the question the agent's screen is built from.

    `in_sales_stock_check` is a COLUMN; membership of the agent's stock list is
    `ReplenishmentService.stock_check_products()` — the column AND `is_active`. An admin
    UI that re-derives the pair in JSX is a second expression of a backend rule, and the
    one that drifts the day the query grows a third clause. The row publishes the answer;
    the page renders it.

    The predicate is IMPORTED, never restated here: this test would otherwise be a third
    copy of the rule, green against a production that had moved on.
    """
    from business_app.models.product import Product
    from business_app.services.sales.replenishment_service import ReplenishmentService

    product_id = sample_product.id
    client = app.test_client()
    assert client.put(
        f"/api/v1/admin/products/{product_id}",
        json={"in_sales_stock_check": True, "is_active": False},
        headers=admin_auth_headers,
    ).status_code == 200

    listing = client.get("/api/v1/admin/products", headers=admin_auth_headers)
    assert listing.status_code == 200, listing.get_json()
    row = next(item for item in listing.get_json()["data"]["items"] if item["id"] == product_id)

    # Flagged, so the switch still shows it on ...
    assert row["in_sales_stock_check"] is True
    # ... and NOT on the list the agent is served, which is what the tag means.
    assert row["in_agent_stock_list"] is False
    with app.app_context():
        product = Product.query.get(product_id)
        assert row["in_agent_stock_list"] is ReplenishmentService.in_stock_check_list(product)
        assert product not in ReplenishmentService.stock_check_products()


def test_update_can_turn_the_sales_stock_check_flag_back_off(app, admin_auth_headers, sample_product):
    """False is a value, not a missing key: the switch has to work both ways.

    Seeded on the AMBIENT session (not a nested app context) so the request
    that follows sees the new column value -- the _seed_stale_marking_code_product
    arrangement above, for the same reason.
    """
    product_id = sample_product.id
    sample_product.in_sales_stock_check = True
    db.session.commit()

    resp = app.test_client().put(
        f"/api/v1/admin/products/{product_id}",
        json={"in_sales_stock_check": False, "price": 15500},
        headers=admin_auth_headers,
    )

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["product"]["in_sales_stock_check"] is False
    assert _read_product(app, product_id)["in_sales_stock_check"] is False


def test_admin_row_round_trip_keeps_the_sales_stock_check_flag_on(app, admin_auth_headers, sample_product):
    """GET the row, PUT it straight back: the modal's no-op save must not clear the flag.

    The listing row now carries in_sales_stock_check and antd submits every
    field the form holds, so the write path has to ACCEPT the key it publishes.
    """
    product_id = sample_product.id
    sample_product.in_sales_stock_check = True
    db.session.commit()

    client = app.test_client()
    listing = client.get("/api/v1/admin/products", headers=admin_auth_headers)
    assert listing.status_code == 200, listing.get_json()
    row = next(item for item in listing.get_json()["data"]["items"] if item["id"] == product_id)
    assert row["in_sales_stock_check"] is True, "precondition: the listing publishes the flag"

    resp = client.put(f"/api/v1/admin/products/{product_id}", json=row, headers=admin_auth_headers)

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["data"]["product"]["in_sales_stock_check"] is True
    assert _read_product(app, product_id)["in_sales_stock_check"] is True


def test_product_serializers_export_no_admin_request_schema():
    """`CreateProductRequest` / `UpdateProductRequest` documented a write contract
    no handler ever validated: `create_product` reads `data.get(...)` and
    `update_product` uses a presence test (`if "in_sales_stock_check" in data`,
    business_app/api/admin.py:3154). The two disagreed with that handler as well --
    `Optional[bool] = None` cannot express "absent", so a payload built from the
    model and dumped would carry an explicit null, which the handler's presence
    test turns into `bool(None)` = flag OFF. The live contract is the four HTTP
    tests above; a second, divergent paper copy must not come back."""
    import business_app.serializers.product_serializers as product_serializers

    assert not hasattr(product_serializers, "CreateProductRequest")
    assert not hasattr(product_serializers, "UpdateProductRequest")
    assert "CreateProductRequest" not in product_serializers.__all__
    assert "UpdateProductRequest" not in product_serializers.__all__
