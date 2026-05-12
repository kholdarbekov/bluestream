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


def test_batched_namespace_request_returns_nested_envelope(client, db):
    """i18next-http-backend with allowMultiLoading sends `+`-joined namespaces.

    The route must collapse them into the `{lng: {ns: {...}}}` envelope that
    i18next expects when parsing a multi-namespace response.
    """
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
        key="ui.login.app_name",
        value="Bluestream Login",
        category="ui_login",
    )

    response = client.get("/api/v1/translations/en/common+navigation+login")
    data = response.get_json()

    assert response.status_code == 200
    # Envelope shape: { "en": { "common": {...}, "navigation": {...}, "login": {...} } }
    assert "en" in data
    assert set(data["en"].keys()) == {"common", "navigation", "login"}

    # `common` aggregates shared `ui` + all `ui_*` categories — see
    # AdminUiTranslationService.get_translations().
    assert data["en"]["common"]["ui.app_name_full"] == "BlueStream Admin"
    assert data["en"]["common"]["ui.nav.dashboard"] == "Dashboard"
    assert data["en"]["common"]["ui.login.app_name"] == "Bluestream Login"

    # Scoped namespaces return only their own category's keys.
    assert data["en"]["navigation"]["ui.nav.dashboard"] == "Dashboard"
    assert data["en"]["login"]["ui.login.app_name"] == "Bluestream Login"


def test_batched_namespace_request_tolerates_unknown_namespace(client, db):
    """Unknown namespaces should yield an empty bundle, not a 500 — i18next
    must still be able to parse the response even if one namespace was a
    typo."""
    _create_translation(
        db,
        key="ui.app_name_full",
        value="BlueStream Admin",
        category="ui",
    )

    response = client.get("/api/v1/translations/en/common+does_not_exist")
    data = response.get_json()

    assert response.status_code == 200
    assert data["en"]["common"]["ui.app_name_full"] == "BlueStream Admin"
    assert data["en"]["does_not_exist"] == {}
