from flask import request

_TRUE_VALUES = ("true", "1", "yes", "on")


def parse_bool_arg(name, default=None):
    """Parse a query-string boolean without Flask's broken ``type=bool``
    (``bool("false")`` is True). Returns ``default`` when the arg is absent;
    any present value is truthy only if it is one of true/1/yes/on (case-insensitive)."""
    raw = request.args.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES
