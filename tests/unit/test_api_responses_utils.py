"""Unit tests for standardized API response helpers."""

import pytest

from business_app.utils.api_responses import (
    APIResponse,
    PaginationMeta,
    conflict_response,
    created_response,
    error_response,
    forbidden_response,
    internal_error_response,
    no_content_response,
    not_found_response,
    paginated_response,
    success_response,
    unauthorized_response,
    validation_error_response,
)


@pytest.mark.unit
class TestAPIResponseModels:
    def test_api_response_model_serialization(self):
        model = APIResponse(success=True, data={"id": 1}, message="ok")
        dumped = model.model_dump()
        assert dumped["success"] is True
        assert dumped["data"]["id"] == 1

    def test_pagination_meta_model_constraints(self):
        meta = PaginationMeta(page=1, per_page=10, total=25, pages=3, has_next=True, has_prev=False)
        dumped = meta.model_dump()
        assert dumped["pages"] == 3
        assert dumped["has_next"] is True


@pytest.mark.unit
class TestAPIResponseFunctions:
    def test_success_response_default_status(self, app):
        with app.app_context():
            response, status = success_response(data={"a": 1}, message="done")

        assert status == 200
        body = response.get_json()
        assert body["success"] is True
        assert body["data"] == {"a": 1}
        assert body["message"] == "done"

    def test_error_response_supports_string_list_and_dict_errors(self, app):
        with app.app_context():
            response_a, status_a = error_response("bad", errors="one")
            response_b, status_b = error_response("bad", errors=["x", "y"])
            response_c, status_c = error_response("bad", errors={"email": ["required"], "phone": "invalid"})

        assert status_a == 400
        assert response_a.get_json()["errors"] == ["one"]
        assert status_b == 400
        assert response_b.get_json()["errors"] == ["x", "y"]
        assert status_c == 400
        assert "email: required" in response_c.get_json()["errors"]
        assert "phone: invalid" in response_c.get_json()["errors"]

    def test_paginated_response_includes_meta(self, app):
        with app.app_context():
            response, status = paginated_response(
                items=[{"id": 1}, {"id": 2}],
                page=2,
                per_page=2,
                total=5,
                message="ok",
                additional_meta={"source": "test"},
            )

        body = response.get_json()
        assert status == 200
        assert body["success"] is True
        assert body["data"]["items"][0]["id"] == 1
        assert body["meta"]["page"] == 2
        assert body["meta"]["pages"] == 3
        assert body["meta"]["source"] == "test"

    def test_created_and_no_content_helpers(self, app):
        with app.app_context():
            created_resp, created_status = created_response(data={"id": 9})
            empty_resp, empty_status = no_content_response()

        assert created_status == 201
        assert created_resp.get_json()["data"]["id"] == 9
        assert empty_resp == ""
        assert empty_status == 204

    def test_not_found_unauthorized_forbidden_conflict_helpers(self, app):
        with app.app_context():
            nf_resp, nf_status = not_found_response(resource_type="Order")
            unauth_resp, unauth_status = unauthorized_response()
            forbidden_resp, forbidden_status = forbidden_response("blocked")
            conflict_resp, conflict_status = conflict_response()

        assert nf_status == 404
        assert nf_resp.get_json()["message"] == "Order not found"
        assert unauth_status == 401
        assert forbidden_status == 403
        assert forbidden_resp.get_json()["message"] == "blocked"
        assert conflict_status == 409

    def test_validation_error_response_formats_multiple_shapes(self, app):
        pydantic_like = [{"loc": ["email"], "msg": "invalid"}]
        dict_like = {"phone": "required"}

        with app.app_context():
            response_a, status_a = validation_error_response(pydantic_like)
            response_b, status_b = validation_error_response(dict_like)
            response_c, status_c = validation_error_response("simple error")

        assert status_a == 400
        assert "email: invalid" in response_a.get_json()["errors"]
        assert status_b == 400
        assert "phone: required" in response_b.get_json()["errors"]
        assert status_c == 400
        assert response_c.get_json()["errors"] == ["simple error"]

    def test_internal_error_response_with_error_id(self, app):
        with app.app_context():
            response, status = internal_error_response(error_id="ERR-001")

        body = response.get_json()
        assert status == 500
        assert body["success"] is False
        assert body["meta"]["error_id"] == "ERR-001"
