"""Move stranded `admin_ui` category translations into the served `ui` category

AdminUiTranslationService is the SSOT for which categories reach the admin UI:
`category == 'ui'` (the shared `common` namespace, fully-dotted `ui.*` keys) or
`category LIKE 'ui_%'` (scoped namespaces). The category `admin_ui` matches
neither, so every row filed under it was silently never served — the browser
never received the key at all.

The visible symptom was the Cancel button of every admin modal rendering the
literal string "ui.common.cancel", because `ui.common.cancel` (uz/ru/en) was
the only key sitting in that category while its sibling `ui.common.save` sat
correctly in `ui`.

Guarded on `key LIKE 'ui.%'` so only fully-dotted admin-UI keys — the ones the
`common` namespace is built from — are moved. Rows whose key already exists in
`ui` for the same language are deleted rather than moved, because
`translations` has no unique constraint on (key, language) and a duplicate pair
would leave which value wins up to row order.

Idempotent: re-running matches nothing once the category is drained.

Revision ID: a4c7e1b9d3f5
Revises: b6c4e8a2f7d1
Create Date: 2026-08-14 19:52:00.000000

"""

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "a4c7e1b9d3f5"
down_revision = "b6c4e8a2f7d1"
branch_labels = None
depends_on = None

_STRANDED_CATEGORY = "admin_ui"
_SERVED_CATEGORY = "ui"


def upgrade():
    bind = op.get_bind()

    # Drop would-be duplicates first: an identical (key, language) already
    # living in the served category makes the stranded row redundant.
    bind.execute(
        text(
            """
            DELETE FROM translations AS stranded
            WHERE stranded.category = :stranded_category
              AND stranded.key LIKE 'ui.%'
              AND EXISTS (
                  SELECT 1 FROM translations AS served
                  WHERE served.key = stranded.key
                    AND served.language = stranded.language
                    AND served.category = :served_category
              )
            """
        ),
        {"stranded_category": _STRANDED_CATEGORY, "served_category": _SERVED_CATEGORY},
    )

    bind.execute(
        text(
            """
            UPDATE translations
            SET category = :served_category,
                updated_at = CURRENT_TIMESTAMP
            WHERE category = :stranded_category
              AND key LIKE 'ui.%'
            """
        ),
        {"stranded_category": _STRANDED_CATEGORY, "served_category": _SERVED_CATEGORY},
    )


def downgrade():
    # Data-only repair. Rows moved here are indistinguishable from keys that
    # have always lived in `ui`, so re-stranding by key would risk hiding a
    # translation that was never broken. The migrated rows are correct where
    # they now sit, so the downgrade is intentionally a no-op.
    pass
