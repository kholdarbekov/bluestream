"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Rollback strategy:
    <one or two sentences. What does ``downgrade()`` actually do? If the
    migration is forward-only (drops/renames a column you intend to keep
    dropped), say so explicitly. ARCH-012 — see business_app/migrations/CONVENTIONS.md.>

"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# revision identifiers, used by Alembic.
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade():
    ${upgrades if upgrades else "pass"}


def downgrade():
    ${downgrades if downgrades else "pass"}
