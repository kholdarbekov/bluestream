"""Canonical SQL mirror of LoyaltyService.is_user_loyalty_eligible.

Used by the Telegram bot (asyncpg) for fast menu-time reads and by the backend
parity test. Keep this in lockstep with the Python rule in
business_app/services/loyalty_service.py — the parity test
(tests/integration/test_loyalty_eligibility_sql_parity.py) enforces agreement.

The expression expects the `users` table aliased as `u`.

CURRENT_TIMESTAMP is used (not now()) for SQLite/Postgres portability: the
parity test runs against the project's SQLite in-memory test DB, while the bot
runs against Postgres in production. CURRENT_TIMESTAMP is standard SQL
supported by both engines.
"""

LOYALTY_ELIGIBLE_SQL = """(
    u.user_type <> 'entity'
    OR EXISTS (
        SELECT 1 FROM corporate_contracts c
        WHERE c.user_id = u.id
          AND c.is_active IS TRUE
          AND c.status = 'active'
          AND c.is_loyalty_points_eligible IS TRUE
          AND (c.start_date IS NULL OR c.start_date <= CURRENT_TIMESTAMP)
          AND (c.end_date   IS NULL OR c.end_date   >= CURRENT_TIMESTAMP)
    )
)"""
