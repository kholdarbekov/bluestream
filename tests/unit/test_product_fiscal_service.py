"""Unit tests for product fiscal metadata and marking-code inventory workflows."""

from business_app import db
from business_app.models.product import ProductMarkingCode
from business_app.services.product_fiscal_service import ProductFiscalService
from business_app.utils.constants import MarkingCodeStatus


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
