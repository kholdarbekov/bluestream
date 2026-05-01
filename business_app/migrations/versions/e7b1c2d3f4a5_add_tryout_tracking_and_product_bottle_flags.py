"""add tryout tracking and product bottle flags

Revision ID: e7b1c2d3f4a5
Revises: d4e6f8a1b2c3
Create Date: 2026-03-02 14:15:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "e7b1c2d3f4a5"
down_revision = "d4e6f8a1b2c3"
branch_labels = None
depends_on = None


tryout_status_enum = postgresql.ENUM(
    "draft",
    "scheduled",
    "active",
    "closed",
    "cancelled",
    name="tryout_status",
    create_type=False,
)
tryout_outcome_enum = postgresql.ENUM(
    "pending",
    "converted",
    "declined",
    name="tryout_outcome",
    create_type=False,
)
tryout_task_type_enum = postgresql.ENUM(
    "handoff",
    "pickup",
    name="tryout_task_type",
    create_type=False,
)
tryout_task_status_enum = postgresql.ENUM(
    "open",
    "assigned",
    "completed",
    "cancelled",
    name="tryout_task_status",
    create_type=False,
)
tryout_bottle_ledger_event_type_enum = postgresql.ENUM(
    "handoff",
    "pickup",
    "adjustment",
    "void",
    name="tryout_bottle_ledger_event_type",
    create_type=False,
)


def upgrade():
    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.add_column(sa.Column("is_tryout_eligible", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(
            sa.Column("tracks_returnable_bottles", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column(
                "returnable_bottles_per_unit", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"
            )
        )
        batch_op.create_index(batch_op.f("ix_products_is_tryout_eligible"), ["is_tryout_eligible"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_products_tracks_returnable_bottles"), ["tracks_returnable_bottles"], unique=False
        )

    bind = op.get_bind()
    tryout_status_enum.create(bind, checkfirst=True)
    tryout_outcome_enum.create(bind, checkfirst=True)
    tryout_task_type_enum.create(bind, checkfirst=True)
    tryout_task_status_enum.create(bind, checkfirst=True)
    tryout_bottle_ledger_event_type_enum.create(bind, checkfirst=True)

    op.create_table(
        "trial_contacts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=True),
        sa.Column("preferred_language", sa.String(length=5), nullable=False, server_default="uz"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("trial_contacts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_trial_contacts_phone"), ["phone"], unique=False)
        batch_op.create_index("idx_trial_contacts_phone", ["phone"], unique=False)
        batch_op.create_index("idx_trial_contacts_company", ["company_name"], unique=False)

    op.create_table(
        "trial_contact_addresses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("trial_contact_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=100), nullable=True),
        sa.Column("full_address", sa.Text(), nullable=False),
        sa.Column("district", sa.String(length=100), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True, server_default="Tashkent"),
        sa.Column("latitude", sa.Numeric(precision=10, scale=7), nullable=True),
        sa.Column("longitude", sa.Numeric(precision=10, scale=7), nullable=True),
        sa.Column("delivery_notes", sa.Text(), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["trial_contact_id"], ["trial_contacts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("trial_contact_addresses", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_trial_contact_addresses_trial_contact_id"), ["trial_contact_id"], unique=False
        )
        batch_op.create_index("idx_trial_contact_addresses_contact", ["trial_contact_id"], unique=False)

    op.create_table(
        "product_tryouts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tryout_number", sa.String(length=50), nullable=True),
        sa.Column("trial_contact_id", sa.Integer(), nullable=False),
        sa.Column("converted_user_id", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("status", tryout_status_enum, nullable=False, server_default="draft"),
        sa.Column("outcome", tryout_outcome_enum, nullable=False, server_default="pending"),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="admin"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("internal_notes", sa.Text(), nullable=True),
        sa.Column("address_snapshot", sa.JSON(), nullable=False),
        sa.Column("handoff_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("return_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("converted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["converted_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["trial_contact_id"], ["trial_contacts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tryout_number", name="uq_product_tryouts_number"),
    )
    with op.batch_alter_table("product_tryouts", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_product_tryouts_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_product_tryouts_return_due_at"), ["return_due_at"], unique=False)
        batch_op.create_index(batch_op.f("ix_product_tryouts_tryout_number"), ["tryout_number"], unique=False)
        batch_op.create_index(batch_op.f("ix_product_tryouts_trial_contact_id"), ["trial_contact_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_product_tryouts_converted_user_id"), ["converted_user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_product_tryouts_created_by_user_id"), ["created_by_user_id"], unique=False)
        batch_op.create_index("idx_product_tryouts_status_due", ["status", "return_due_at"], unique=False)
        batch_op.create_index("idx_product_tryouts_contact", ["trial_contact_id"], unique=False)

    op.create_table(
        "product_tryout_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tryout_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_snapshot", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
        sa.Column("returnable_bottles_due", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["tryout_id"], ["product_tryouts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("product_tryout_items", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_product_tryout_items_tryout_id"), ["tryout_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_product_tryout_items_product_id"), ["product_id"], unique=False)
        batch_op.create_index("idx_product_tryout_items_tryout", ["tryout_id"], unique=False)
        batch_op.create_index("idx_product_tryout_items_product", ["product_id"], unique=False)

    op.create_table(
        "tryout_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tryout_id", sa.Integer(), nullable=False),
        sa.Column("task_type", tryout_task_type_enum, nullable=False),
        sa.Column("status", tryout_task_status_enum, nullable=False, server_default="open"),
        sa.Column("assigned_driver_user_id", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.Integer(), nullable=True),
        sa.Column("completed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("completion_payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assigned_driver_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["completed_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["tryout_id"], ["product_tryouts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("tryout_tasks", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tryout_tasks_tryout_id"), ["tryout_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tryout_tasks_task_type"), ["task_type"], unique=False)
        batch_op.create_index(batch_op.f("ix_tryout_tasks_status"), ["status"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_tryout_tasks_assigned_driver_user_id"), ["assigned_driver_user_id"], unique=False
        )
        batch_op.create_index("idx_tryout_tasks_tryout_status", ["tryout_id", "status"], unique=False)
        batch_op.create_index("idx_tryout_tasks_driver_status", ["assigned_driver_user_id", "status"], unique=False)

    op.create_table(
        "tryout_bottle_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tryout_id", sa.Integer(), nullable=False),
        sa.Column("tryout_item_id", sa.Integer(), nullable=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("event_type", tryout_bottle_ledger_event_type_enum, nullable=False),
        sa.Column("units", sa.Numeric(precision=12, scale=2), nullable=False, server_default="0.00"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("entry_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["tryout_tasks.id"]),
        sa.ForeignKeyConstraint(["tryout_id"], ["product_tryouts.id"]),
        sa.ForeignKeyConstraint(["tryout_item_id"], ["product_tryout_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_tryout_bottle_ledger_idempotency"),
    )
    with op.batch_alter_table("tryout_bottle_ledger", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_tryout_bottle_ledger_tryout_id"), ["tryout_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tryout_bottle_ledger_tryout_item_id"), ["tryout_item_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tryout_bottle_ledger_product_id"), ["product_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tryout_bottle_ledger_task_id"), ["task_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tryout_bottle_ledger_actor_user_id"), ["actor_user_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_tryout_bottle_ledger_event_type"), ["event_type"], unique=False)
        batch_op.create_index("idx_tryout_bottle_ledger_tryout_created", ["tryout_id", "created_at"], unique=False)
        batch_op.create_index("idx_tryout_bottle_ledger_task_event", ["task_id", "event_type"], unique=False)
        batch_op.create_index("idx_tryout_bottle_ledger_product_event", ["product_id", "event_type"], unique=False)


def downgrade():
    with op.batch_alter_table("tryout_bottle_ledger", schema=None) as batch_op:
        batch_op.drop_index("idx_tryout_bottle_ledger_product_event")
        batch_op.drop_index("idx_tryout_bottle_ledger_task_event")
        batch_op.drop_index("idx_tryout_bottle_ledger_tryout_created")
        batch_op.drop_index(batch_op.f("ix_tryout_bottle_ledger_event_type"))
        batch_op.drop_index(batch_op.f("ix_tryout_bottle_ledger_actor_user_id"))
        batch_op.drop_index(batch_op.f("ix_tryout_bottle_ledger_task_id"))
        batch_op.drop_index(batch_op.f("ix_tryout_bottle_ledger_product_id"))
        batch_op.drop_index(batch_op.f("ix_tryout_bottle_ledger_tryout_item_id"))
        batch_op.drop_index(batch_op.f("ix_tryout_bottle_ledger_tryout_id"))
    op.drop_table("tryout_bottle_ledger")

    with op.batch_alter_table("tryout_tasks", schema=None) as batch_op:
        batch_op.drop_index("idx_tryout_tasks_driver_status")
        batch_op.drop_index("idx_tryout_tasks_tryout_status")
        batch_op.drop_index(batch_op.f("ix_tryout_tasks_assigned_driver_user_id"))
        batch_op.drop_index(batch_op.f("ix_tryout_tasks_status"))
        batch_op.drop_index(batch_op.f("ix_tryout_tasks_task_type"))
        batch_op.drop_index(batch_op.f("ix_tryout_tasks_tryout_id"))
    op.drop_table("tryout_tasks")

    with op.batch_alter_table("product_tryout_items", schema=None) as batch_op:
        batch_op.drop_index("idx_product_tryout_items_product")
        batch_op.drop_index("idx_product_tryout_items_tryout")
        batch_op.drop_index(batch_op.f("ix_product_tryout_items_product_id"))
        batch_op.drop_index(batch_op.f("ix_product_tryout_items_tryout_id"))
    op.drop_table("product_tryout_items")

    with op.batch_alter_table("product_tryouts", schema=None) as batch_op:
        batch_op.drop_index("idx_product_tryouts_contact")
        batch_op.drop_index("idx_product_tryouts_status_due")
        batch_op.drop_index(batch_op.f("ix_product_tryouts_created_by_user_id"))
        batch_op.drop_index(batch_op.f("ix_product_tryouts_converted_user_id"))
        batch_op.drop_index(batch_op.f("ix_product_tryouts_trial_contact_id"))
        batch_op.drop_index(batch_op.f("ix_product_tryouts_tryout_number"))
        batch_op.drop_index(batch_op.f("ix_product_tryouts_return_due_at"))
        batch_op.drop_index(batch_op.f("ix_product_tryouts_status"))
    op.drop_table("product_tryouts")

    with op.batch_alter_table("trial_contact_addresses", schema=None) as batch_op:
        batch_op.drop_index("idx_trial_contact_addresses_contact")
        batch_op.drop_index(batch_op.f("ix_trial_contact_addresses_trial_contact_id"))
    op.drop_table("trial_contact_addresses")

    with op.batch_alter_table("trial_contacts", schema=None) as batch_op:
        batch_op.drop_index("idx_trial_contacts_company")
        batch_op.drop_index("idx_trial_contacts_phone")
        batch_op.drop_index(batch_op.f("ix_trial_contacts_phone"))
    op.drop_table("trial_contacts")

    tryout_bottle_ledger_event_type_enum.drop(op.get_bind(), checkfirst=True)
    tryout_task_status_enum.drop(op.get_bind(), checkfirst=True)
    tryout_task_type_enum.drop(op.get_bind(), checkfirst=True)
    tryout_outcome_enum.drop(op.get_bind(), checkfirst=True)
    tryout_status_enum.drop(op.get_bind(), checkfirst=True)

    with op.batch_alter_table("products", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_products_tracks_returnable_bottles"))
        batch_op.drop_index(batch_op.f("ix_products_is_tryout_eligible"))
        batch_op.drop_column("returnable_bottles_per_unit")
        batch_op.drop_column("tracks_returnable_bottles")
        batch_op.drop_column("is_tryout_eligible")
