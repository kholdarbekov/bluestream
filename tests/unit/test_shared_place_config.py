"""Shared-place detection settings (Task 1)."""
import importlib

import pytest


@pytest.fixture
def restore_business_config():
    import shared.business_config as bc

    radius, window = bc.SHARED_PLACE_CANDIDATE_RADIUS_M, bc.SHARED_PLACE_DRIVER_ANSWER_WINDOW_S
    yield
    importlib.reload(bc)
    bc.SHARED_PLACE_CANDIDATE_RADIUS_M = radius
    bc.SHARED_PLACE_DRIVER_ANSWER_WINDOW_S = window


@pytest.mark.unit
def test_defaults(monkeypatch, restore_business_config):
    monkeypatch.delenv("SHARED_PLACE_CANDIDATE_RADIUS_M", raising=False)
    monkeypatch.delenv("SHARED_PLACE_DRIVER_ANSWER_WINDOW_S", raising=False)
    import shared.business_config as bc

    bc = importlib.reload(bc)
    assert bc.SHARED_PLACE_CANDIDATE_RADIUS_M == 50.0
    assert bc.SHARED_PLACE_DRIVER_ANSWER_WINDOW_S == 900


@pytest.mark.unit
def test_env_overrides(monkeypatch, restore_business_config):
    monkeypatch.setenv("SHARED_PLACE_CANDIDATE_RADIUS_M", "35")
    monkeypatch.setenv("SHARED_PLACE_DRIVER_ANSWER_WINDOW_S", "600")
    import shared.business_config as bc

    bc = importlib.reload(bc)
    assert bc.SHARED_PLACE_CANDIDATE_RADIUS_M == 35.0
    assert bc.SHARED_PLACE_DRIVER_ANSWER_WINDOW_S == 600


@pytest.mark.unit
def test_flask_config_mirrors_the_shared_literals(app):
    assert app.config["SHARED_PLACE_CANDIDATE_RADIUS_M"] == 50.0
    assert app.config["SHARED_PLACE_DRIVER_ANSWER_WINDOW_S"] == 900
