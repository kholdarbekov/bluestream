"""Report email templates (daily/weekly/churn/demand/kpi) must render.

These admin report emails had no filesystem template, so render_template
returned None every day at 18:00 UTC (WARNING + ERROR in prod) and no email
was ever sent. Each report type now has a uz/en/ru template + subject.

The per-language marker asserts the RIGHT language file rendered (not a
cross-language fallback), and 'None' must never leak from an optional key.
"""

import pathlib
import re

import pytest

import business_app
from business_app.services.email_template_service import EmailTemplateService
from business_app.services.sales.agent_metrics_service import METRIC_KEYS

# Resolved from the package, not from the runner's cwd: these tests are collected
# inside the container from a path the host does not share.
EMAIL_TEMPLATES_DIR = pathlib.Path(business_app.__file__).parent / "templates" / "emails"

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
    # Mirrors business_app/tasks/analytics_tasks.py::generate_weekly_agent_performance_report.
    # The second agent's week is empty on purpose: every ratio is null, which is
    # what "None" not in html (below) proves the templates print as an em dash.
    "agent_performance": {
        "week_start": "2026-09-07",
        "week_end": "2026-09-13",
        "agent_count": 2,
        "top_agent_name": "Sardor Agent",
        "agents": [
            {
                "agent_user_id": 11,
                "agent_name": "Sardor Agent",
                "phone": "+998901234585",
                "planned_visits": 18,
                "completed_visits": 15,
                "plan_vs_fact_pct": 83.3,
                "unplanned_visits": 2,
                "visits_per_day": 2.1,
                "strike_rate_pct": 60.0,
                "assigned_outlets": 24,
                "active_outlets": 9,
                "active_share_pct": 37.5,
                "new_outlets_registered": 4,
                "new_outlets_activated": 2,
                "orders_placed": 9,
                "orders_delivered_paid": 6,
                "bottles_delivered_paid": 137,
                "revenue_delivered_paid": 2480000.0,
                "agent_orders_cancelled": 1,
                "suggested_vs_accepted_pct": 72.5,
                "out_of_range_checkins": 1,
                "skipped_checkins": 2,
                "avg_visit_minutes": 12.4,
            },
            {
                "agent_user_id": 12,
                "agent_name": "Dilnoza Agent",
                "phone": "+998901234586",
                "planned_visits": 0,
                "completed_visits": 0,
                "plan_vs_fact_pct": None,
                "unplanned_visits": 0,
                "visits_per_day": 0.0,
                "strike_rate_pct": None,
                "assigned_outlets": 0,
                "active_outlets": 0,
                "active_share_pct": None,
                "new_outlets_registered": 0,
                "new_outlets_activated": 0,
                "orders_placed": 0,
                "orders_delivered_paid": 0,
                "bottles_delivered_paid": 0,
                "revenue_delivered_paid": 0.0,
                "agent_orders_cancelled": 0,
                "suggested_vs_accepted_pct": None,
                "out_of_range_checkins": 0,
                "skipped_checkins": 0,
                "avg_visit_minutes": None,
            },
        ],
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
    "agent_performance": {
        "uz": "Agentlar samaradorligi",
        "en": "Agent Performance",
        "ru": "Эффективность агентов",
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
    "agent_performance": "137",
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


@pytest.mark.unit
def test_the_agent_performance_notification_type_resolves_to_its_template(app):
    """`render_notification_email` is the real send path: it maps the notification
    TYPE through TEMPLATE_MAPPING before rendering and pulls the subject from
    EMAIL_SUBJECTS. The two parametrized tests above call render_template() and
    get_subject() with the template NAME directly, so they stay green even with
    the mapping entry missing and the subject silently empty."""
    with app.app_context():
        rendered = EmailTemplateService().render_notification_email(
            "agent_performance", "en", PAYLOADS["agent_performance"]
        )

    assert rendered is not None, "agent_performance does not resolve to a template"
    assert rendered["subject"].startswith("Agent Performance (week of 2026-09-07)")
    assert "{" not in rendered["subject"], "unresolved subject placeholder"
    assert MARKERS["agent_performance"]["en"] in rendered["content"]
    assert SCALAR["agent_performance"] in rendered["content"]


@pytest.mark.unit
@pytest.mark.parametrize("language", ("uz", "en", "ru"))
def test_the_agent_performance_template_prints_every_metric_key_in_order(language):
    """Each language's email prints all 20 KPI keys, in METRIC_KEYS order.

    Nothing else can see a renamed key: Jinja's default undefined renders a missing
    attribute as the EMPTY string, so `"None" not in html` stays green while the email
    silently ships a blank column, and `test_report_template_renders` would too. The
    task-level test pins the keys on the PRODUCER side only. This reads the template
    source rather than rendered HTML, so it also pins the ORDER the brief calls the
    pinned group order (visits 6, outlets 5, orders 6, discipline 3).
    """
    source = (EMAIL_TEMPLATES_DIR / language / "agent_performance.html").read_text(encoding="utf-8")

    assert re.findall(r"cell\(agent\.(\w+)\)", source) == list(METRIC_KEYS)
