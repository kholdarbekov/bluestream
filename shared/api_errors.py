"""Error-body vocabulary shared by the backend and the bots."""

# `business_app.utils.error_handlers.ExceptionMapper` error types whose
# `message` is ``str()`` of a raw Python exception (ValueError, TypeError,
# KeyError), not a sentence written for a person. Clients never display those
# messages. Pinned to the mapper by tests/unit/test_api_error_types_contract.py.
RAW_EXCEPTION_ERROR_TYPES = frozenset({"INVALID_VALUE", "TYPE_ERROR", "MISSING_KEY"})
