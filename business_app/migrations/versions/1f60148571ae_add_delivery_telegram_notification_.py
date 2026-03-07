"""add delivery telegram notification preference override

Revision ID: 1f60148571ae
Revises: 62f836d8701b
Create Date: 2026-03-05 23:54:49.066777

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '1f60148571ae'
down_revision = '62f836d8701b'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        sa.text(
            """
            DELETE FROM notification_preferences
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT
                        id,
                        ROW_NUMBER() OVER (
                            PARTITION BY user_id, notification_type, channel
                            ORDER BY
                                updated_at DESC NULLS LAST,
                                created_at DESC NULLS LAST,
                                id DESC
                        ) AS row_rank
                    FROM notification_preferences
                ) ranked_preferences
                WHERE ranked_preferences.row_rank > 1
            )
            """
        )
    )

    with op.batch_alter_table('notification_preferences', schema=None) as batch_op:
        batch_op.create_unique_constraint('uq_notification_preferences_user_type_channel', ['user_id', 'notification_type', 'channel'])


def downgrade():
    with op.batch_alter_table('notification_preferences', schema=None) as batch_op:
        batch_op.drop_constraint('uq_notification_preferences_user_type_channel', type_='unique')
