"""The settings the test apps are created with (tests/conftest.py, tests/integration/conftest.py).

Handed to `create_app` as a class, never a dict: `create_app` prints the config it is given ("!!! CREATE_APP CALLED ...
config_class: ..."), and a dict prints every value, so the Redis password in REDIS_URL and the Postgres one in
SQLALCHEMY_DATABASE_URI went into failing tests' output and logs/test-runs/*.log. A class prints only its name, and
`create_app` copies its attributes into `app.config` as it would a dict's keys.
"""


def _config_class(settings: dict) -> type:
    return type("PytestAppConfig", (), settings)


def sqlite_app_config(redis_url: str) -> type:
    """The session app: in-memory SQLite, on this worker's Redis database."""
    return _config_class({
        'TESTING': True,
        'WTF_CSRF_ENABLED': False,
        'SECRET_KEY': 'test-secret-key',
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'JWT_SECRET_KEY': 'test-jwt-secret',
        'REDIS_URL': redis_url,
        'CELERY_ALWAYS_EAGER': True,  # Run tasks synchronously in tests
    })


def postgres_app_config(database_url: str) -> type:
    """An app bound to a migrated Postgres database (`pg_app`, `migrated_pg_template`)."""
    return _config_class({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": database_url,
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "SECRET_KEY": "test-secret-key-for-pg-e2e-32-characters",
        "JWT_SECRET_KEY": "test-jwt-secret-key-for-pg-e2e",
        "CELERY_ALWAYS_EAGER": True,
        "WTF_CSRF_ENABLED": False,
    })
