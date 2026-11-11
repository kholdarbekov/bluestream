import json
from typing import Any, Dict, Tuple, Type, Union

from flask import Response, request
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from business_app.utils.api_responses import validation_error_response
from business_app.utils.exceptions import ValidationError

_TRUE_VALUES = ("true", "1", "yes", "on")


def parse_bool_arg(name, default=None):
    """Parse a query-string boolean without Flask's broken ``type=bool``
    (``bool("false")`` is True). Returns ``default`` when the arg is absent;
    any present value is truthy only if it is one of true/1/yes/on (case-insensitive)."""
    raw = request.args.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def validated_json_payload(schema_cls: Type[BaseModel]) -> Union[Dict[str, Any], Tuple[Response, int]]:
    """The request's JSON body validated by `schema_cls`, or the 400 to return instead.

    The caller checks `isinstance(result, dict)` and returns anything else as it is.

    exclude_unset, NOT exclude_none: a field the client never mentioned is dropped, but a null it
    deliberately sent survives, so emptying an input in the admin UI actually clears the column
    instead of returning a success toast that wrote nothing.

    The rule the services then apply: an explicit null CLEARS a nullable column, and a NOT-NULL
    column REFUSES it (`outlets.preferred_language` keeps its truthiness guard; `outlets.payment_terms`
    400s with SALES_PAYMENT_TERMS_INVALID). Both halves are pinned in
    tests/integration/test_admin_sales_outlets_api.py.

    The one copy for `admin_sales.py`, `staff_sales.py` and `admin_sales_pay.py`. The
    `_validated_payload` helpers in `staff_tryouts.py`, `admin_tryouts.py` and `admin_bottles.py`
    dump with `exclude_none`, which is a different rule.
    """
    payload = request.get_json() or {}
    try:
        return schema_cls(**payload).model_dump(exclude_unset=True)
    except PydanticValidationError as exc:
        return validation_error_response(exc.errors())


def json_body_as_sent(*, error_code: str) -> Dict[str, Any]:
    """The request's JSON object exactly as the client sent it. No body at all is an empty object.

    `TimezoneMiddleware.before_request` rewrites the datetime-named fields (`paid_at`, `created_at`, ...)
    of the cached `request.get_json()` in place: it reads a NAIVE value as the user's local time and
    rewrites every listed field to a UTC +00:00 string, so a route that must refuse naive values (an
    explicit offset required) reads the raw body here, from the bytes the middleware leaves untouched.

    Text that is not JSON, or JSON that is not an object (an array, a string, a number, null), raises a
    ``ValidationError`` carrying the caller's ``error_code``: a broken body is named as one, rather than
    read as empty and refused for whichever field happens to be checked first.
    """
    raw = request.get_data(cache=True, as_text=True)
    if not raw.strip():
        return {}
    invalid = ValidationError("The request body must be a JSON object", error_code=error_code)
    try:
        body = json.loads(raw)
    except ValueError:
        raise invalid
    if not isinstance(body, dict):
        raise invalid
    return body
