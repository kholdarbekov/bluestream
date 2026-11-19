"""`create_app` prints no setting it is given.

It printed "!!! CREATE_APP CALLED - app id: ..., config_class: {config_class}". Handed a dict (as tests, scripts and
older callers do), that printed every value: the Redis password in REDIS_URL, the database password, SECRET_KEY and
JWT_SECRET_KEY, into stdout, container logs and test-run logs. The line now names only the kind of config.
"""

from business_app import create_app

PASSWORD = "create-app-print-sentinel-password"
REDIS_URL = f"redis://:{PASSWORD}@redis:6379/15"
SECRET_KEY = "create-app-print-sentinel-secret-key"
JWT_SECRET_KEY = "create-app-print-sentinel-jwt-secret"


def test_create_app_with_a_dict_config_prints_no_value_of_it(capsys):
    app = create_app(
        {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": SECRET_KEY,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "SQLALCHEMY_TRACK_MODIFICATIONS": False,
            "JWT_SECRET_KEY": JWT_SECRET_KEY,
            "REDIS_URL": REDIS_URL,
            "CELERY_ALWAYS_EAGER": True,
        }
    )

    printed = capsys.readouterr()
    output = printed.out + printed.err
    assert "!!! CREATE_APP CALLED" in output
    for value in (PASSWORD, SECRET_KEY, JWT_SECRET_KEY):
        assert value not in output, "create_app printed a value of the config it was given"
    # ...and the app still gets every setting
    assert app.config["REDIS_URL"] == REDIS_URL
    assert app.config["SECRET_KEY"] == SECRET_KEY
