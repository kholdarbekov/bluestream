"""The automatic IndexNow trigger: one detector, one drain (spec 5.9).

A save that changes what a public product page says asks for one submission once it has committed. The detector, a
session-level `before_flush` listener, reads the attribute history of what each flush writes and flags the transaction
when it inserts or deletes a product, changes one of `PUBLIC_PRODUCT_COLUMNS`, or writes a translation of a public
product field. The drain, an `after_commit` listener, then calls `request_submission()` once for that transaction, and
its debounce queues one submission however many commits a burst holds. Nothing is requested for a rollback, before the
outer commit of a SAVEPOINT, for a stock-only save, nor at all with the switch off.

Each test drives the admin route the admin UI calls, with the body it sends. The scene is built with the switch off, as
the tests' config has it, and the switch goes on just before the write under test, so what is recorded is what that
write requested. Faked, as external I/O only: the publish (the one seam, `_publish_submission`, bound to a recorder)
and Redis (`indexnow_redis`), because the suite's lanes share the real Redis DBs.

The detector turns its own crash into a WARNING and flags nothing, so a "publishes nothing" test would also pass on a
broken detector. Every test here therefore fails on a WARNING from the dispatcher's logger (`no_dispatch_warning`),
except the one that breaks the detector on purpose.
"""

import logging
from decimal import Decimal

import pytest

from business_app.models.translation import Translation
from business_app.utils import indexnow_dispatch
from business_app.utils.indexnow_dispatch import PUBLIC_PRODUCT_COLUMNS
from shared.redis_keyspace import RedisKeyspace
from tests.integration.dispatch_builders import calls_to, sql_inside
from tests.integration.indexnow_builders import (  # noqa: F401 -- fixtures
    indexnow_redis_fixture as indexnow_redis,
    submission_requests_fixture as submission_requests,
)
from tests.integration.public_page_builders import offer_skus  # noqa: F401 -- fixture
from tests.integration.reorder_reminder_builders import captured_logs

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("indexnow_redis")]

ADMIN = "/api/v1/admin"
MARKER = RedisKeyspace.indexnow_submit_scheduled()
ONE_SUBMISSION = [60]  # INDEXNOW_COUNTDOWN_SECONDS
# The tests that make the dispatcher warn on purpose.
WARNS_ON_PURPOSE = frozenset({"test_a_failing_detector_never_fails_the_save"})
# A value different from `offer_skus.p19`'s for each watched column (the fixture's own value in the comment).
NEW_VALUE = {
    "name": "Aqua Element 18.9 L, renamed",  # "Aqua Element 18.9 L"
    "description": "Artesian water",  # None
    "short_description": "Artesian",  # None
    "meta_title": "Aqua Element 18.9 L water",  # None
    "meta_description": "Artesian drinking water in an 18.9 L bottle",  # None
    "base_price": Decimal("17345"),  # 23450.00
    "discount_price": Decimal("15000"),  # None
    "is_active": False,  # True
    "slug": "test-19",  # None
    "min_order_quantity": 3,  # 2
    "size": "10L",  # "19L"
    "volume": 19.0,  # 18.9
    "tracks_returnable_bottles": False,  # True
    "returnable_bottles_per_unit": Decimal("2.00"),  # 1.00
    "images": ["/x.png"],  # [] (the column default)
}


@pytest.fixture(autouse=True)
def no_dispatch_warning(request, caplog):
    """Fails the test on any WARNING (or worse) from `indexnow_dispatch.logger`: "nothing was published" must mean the
    detector looked and found nothing, never that it crashed. A test in WARNS_ON_PURPOSE opts out."""
    if request.node.originalname in WARNS_ON_PURPOSE:
        yield
        return
    logger = indexnow_dispatch.logger
    with captured_logs(caplog, logger, logging.WARNING):
        yield
    # caplog keeps one list of records per phase, and at teardown `caplog.records` is the teardown's: read the call's.
    warned = [r for r in caplog.get_records("call") if r.name == logger.name and r.levelno >= logging.WARNING]
    assert warned == [], [record.getMessage() for record in warned]


def _ok(response, status=200):
    assert response.status_code == status, response.get_data(as_text=True)
    return response.get_json()


def _switch_on(app, monkeypatch) -> None:
    """INDEXNOW_ENABLED on for the rest of the test (the tests' config has it off): the scene built before this
    requested nothing, so what is recorded from here is what the write under test requested."""
    monkeypatch.setitem(app.config, "INDEXNOW_ENABLED", True)


def _named(db, product) -> None:
    """The product's name in ru and en, as entity translations, committed."""
    product.set_translations({"name": {"ru": "Вода 19 л", "en": "Water 19 L"}})
    db.session.commit()


def _again(indexnow_redis, submission_requests) -> None:
    """A clean slate for a second write in one test: no queued marker, nothing recorded."""
    indexnow_redis.delete(MARKER)
    submission_requests.clear()


def _edit_modal_payload(client, headers, product_id, **changes):
    """What the Products edit modal PUTs: the listing row echoed back (admin_ui/src/pages/Products.js:387-442, 505-530)."""
    rows = client.get("/api/v1/admin/products", headers=headers).get_json()["data"]["items"]
    row = next(item for item in rows if item["id"] == product_id)
    payload = {
        key: row[key]
        for key in (
            "name",
            "description",
            "category_id",
            "sku",
            "price",
            "stock_quantity",
            "volume",
            "status",
            "is_featured",
            "barcode",
            "spic",
            "package_code",
            "units",
            "vat_percent",
        )
    }
    payload.update(
        min_order_quantity=row["min_order_quantity"] if row["min_order_quantity"] is not None else 1,
        is_tryout_eligible=row["is_tryout_eligible"] is not False,
        in_sales_stock_check=bool(row["in_sales_stock_check"]),
        tracks_returnable_bottles=bool(row["tracks_returnable_bottles"]),
        returnable_bottles_per_unit=row["returnable_bottles_per_unit"] or 0,
        fiscalization_enabled=bool(row["fiscalization_enabled"]),
        requires_marking_codes=bool(row["requires_marking_codes"]),
        images=row["images"] if row["image_url"] else [],
    )
    form = {
        f"{field}_{lang}": row[f"{field}_translations"].get(lang) or ""
        for field in ("name", "description")
        for lang in ("ru", "en")
    }
    form.update({key: changes.pop(key) for key in list(changes) if key in form})
    translations = {
        field: {lang: form[f"{field}_{lang}"] for lang in ("ru", "en") if form[f"{field}_{lang}"]}
        for field in ("name", "description")
    }
    if translations["name"] or translations["description"]:
        payload["translations"] = translations
    return {**payload, **changes}


def _edit(client, headers, product_id, **changes):
    """Save the Products edit modal of `product_id` with `changes` typed into it."""
    return client.put(
        f"{ADMIN}/products/{product_id}",
        json=_edit_modal_payload(client, headers, product_id, **changes),
        headers=headers,
    )


# ---------------------------------------------------------------------------------------------------------------------
# The admin's writes, through the routes the admin UI calls.
# ---------------------------------------------------------------------------------------------------------------------


def test_a_price_change_publishes_one_submission(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers
):
    client = app.test_client()
    p19 = offer_skus.p19
    _named(db, p19)
    _switch_on(app, monkeypatch)

    _ok(_edit(client, admin_claim_headers, p19.id, price=17345))

    assert submission_requests == ONE_SUBMISSION


def test_a_stock_only_save_of_the_edit_modal_publishes_nothing(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers
):
    """The modal echoes every field and the product's translations back; only the stock changed, and `updated_at`
    is rewritten on every save. Neither is a public fact."""
    client = app.test_client()
    p19 = offer_skus.p19
    _named(db, p19)
    _switch_on(app, monkeypatch)

    _ok(_edit(client, admin_claim_headers, p19.id, stock_quantity=40))

    assert p19.stock_quantity == 40
    assert submission_requests == []


def test_entering_descriptions_in_the_edit_modal_publishes_once(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers
):
    """Owner checklist item 8: the description in uz and its ru and en translations, typed into the modal."""
    client = app.test_client()
    p19 = offer_skus.p19
    _switch_on(app, monkeypatch)

    _ok(
        _edit(
            client,
            admin_claim_headers,
            p19.id,
            description="Artezian suv",
            description_ru="Артезианская вода",
            description_en="Artesian water",
        )
    )

    assert submission_requests == ONE_SUBMISSION


def test_the_stock_endpoint_publishes_nothing(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers
):
    client = app.test_client()
    p19 = offer_skus.p19
    _switch_on(app, monkeypatch)

    _ok(client.put(f"{ADMIN}/products/{p19.id}/stock", json={"stock_quantity": 40}, headers=admin_claim_headers))

    assert submission_requests == []


def test_creating_a_product_with_translations_publishes_once(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers
):
    """The route commits twice (the product, then its translations); the debounce makes it one submission."""
    client = app.test_client()
    body = {
        "name": "Aqua test 19L",
        "price": 17345,
        "category_id": offer_skus.p19.category_id,
        "stock_quantity": 10,
        "min_order_quantity": 1,
        "volume": 19.0,
        "status": "active",
        "images": [],
        "translations": {"name": {"ru": "Вода тест 19 л"}, "description": {}},
    }
    _switch_on(app, monkeypatch)

    _ok(client.post(f"{ADMIN}/products", json=body, headers=admin_claim_headers))

    assert submission_requests == ONE_SUBMISSION


def test_deleting_a_product_publishes_once(app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers):
    client = app.test_client()
    p19 = offer_skus.p19
    _switch_on(app, monkeypatch)

    _ok(client.delete(f"{ADMIN}/products/{p19.id}", headers=admin_claim_headers))

    assert submission_requests == ONE_SUBMISSION


def test_bulk_deactivate_of_two_products_publishes_once(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers
):
    """One commit per product; the debounce makes it one submission."""
    client = app.test_client()
    body = {
        "action": "deactivate",
        "target_type": "product",
        "target_ids": [offer_skus.p19.id, offer_skus.p10.id],
        "parameters": {},
    }
    _switch_on(app, monkeypatch)

    data = _ok(client.post(f"{ADMIN}/bulk-actions", json=body, headers=admin_claim_headers))["data"]

    assert data["results"]["success_count"] == 2
    assert submission_requests == ONE_SUBMISSION


def test_bulk_stock_update_publishes_nothing(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers
):
    client = app.test_client()
    body = {
        "action": "update_stock",
        "target_type": "product",
        "target_ids": [offer_skus.p19.id],
        "parameters": {"new_stock": 5},
    }
    _switch_on(app, monkeypatch)

    data = _ok(client.post(f"{ADMIN}/bulk-actions", json=body, headers=admin_claim_headers))["data"]

    assert data["results"]["success_count"] == 1
    assert submission_requests == []


def test_a_product_translation_publishes_once(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers
):
    client = app.test_client()
    body = {
        "entity_type": "Product",
        "entity_id": offer_skus.p19.id,
        "field_name": "name",
        "language": "ru",
        "content": "Вода 19 л",
    }
    _switch_on(app, monkeypatch)

    _ok(client.post(f"{ADMIN}/translations", json=body, headers=admin_claim_headers), status=201)

    assert submission_requests == ONE_SUBMISSION


def test_editing_a_product_translation_publishes_once_and_an_unchanged_save_publishes_nothing(
    app, db, monkeypatch, offer_skus, indexnow_redis, submission_requests, admin_claim_headers
):
    """The Translations edit modal's two fields (admin_ui/src/pages/Translations.js:456-461). The route rewrites
    `updated_at` and `updated_by` on every save; a save that changes neither field publishes nothing."""
    client = app.test_client()
    p19 = offer_skus.p19
    _named(db, p19)
    row = Translation.query.filter_by(key=f"Product.name.{p19.id}", language="ru").one()
    body = {"value": "Новая вода 19 л", "is_active": True}
    _switch_on(app, monkeypatch)

    _ok(client.put(f"{ADMIN}/translations/{row.id}", json=body, headers=admin_claim_headers))
    assert submission_requests == ONE_SUBMISSION

    _again(indexnow_redis, submission_requests)
    _ok(client.put(f"{ADMIN}/translations/{row.id}", json=body, headers=admin_claim_headers))
    assert submission_requests == []


def test_deleting_a_product_translation_publishes_once(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers
):
    client = app.test_client()
    p19 = offer_skus.p19
    _named(db, p19)
    row = Translation.query.filter_by(key=f"Product.name.{p19.id}", language="ru").one()
    _switch_on(app, monkeypatch)

    _ok(client.delete(f"{ADMIN}/translations/{row.id}", headers=admin_claim_headers))

    assert submission_requests == ONE_SUBMISSION


def test_a_product_translation_typed_in_the_create_modal_publishes_once(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers
):
    """The Translations create modal posts the key-and-value branch."""
    client = app.test_client()
    body = {
        "category": "entity_product",
        "language": "en",
        "key": f"Product.description.{offer_skus.p19.id}",
        "value": "Artesian water",
        "is_active": True,
    }
    _switch_on(app, monkeypatch)

    _ok(client.post(f"{ADMIN}/translations", json=body, headers=admin_claim_headers), status=201)

    assert submission_requests == ONE_SUBMISSION


@pytest.mark.parametrize(
    "key",
    ["Product.ingredients.{id}", "ProductCategory.name.{id}", "Product.name.draft"],
    ids=["not-a-public-field", "not-a-product", "not-an-id"],
)
def test_a_translation_of_no_public_product_field_publishes_nothing(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers, key
):
    """The key filter: a product field the pages do not print, another entity's field, and a key whose last part is no
    id. Typed in the Translations create modal, as the publishing case above."""
    client = app.test_client()
    body = {
        "category": "entity_product",
        "language": "en",
        "key": key.format(id=offer_skus.p19.id),
        "value": "Artesian water",
        "is_active": True,
    }
    _switch_on(app, monkeypatch)

    _ok(client.post(f"{ADMIN}/translations", json=body, headers=admin_claim_headers), status=201)

    assert submission_requests == []


def test_a_landing_copy_edit_publishes_nothing(app, db, monkeypatch, submission_requests, admin_claim_headers):
    client = app.test_client()
    row = Translation(key="landing.offer.every_day", language="ru", value="x", category="landing", is_active=True)
    db.session.add(row)
    db.session.commit()
    _switch_on(app, monkeypatch)

    _ok(
        client.put(
            f"{ADMIN}/translations/{row.id}", json={"value": "y", "is_active": True}, headers=admin_claim_headers
        )
    )

    assert submission_requests == []


def test_a_refused_edit_publishes_nothing(app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers):
    client = app.test_client()
    p19 = offer_skus.p19
    _switch_on(app, monkeypatch)

    _ok(_edit(client, admin_claim_headers, p19.id, price=17345, min_order_quantity=0), status=400)
    db.session.rollback()

    assert submission_requests == []


# ---------------------------------------------------------------------------------------------------------------------
# The session and the safety rules.
# ---------------------------------------------------------------------------------------------------------------------


def test_a_rolled_back_change_publishes_nothing_even_at_the_next_commit(
    app, db, monkeypatch, offer_skus, submission_requests
):
    p19 = offer_skus.p19
    _switch_on(app, monkeypatch)

    p19.base_price = Decimal("17345")
    db.session.flush()  # the detector has seen it
    db.session.rollback()
    p19.stock_quantity = 5
    db.session.commit()

    assert submission_requests == []


def test_a_savepoint_release_does_not_publish_early(app, db, monkeypatch, offer_skus, submission_requests):
    p19 = offer_skus.p19
    _switch_on(app, monkeypatch)

    with db.session.begin_nested():
        p19.base_price = Decimal("17345")
    assert submission_requests == []

    db.session.commit()
    assert submission_requests == ONE_SUBMISSION


@pytest.mark.parametrize("column", PUBLIC_PRODUCT_COLUMNS, ids=lambda column: column.key)
def test_a_change_of_any_one_watched_column_publishes_one_submission(
    app, db, monkeypatch, offer_skus, submission_requests, column
):
    """Each watched column on its own: the detector walks them in order and stops at the first change, so a history
    read that broke for one column would otherwise hide behind the price and description tests."""
    assert set(NEW_VALUE) == {watched.key for watched in PUBLIC_PRODUCT_COLUMNS}, "a watched column has no new value"
    p19 = offer_skus.p19
    assert getattr(p19, column.key) != NEW_VALUE[column.key]
    _switch_on(app, monkeypatch)

    setattr(p19, column.key, NEW_VALUE[column.key])
    db.session.commit()

    assert submission_requests == ONE_SUBMISSION


def test_the_watched_columns_are_the_spec_list():
    assert {column.key for column in PUBLIC_PRODUCT_COLUMNS} == {
        "name",
        "description",
        "short_description",
        "meta_title",
        "meta_description",
        "base_price",
        "discount_price",
        "is_active",
        "slug",
        "min_order_quantity",
        "size",
        "volume",
        "tracks_returnable_bottles",
        "returnable_bottles_per_unit",
        "images",
    }


def test_switched_off_the_detector_does_not_look(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers
):
    """The tests' config has INDEXNOW_ENABLED off: the detector looks at no flush and nothing is requested."""
    client = app.test_client()
    p19 = offer_skus.p19
    looked = calls_to(monkeypatch, indexnow_dispatch, "_touches_public_product_facts")

    _ok(_edit(client, admin_claim_headers, p19.id, price=17345))

    assert (looked, submission_requests) == ([], [])


def test_a_failing_detector_never_fails_the_save(
    app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers, caplog
):
    client = app.test_client()
    p19 = offer_skus.p19

    def broken_detector(session):
        raise RuntimeError("the detector is broken")

    monkeypatch.setattr(indexnow_dispatch, "_touches_public_product_facts", broken_detector)
    _switch_on(app, monkeypatch)

    with captured_logs(caplog, indexnow_dispatch.logger, logging.WARNING):
        _ok(_edit(client, admin_claim_headers, p19.id, price=17345))

    db.session.expire_all()
    assert p19.base_price == Decimal("17345")
    assert submission_requests == []
    warnings = [line for line in caplog.records if line.levelno == logging.WARNING]
    assert any(line.exc_info and line.exc_info[0] is RuntimeError for line in warnings)


def test_the_detector_runs_no_sql(app, db, monkeypatch, offer_skus, submission_requests, admin_claim_headers):
    client = app.test_client()
    p19 = offer_skus.p19
    _named(db, p19)
    calls = sql_inside(monkeypatch, indexnow_dispatch, "_touches_public_product_facts")
    _switch_on(app, monkeypatch)

    _ok(_edit(client, admin_claim_headers, p19.id, price=17345))

    assert calls
    assert all(statements == [] for statements in calls)
