"""The six choices the shared-place case screen offers are spelled once, beside the case vocabulary.

`SHARED_PLACE_CHOICE_KINDS` lives in models/shared_place_case.py, so the request models (read by the staff routes too)
never load the admin review service to learn them, and the "every choice names its fields" rule is pinned here rather
than by a module-level `assert` that `python -O` strips.
"""

import ast
import inspect

import pytest

from business_app.models.shared_place_case import SHARED_PLACE_CHOICE_KINDS, SHARED_PLACE_RESOLUTION_KINDS
from business_app.serializers import shared_place_serializers
from business_app.services.shared_place import review


@pytest.mark.unit
def test_choice_kinds_are_the_resolution_kinds_then_close():
    assert SHARED_PLACE_CHOICE_KINDS == SHARED_PLACE_RESOLUTION_KINDS + ("close",)


@pytest.mark.unit
def test_every_choice_the_screen_offers_names_its_resolve_fields():
    assert tuple(shared_place_serializers._RESOLVE_FIELDS_BY_KIND) == SHARED_PLACE_CHOICE_KINDS


@pytest.mark.unit
def test_the_resolve_request_accepts_exactly_the_choice_kinds():
    field = shared_place_serializers.SharedPlaceResolveRequest.model_fields["kind"]
    assert field.annotation.__args__ == SHARED_PLACE_CHOICE_KINDS


@pytest.mark.unit
def test_the_screen_offers_the_same_kinds():
    assert review.SHARED_PLACE_CHOICE_KINDS is SHARED_PLACE_CHOICE_KINDS


@pytest.mark.unit
def test_the_request_models_import_no_service():
    """staff.py imports these models lazily; a service import here would load the admin review service with them."""
    tree = ast.parse(inspect.getsource(shared_place_serializers))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert not {name for name in imported if name and name.startswith("business_app.services")}
