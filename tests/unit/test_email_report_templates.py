"""Report email templates (daily/weekly/churn/demand/kpi) must render.

These admin report emails had no filesystem template, so render_template
returned None every day at 18:00 UTC (WARNING + ERROR in prod) and no email
was ever sent. Each report type now has a uz/en/ru template + subject.

The per-language marker asserts the RIGHT language file rendered (not a
cross-language fallback), and 'None' must never leak from an optional key.
"""

import pytest

from business_app.services.email_template_service import EmailTemplateService

# Representative payloads mirroring business_app/tasks/analytics_tasks.py sends.
PAYLOADS = {
    "daily_report": {
        "report_date": "2026-07-01",
        "total_revenue": 1500000,
        "total_orders": 42,
        "new_customers": 7,
    },
    "weekly_business_report": {
        "week_ending": "2026-07-06",
        "total_revenue": 9800000,
        "revenue_growth": 12.5,
        "total_orders": 310,
        "customer_acquisition": 25,
        "key_insights": ["Revenue up 12%", "New product strong", "Retention improving"],
    },
    "churn_alert": {
        "high_risk_count": 3,
        "total_at_risk": 8,
        "top_customers": [
            {"user_id": 1, "user_name": "Ali Valiyev", "email": "a@x.uz", "churn_probability": 0.82, "risk_level": "high"},
        ],
    },
    "demand_forecast": {
        "forecast_period": "30 days",
        "total_predicted_orders": 900,
        "daily_average": 30.0,
        "model_accuracy": "87%",
    },
    "kpi_alert": {
        "date": "2026-07-02",
        "alerts": ["Order volume dropped significantly: 5 today vs 20 yesterday"],
        "today_orders": 5,
        "today_revenue": 250000,
    },
    # Mirrors business_app/tasks/inventory_tasks.py sends (task C3).
    "low_stock_alert": {
        "product_id": 42,
        "product_name": "19L Aqua Element",
        "sku": "AE-19L-001",
        "current_stock": 3,
        "available_quantity": 3,
        "min_stock_level": 10,
        "is_out_of_stock": False,
    },
    "inventory_report": {
        "report_type": "daily",
        "generated_at": "2026-07-01T06:00:00+00:00",
        "total_products": 25,
        "low_stock_count": 1,
        "out_of_stock_count": 1,
        "total_inventory_value": 12500000.0,
        "low_stock_products": [
            {"id": 1, "name": "19L Aqua Element", "sku": "AE-19L-001", "stock": 5, "min_level": 10}
        ],
        "out_of_stock_products": [{"id": 2, "name": "0.5L Aqua Element", "sku": "AE-05L-001", "stock": 0}],
    },
    "reorder_suggestions": {
        "products_to_reorder": [
            {
                "product_id": 1,
                "product_name": "19L Aqua Element",
                "sku": "AE-19L-001",
                "current_stock": 5,
                "available_quantity": 5,
                "min_stock_level": 10,
                "max_stock_level": 200,
                "suggested_quantity": 195,
            }
        ],
        "total_products": 1,
        "generated_at": "2026-07-01T06:30:00+00:00",
    },
}

# Unique per-language header text — proves the correct language file rendered.
MARKERS = {
    "daily_report": {"uz": "Kunlik hisobot", "en": "Daily Report", "ru": "Ежедневный отчёт"},
    "weekly_business_report": {
        "uz": "Haftalik biznes hisoboti",
        "en": "Weekly Business Report",
        "ru": "Еженедельный бизнес-отчёт",
    },
    "churn_alert": {"uz": "Mijozlar ketishi", "en": "Customer Churn", "ru": "Отток клиентов"},
    "demand_forecast": {"uz": "Talab prognozi", "en": "Demand Forecast", "ru": "Прогноз спроса"},
    "kpi_alert": {"uz": "KPI", "en": "KPI", "ru": "KPI"},
    "low_stock_alert": {
        "uz": "Kam zaxira ogohlantirishi",
        "en": "Low Stock Alert",
        "ru": "Предупреждение о низком запасе",
    },
    "inventory_report": {"uz": "Inventar hisoboti", "en": "Inventory Report", "ru": "Отчёт по инвентарю"},
    "reorder_suggestions": {
        "uz": "Qayta buyurtma takliflari",
        "en": "Reorder Suggestions",
        "ru": "Предложения по дозаказу",
    },
}

# A scalar value from each payload that must appear verbatim in the body.
SCALAR = {
    "daily_report": "42",
    "weekly_business_report": "310",
    "churn_alert": "3",
    "demand_forecast": "900",
    "kpi_alert": "5",
    "low_stock_alert": "AE-19L-001",
    "inventory_report": "19L Aqua Element",
    "reorder_suggestions": "195",
}

_CASES = [(rt, lang) for rt in PAYLOADS for lang in ("uz", "en", "ru")]


@pytest.mark.unit
@pytest.mark.parametrize("report_type,language", _CASES)
def test_report_template_renders(app, report_type, language):
    with app.app_context():
        html = EmailTemplateService().render_template(report_type, language, PAYLOADS[report_type])

    assert html is not None, f"{report_type}/{language} template missing"
    assert MARKERS[report_type][language] in html, f"{report_type}/{language} rendered wrong language"
    assert SCALAR[report_type] in html
    assert "None" not in html


@pytest.mark.unit
@pytest.mark.parametrize("report_type,language", _CASES)
def test_report_subject_is_non_empty(app, report_type, language):
    with app.app_context():
        subject = EmailTemplateService().get_subject(report_type, language, PAYLOADS[report_type])

    assert subject and subject.strip(), f"{report_type}/{language} subject empty"
    assert "{" not in subject, "unresolved subject placeholder"
