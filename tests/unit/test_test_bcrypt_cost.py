"""The suite's bcrypt cost must stay at the fast test setting for a worker's whole life.

``setup_password_security`` swaps a PROCESS-WIDE hasher on every ``create_app()``
and reads the rounds from ``current_app``. A worker's first app is built with no
context active (env fallback: 4 rounds), but every Postgres-backed test builds
another app INSIDE the session app's context, where ``BCRYPT_ROUNDS`` used to be
the production 12 inherited from ``BaseConfig``. From the first such test on,
every fixture's ``hash_password`` cost ~230 ms instead of ~1 ms — the same tests
ran 7x slower late in a worker's life than early.
"""

from business_app import create_app
from business_app.utils.password_security import hash_password


def _cost(password_hash: str) -> int:
    return int(password_hash.split("$")[2])


def test_building_another_app_inside_an_app_context_keeps_the_test_bcrypt_cost(app):
    with app.app_context():
        create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})

    assert _cost(hash_password("TestPassword123!")) == 4
