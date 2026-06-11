"""Unit tests for product fiscal metadata and marking-code inventory workflows."""

import pytest
from datetime import datetime, timezone

from business_app import db
from business_app.models.product import ProductMarkingCode
from business_app.services.product_fiscal_service import ProductFiscalService
from shared.enums import MarkingCodeStatus
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
