"""Unit tests for product fiscal metadata and marking-code inventory workflows."""

import pytest
from datetime import datetime, timezone

from business_app import db
from business_app.models.product import ProductMarkingCode
from business_app.services.product_fiscal_service import ProductFiscalService
from shared.enums import MarkingCodeStatus, PaymentMethod
from business_app.utils.exceptions import ValidationError


def test_import_marking_codes_csv_preserves_gs1_separator(db, sample_product):
    """Full GS1 marking codes containing ASCII 29 (GS) must be stored intact."""
    code_with_gs = '010460791191734421xzMK7TLH\x1d9151234567890'
    csv_content = 'code\n' + code_with_gs

    payload = ProductFiscalService().import_marking_codes_csv(sample_product.id, csv_content, actor_user_id=99)

    assert payload['created'] == 1
    stored = ProductMarkingCode.query.filter_by(product_id=sample_product.id).one()
    assert stored.code == code_with_gs


def test_import_marking_codes_csv_normalizes_literal_unicode_escape(db, sample_product):
    """Literal \\u001d escape sequences (6 chars) must be converted to the actual GS character."""
    csv_content = 'code\n010460791191734421xzMK7TLH\\u001d9151234567890'

    payload = ProductFiscalService().import_marking_codes_csv(sample_product.id, csv_content, actor_user_id=99)

    assert payload['created'] == 1
    stored = ProductMarkingCode.query.filter_by(product_id=sample_product.id).one()
    assert stored.code == '010460791191734421xzMK7TLH\x1d9151234567890'


def test_import_marking_codes_csv_gs1_codes_no_header(db, sample_product):
    """GS1 codes without a header row must not be split at the GS character."""
    code_a = 'CODE-A\x1dSEG-1'
    code_b = 'CODE-B\x1dSEG-2'
    csv_content = f'{code_a}\n{code_b}'

    payload = ProductFiscalService().import_marking_codes_csv(sample_product.id, csv_content, actor_user_id=99)

    assert payload['created'] == 2
    stored_codes = {r.code for r in ProductMarkingCode.query.filter_by(product_id=sample_product.id).all()}
    assert stored_codes == {code_a, code_b}


def test_import_marking_codes_csv_reports_existing_codes_row_by_row(db, sample_product):
    existing_code = ProductMarkingCode(
        product_id=sample_product.id,
        code='EXISTING-CODE',
        status=MarkingCodeStatus.AVAILABLE,
    )
    db.session.add(existing_code)
    db.session.commit()

    csv_content = '\n'.join([
        'code',
        'EXISTING-CODE',
        'NEW-CODE-1',
        'NEW-CODE-1',
        'NEW-CODE-2',
    ])

    payload = ProductFiscalService().import_marking_codes_csv(sample_product.id, csv_content, actor_user_id=99)

    assert payload['created'] == 2
    assert {row['code'] for row in payload['invalid_rows']} == {'EXISTING-CODE', 'NEW-CODE-1'}
    assert any(row['reason'] == 'Marking code already exists' for row in payload['invalid_rows'])
    assert any(row['reason'] == 'Duplicate code in CSV' for row in payload['invalid_rows'])


def test_update_product_fiscal_profile_accepts_first_time_enable_with_required_fields(db, sample_product):
    service = ProductFiscalService()

    updated_product = service.update_product_fiscal_profile(
        sample_product,
        {
            'barcode': '4780162410027',
            'spic': '2201101100',
            'package_code': '14101010005000',
            'units': '121232',
            'fiscalization_enabled': True,
        },
    )
    db.session.commit()

    assert updated_product.fiscal_profile is not None
    assert updated_product.barcode == '4780162410027'
    assert updated_product.spic == '2201101100'
    assert updated_product.units == '121232'
    assert updated_product.fiscalization_enabled is True


def test_update_product_fiscal_profile_enable_without_barcode_and_units(db, sample_product):
    service = ProductFiscalService()

    updated_product = service.update_product_fiscal_profile(
        sample_product,
        {
            'spic': '2201101100',
            'package_code': '14101010005000',
            'fiscalization_enabled': True,
        },
    )
    db.session.commit()

    assert updated_product.fiscal_profile is not None
    assert updated_product.spic == '2201101100'
    assert updated_product.package_code == '14101010005000'
    assert updated_product.barcode is None
    assert updated_product.units is None
    assert updated_product.fiscalization_enabled is True


def test_update_product_fiscal_profile_enable_without_package_code(db, sample_product):
    service = ProductFiscalService()

    updated_product = service.update_product_fiscal_profile(
        sample_product,
        {
            'spic': '2201101100',
            'package_code': '',
            'fiscalization_enabled': True,
        },
    )
    db.session.commit()

    assert updated_product.fiscal_profile is not None
    assert updated_product.spic == '2201101100'
    assert updated_product.package_code is None
    assert updated_product.fiscalization_enabled is True


# ======================================================================
# Stock quantity sync from marking codes
# ======================================================================

from business_app.models.product import ProductFiscalProfile


def test_sync_stock_from_marking_codes_updates_quantity(db, sample_product):
    """When requires_marking_codes=True, stock_quantity = available marking codes count."""
    service = ProductFiscalService()
    profile = ProductFiscalProfile(
        product_id=sample_product.id,
        fiscalization_enabled=True,
        requires_marking_codes=True,
        spic='SPIC-TEST',
    )
    db.session.add(profile)
    db.session.add_all([
        ProductMarkingCode(product_id=sample_product.id, code='SYNC-001', status=MarkingCodeStatus.AVAILABLE),
        ProductMarkingCode(product_id=sample_product.id, code='SYNC-002', status=MarkingCodeStatus.AVAILABLE),
        ProductMarkingCode(product_id=sample_product.id, code='SYNC-003', status=MarkingCodeStatus.RESERVED),
        ProductMarkingCode(product_id=sample_product.id, code='SYNC-004', status=MarkingCodeStatus.USED),
    ])
    db.session.flush()

    count = service.sync_stock_from_marking_codes(sample_product)

    assert count == 2
    assert sample_product.stock_quantity == 2


def test_sync_stock_noop_when_not_requires_marking_codes(db, sample_product):
    """When requires_marking_codes=False, stock_quantity is not changed."""
    service = ProductFiscalService()
    sample_product.stock_quantity = 50
    db.session.flush()

    count = service.sync_stock_from_marking_codes(sample_product)

    assert count == 50
    assert sample_product.stock_quantity == 50


def test_create_marking_codes_syncs_stock(db, sample_product):
    """Creating marking codes should sync stock_quantity automatically."""
    service = ProductFiscalService()
    profile = ProductFiscalProfile(
        product_id=sample_product.id,
        fiscalization_enabled=True,
        requires_marking_codes=True,
        spic='SPIC-SYNC',
    )
    db.session.add(profile)
    db.session.flush()

    result = service.create_marking_codes(
        sample_product.id,
        ['AUTO-SYNC-001', 'AUTO-SYNC-002', 'AUTO-SYNC-003'],
    )

    assert result['created'] == 3
    assert sample_product.stock_quantity == 3


def _seed_pool(db, product_id):
    """Four codes covering both AVAILABLE utilisation states plus RESERVED/USED."""
    now = datetime.now(timezone.utc)
    codes = [
        ProductMarkingCode(product_id=product_id, code='POOL-AVAIL-NULL',
                           status=MarkingCodeStatus.AVAILABLE, tax_committee_utilised_at=None),
        ProductMarkingCode(product_id=product_id, code='POOL-AVAIL-UTIL',
                           status=MarkingCodeStatus.AVAILABLE, tax_committee_utilised_at=now),
        ProductMarkingCode(product_id=product_id, code='POOL-RESERVED',
                           status=MarkingCodeStatus.RESERVED, tax_committee_utilised_at=None),
        ProductMarkingCode(product_id=product_id, code='POOL-USED',
                           status=MarkingCodeStatus.USED, tax_committee_utilised_at=now),
    ]
    db.session.add_all(codes)
    db.session.flush()


def test_list_marking_codes_available_unutilised_returns_only_null_timestamp(db, sample_product):
    """available_unutilised → only AVAILABLE codes with tax_committee_utilised_at IS NULL."""
    _seed_pool(db, sample_product.id)

    result = ProductFiscalService().list_marking_codes(sample_product.id, status='available_unutilised')

    returned = {item['code'] for item in result['items']}
    assert returned == {'POOL-AVAIL-NULL'}


def test_list_marking_codes_available_pre_utilised_returns_only_not_null_timestamp(db, sample_product):
    """available_pre_utilised → only AVAILABLE codes with tax_committee_utilised_at IS NOT NULL."""
    _seed_pool(db, sample_product.id)

    result = ProductFiscalService().list_marking_codes(sample_product.id, status='available_pre_utilised')

    returned = {item['code'] for item in result['items']}
    assert returned == {'POOL-AVAIL-UTIL'}


def test_list_marking_codes_available_includes_both_utilisation_states(db, sample_product):
    """Regression: plain 'available' still returns ALL AVAILABLE codes regardless of utilisation."""
    _seed_pool(db, sample_product.id)

    result = ProductFiscalService().list_marking_codes(sample_product.id, status='available')

    returned = {item['code'] for item in result['items']}
    assert returned == {'POOL-AVAIL-NULL', 'POOL-AVAIL-UTIL'}


def test_list_marking_codes_invalid_status_still_raises(db, sample_product):
    """Unknown status values are still rejected."""
    _seed_pool(db, sample_product.id)

    with pytest.raises(ValidationError):
        ProductFiscalService().list_marking_codes(sample_product.id, status='bogus_status')


def test_consumes_marking_code_true_for_card_on_derived_product(db, sample_product):
    profile = ProductFiscalProfile(
        product_id=sample_product.id,
        fiscalization_enabled=True,
        requires_marking_codes=True,
        spic='SPIC-CONSUME',
    )
    db.session.add(profile)
    db.session.flush()

    assert ProductFiscalService.consumes_marking_code(sample_product, PaymentMethod.CLICK) is True
    assert ProductFiscalService.consumes_marking_code(sample_product, PaymentMethod.CARD) is True


def test_consumes_marking_code_false_for_cash(db, sample_product):
    profile = ProductFiscalProfile(
        product_id=sample_product.id,
        fiscalization_enabled=True,
        requires_marking_codes=True,
        spic='SPIC-CONSUME-CASH',
    )
    db.session.add(profile)
    db.session.flush()

    assert ProductFiscalService.consumes_marking_code(sample_product, PaymentMethod.CASH) is False


def test_consumes_marking_code_false_for_non_derived_product(db, sample_product):
    """No fiscal profile: card still consumes no code, because there is no pool."""
    assert ProductFiscalService.consumes_marking_code(sample_product, PaymentMethod.CLICK) is False


def test_consumes_marking_code_accepts_raw_string_and_none(db, sample_product):
    profile = ProductFiscalProfile(
        product_id=sample_product.id,
        fiscalization_enabled=True,
        requires_marking_codes=True,
        spic='SPIC-CONSUME-STR',
    )
    db.session.add(profile)
    db.session.flush()

    assert ProductFiscalService.consumes_marking_code(sample_product, 'click') is True
    assert ProductFiscalService.consumes_marking_code(sample_product, 'cash') is False
    # NULL payment_method must never be treated as cash.
    assert ProductFiscalService.consumes_marking_code(sample_product, None) is False


# ======================================================================
# pool_covers_order (Task 8 round 2: C-1 flip guard + I-2 reward carve-out)
# ======================================================================

from decimal import Decimal

from business_app.models.order import OrderItem


def test_pool_covers_order_skips_reward_items(db, sample_user, sample_product, sample_order):
    """I-2: a loyalty free-product reward line never draws a code (mirrors
    PaymentFiscalizationService._is_fiscalizable_item / reserve_required_
    marking_codes, which skip it entirely). An empty pool must not refuse an
    order whose only marking-code-product line is a reward."""
    profile = ProductFiscalProfile(
        product_id=sample_product.id,
        fiscalization_enabled=True,
        requires_marking_codes=True,
        spic='SPIC-POOL-REWARD',
    )
    db.session.add(profile)
    db.session.add(
        OrderItem(
            order_id=sample_order.id,
            product_id=sample_product.id,
            quantity=2,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("30000.00"),
            is_reward_item=True,
        )
    )
    db.session.commit()
    # 0 marking codes exist at all for this product -- would refuse if the
    # reward line were counted as code-consuming.

    pool_ok, short_product = ProductFiscalService().pool_covers_order(sample_order, PaymentMethod.CLICK)
    assert pool_ok is True
    assert short_product is None


def test_pool_covers_order_still_refuses_a_non_reward_line_short_on_codes(
    db, sample_user, sample_product, sample_order
):
    """Companion to the reward carve-out above: it must not blanket-skip the
    whole order -- a genuine (non-reward) short line still refuses."""
    profile = ProductFiscalProfile(
        product_id=sample_product.id,
        fiscalization_enabled=True,
        requires_marking_codes=True,
        spic='SPIC-POOL-REWARD-2',
    )
    db.session.add(profile)
    db.session.add(
        OrderItem(
            order_id=sample_order.id,
            product_id=sample_product.id,
            quantity=2,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("30000.00"),
            is_reward_item=False,
        )
    )
    db.session.commit()

    pool_ok, short_product = ProductFiscalService().pool_covers_order(sample_order, PaymentMethod.CLICK)
    assert pool_ok is False
    assert short_product == sample_product.name


def test_pool_covers_order_credits_codes_already_reserved_to_this_order(
    db, sample_user, sample_product, sample_order
):
    """C-1b unit-level check: a code RESERVED and owned by THIS order counts
    as covered even though the shared AVAILABLE pool is empty -- PREPARE
    would succeed on a code the payment already holds."""
    profile = ProductFiscalProfile(
        product_id=sample_product.id,
        fiscalization_enabled=True,
        requires_marking_codes=True,
        spic='SPIC-POOL-RESERVED',
    )
    db.session.add(profile)
    db.session.add(
        OrderItem(
            order_id=sample_order.id,
            product_id=sample_product.id,
            quantity=2,
            unit_price=Decimal("15000.00"),
            total_price=Decimal("30000.00"),
        )
    )
    for index in range(2):
        db.session.add(
            ProductMarkingCode(
                product_id=sample_product.id,
                order_id=sample_order.id,
                code=f"POOL-RESERVED-{index}",
                status=MarkingCodeStatus.RESERVED,
            )
        )
    db.session.commit()

    pool_ok, short_product = ProductFiscalService().pool_covers_order(sample_order, PaymentMethod.CLICK)
    assert pool_ok is True
    assert short_product is None

    # A code RESERVED to a DIFFERENT order must not be credited -- ownership,
    # not just status, is the test.
    ProductMarkingCode.query.filter_by(product_id=sample_product.id).update({"order_id": sample_order.id + 999})
    db.session.commit()

    pool_ok, short_product = ProductFiscalService().pool_covers_order(sample_order, PaymentMethod.CLICK)
    assert pool_ok is False
    assert short_product == sample_product.name

    # N-1: USED codes owned by THIS order must ALSO count as covered -- not
    # just RESERVED. A settled business_account order's codes are marked
    # USED at settlement (see _seed_business_account_settled_order /
    # _marking_codes_consumed_warnings in test_order_payment_method_edit_
    # service.py), and PaymentFiscalizationService._codes_currently_held
    # admits RESERVED/USED/UTILISED for "does this payment still hold this
    # code" -- so a RESERVED-only credit here was stricter than what PREPARE
    # itself would actually require.
    ProductMarkingCode.query.filter_by(product_id=sample_product.id).update(
        {"order_id": sample_order.id, "status": MarkingCodeStatus.USED}
    )
    db.session.commit()

    pool_ok, short_product = ProductFiscalService().pool_covers_order(sample_order, PaymentMethod.CLICK)
    assert pool_ok is True
    assert short_product is None

    # A USED code owned by a DIFFERENT order must not be credited either --
    # same ownership requirement as the RESERVED case above.
    ProductMarkingCode.query.filter_by(product_id=sample_product.id).update(
        {"order_id": sample_order.id + 999}
    )
    db.session.commit()

    pool_ok, short_product = ProductFiscalService().pool_covers_order(sample_order, PaymentMethod.CLICK)
    assert pool_ok is False
    assert short_product == sample_product.name
