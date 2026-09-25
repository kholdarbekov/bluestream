"""The bot may show a backend `message` only when a person wrote it.

`ExceptionMapper` maps plain ValueError/TypeError/KeyError to 4xx with
``str(exception)`` as the message — internals, not a reason. The bot filters
them by error type using `shared.api_errors.RAW_EXCEPTION_ERROR_TYPES`; this
pins that set to the mapper so neither side can drift.
"""
from business_app.utils.error_handlers import ExceptionMapper
from business_app.utils.exceptions import WaterBusinessException
from shared.api_errors import RAW_EXCEPTION_ERROR_TYPES


def _is_auth_library(exc_class) -> bool:
    return exc_class.__module__.split(".")[0] in {"flask_jwt_extended", "jwt"}


def test_raw_exception_types_match_the_mapper_exactly():
    raw_4xx = {
        error_type
        for exc_class, (status, error_type) in ExceptionMapper.EXCEPTION_MAPPING.items()
        if status < 500
        and not issubclass(exc_class, WaterBusinessException)
        and not _is_auth_library(exc_class)
    }
    assert raw_4xx == set(RAW_EXCEPTION_ERROR_TYPES)
