"""Route regressions for admin UI translation loading."""

from business_app.models.translation import Translation


def _create_translation(
    db,
    *,
    key: str,
    value: str,
    language: str = "en",
    category: str = "ui",
) -> None:
    db.session.add(
        Translation(
            key=key,
            language=language,
            value=value,
            category=category,
            is_active=True,
        )
    )
    db.session.commit()


def test_navigation_namespace_includes_legacy_shared_ui_keys(client, db):
    _create_translation(
        db,
        key="ui.nav.corporate_contracts",
        value="Corporate Contracts",
        category="ui",
    )

    response = client.get("/api/v1/translations/en/navigation")

    assert response.status_code == 200
    assert response.get_json()["ui.nav.corporate_contracts"] == "Corporate Contracts"


def test_common_namespace_aggregates_shared_and_scoped_ui_categories(client, db):
    _create_translation(
        db,
        key="ui.app_name_full",
        value="BlueStream Admin",
        category="ui",
    )
    _create_translation(
        db,
        key="ui.nav.dashboard",
        value="Dashboard",
        category="ui_navigation",
    )
    _create_translation(
        db,
        key="ui.dashboard.title",
        value="Admin Dashboard",
        category="ui_dashboard",
    )

    response = client.get("/api/v1/translations/en/common")
    data = response.get_json()

    assert response.status_code == 200
    assert data["ui.app_name_full"] == "BlueStream Admin"
    assert data["ui.nav.dashboard"] == "Dashboard"
    assert data["ui.dashboard.title"] == "Admin Dashboard"


def test_namespaces_include_common_and_scoped_ui_categories(client, db):
    _create_translation(
        db,
        key="ui.nav.dashboard",
        value="Dashboard",
        category="ui_navigation",
    )
    _create_translation(
        db,
        key="ui.login.app_name",
        value="Bluestream Login",
        category="ui_login",
    )

    response = client.get("/api/v1/translations/namespaces")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["success"] is True
    assert "common" in payload["namespaces"]
    assert "login" in payload["namespaces"]
    assert "navigation" in payload["namespaces"]
