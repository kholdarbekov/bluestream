"""
Hard-delete orders and all dependent rows discovered through foreign keys.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from decimal import Decimal
from typing import Any, Dict, List, Sequence, Set, Tuple

from sqlalchemy import MetaData, delete, select, tuple_

from business_app import db

logger = logging.getLogger(__name__)

RowKey = Tuple[Any, ...]

BOTTLE_LEDGER_TABLE_NAME = "bottle_ledger"


class OrderDeletionService:
    """Delete an order and every dependent row by traversing FK relationships."""

    ROOT_TABLE_NAME = "orders"
    ROOT_ORDER_NUMBER_COLUMN = "order_number"

    def build_deletion_plan(self, order_number: str) -> Dict[str, Any]:
        """Build a deletion plan for one order number without mutating data."""
        if not str(order_number or "").strip():
            raise ValueError("order_number is required")

        metadata = self._reflect_metadata()
        root_table = metadata.tables.get(self.ROOT_TABLE_NAME)
        if root_table is None:
            raise RuntimeError(f"'{self.ROOT_TABLE_NAME}' table was not found")
        if self.ROOT_ORDER_NUMBER_COLUMN not in root_table.c:
            raise RuntimeError(f"'{self.ROOT_ORDER_NUMBER_COLUMN}' column is missing from '{self.ROOT_TABLE_NAME}'")

        root_row_keys = self._fetch_root_row_keys(root_table, order_number)
        if not root_row_keys:
            return {
                "found": False,
                "order_number": order_number,
                "order_ids": [],
                "rows_by_table": {},
                "deletion_order": [],
                "total_rows": 0,
                "_row_keys_by_table": {},
            }

        referenced_by = self._build_referenced_by_map(metadata)
        discovered_row_keys: Dict[str, Set[RowKey]] = defaultdict(set)
        discovered_row_keys[self.ROOT_TABLE_NAME].update(root_row_keys)

        queue: deque[str] = deque([self.ROOT_TABLE_NAME])
        while queue:
            parent_table_name = queue.popleft()
            parent_table = metadata.tables[parent_table_name]
            parent_row_keys = discovered_row_keys[parent_table_name]

            for fk_constraint in referenced_by.get(parent_table_name, []):
                child_table = fk_constraint.table
                child_table_name = child_table.name

                child_row_keys = self._fetch_child_row_keys(
                    parent_table=parent_table,
                    parent_row_keys=parent_row_keys,
                    fk_constraint=fk_constraint,
                )
                if not child_row_keys:
                    continue

                new_row_keys = child_row_keys - discovered_row_keys[child_table_name]
                if not new_row_keys:
                    continue

                discovered_row_keys[child_table_name].update(new_row_keys)
                queue.append(child_table_name)

        normalized_row_keys_by_table: Dict[str, List[RowKey]] = {
            table_name: sorted(row_keys) for table_name, row_keys in discovered_row_keys.items() if row_keys
        }
        rows_by_table = {table_name: len(row_keys) for table_name, row_keys in normalized_row_keys_by_table.items()}
        deletion_order = self._build_deletion_order(
            metadata=metadata,
            table_names_with_rows=set(normalized_row_keys_by_table.keys()),
        )

        return {
            "found": True,
            "order_number": order_number,
            "order_ids": [row_key[0] for row_key in normalized_row_keys_by_table[self.ROOT_TABLE_NAME]],
            "rows_by_table": rows_by_table,
            "deletion_order": deletion_order,
            "total_rows": sum(rows_by_table.values()),
            "_row_keys_by_table": normalized_row_keys_by_table,
        }

    def execute_deletion_plan(self, plan: Dict[str, Any]) -> Dict[str, int]:
        """Execute a previously built deletion plan."""
        row_keys_by_table: Dict[str, List[RowKey]] = plan.get("_row_keys_by_table") or {}
        deletion_order: List[str] = plan.get("deletion_order") or []
        if not row_keys_by_table or not deletion_order:
            return {}

        metadata = self._reflect_metadata()
        deleted_rows_by_table: Dict[str, int] = {}

        try:
            # HERE, not in `delete_order_by_number`: this is the only method every
            # caller goes through — `scripts/delete_order_by_number.py` builds a
            # plan and executes it directly, so a fence placed one level up would
            # be skipped by the exact path an operator actually uses.
            plan = self._plan_with_bottle_ledger_reversed(plan)
            row_keys_by_table = plan["_row_keys_by_table"]
            deletion_order = plan["deletion_order"]

            for table_name in deletion_order:
                row_keys = row_keys_by_table.get(table_name) or []
                if not row_keys:
                    continue

                table = metadata.tables[table_name]
                pk_columns = self._primary_key_columns(table)
                where_condition = self._build_key_condition(pk_columns, row_keys)
                result = db.session.execute(delete(table).where(where_condition))
                deleted_rows_by_table[table_name] = (
                    int(result.rowcount) if result.rowcount is not None and result.rowcount >= 0 else len(row_keys)
                )

            db.session.commit()
            return deleted_rows_by_table

        except Exception:
            db.session.rollback()
            raise

    def delete_order_by_number(self, order_number: str, *, apply_changes: bool = False) -> Dict[str, Any]:
        """Build plan and optionally execute it."""
        plan = self.build_deletion_plan(order_number)
        if not plan["found"]:
            return {
                "found": False,
                "applied": False,
                "order_number": order_number,
                "order_ids": [],
                "rows_by_table": {},
                "deletion_order": [],
                "total_rows": 0,
                "deleted_rows_by_table": {},
                "deleted_total_rows": 0,
            }

        if not apply_changes:
            return {
                "found": True,
                "applied": False,
                "order_number": plan["order_number"],
                "order_ids": plan["order_ids"],
                "rows_by_table": plan["rows_by_table"],
                "deletion_order": plan["deletion_order"],
                "total_rows": plan["total_rows"],
                "deleted_rows_by_table": {},
                "deleted_total_rows": 0,
            }

        # The bottle-ledger reversal is NOT done here — see
        # `execute_deletion_plan`, the one method every caller shares. The
        # `rows_by_table`/`total_rows` reported below therefore describe the plan
        # as PREVIEWED; `deleted_rows_by_table` describes what was actually
        # removed, reversal entries included.
        deleted_rows_by_table = self.execute_deletion_plan(plan)
        return {
            "found": True,
            "applied": True,
            "order_number": plan["order_number"],
            "order_ids": plan["order_ids"],
            "rows_by_table": plan["rows_by_table"],
            "deletion_order": plan["deletion_order"],
            "total_rows": plan["total_rows"],
            "deleted_rows_by_table": deleted_rows_by_table,
            "deleted_total_rows": sum(deleted_rows_by_table.values()),
        }

    def _plan_with_bottle_ledger_reversed(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Reverse the plan's bottle-ledger rows, then rebuild the plan around them.

        THE ORDER OWNS ITS MONEY ROWS; IT DOES NOT OWN THE PLACE'S BOTTLES.

        The plan is reflection-driven, so ``bottle_ledger.order_id`` (and
        ``bottle_ledger.delivery_id``, via the order's delivery) make ledger rows
        children of the order and they disappear with it — while
        ``bottle_balances`` has no FK to ``orders`` and is left holding a figure
        nothing on the ledger supports any more. That is a permanent
        stored-vs-ledger divergence created OUTSIDE the coupled/decoupled
        discipline entirely, and ``POST /admin/bottles/reconcile/<addr>`` then
        "repairs" it by overwriting the customer's real balance with a figure
        derived from a ledger the deletion just truncated.

        So before anything is deleted, those rows are REVERSED through the normal
        write funnel: the stored balance moves with them, under the place lock,
        as an audited entry. The plan is then REBUILT so the reversal entries —
        which carry the same ``order_id``/``delivery_id`` as the rows they cancel
        — are swept away together with them, leaving stored and ledger equal on
        every scope the order touched.

        Returns the plan to execute: the rebuilt one when anything was reversed,
        otherwise the plan as given.
        """
        row_keys_by_table: Dict[str, List[RowKey]] = plan.get("_row_keys_by_table") or {}
        ledger_row_keys = row_keys_by_table.get(BOTTLE_LEDGER_TABLE_NAME) or []
        if not ledger_row_keys:
            return plan

        reversed_entry_ids = self._reverse_bottle_ledger_rows([row_key[0] for row_key in ledger_row_keys])
        if not reversed_entry_ids:
            return plan

        rebuilt = self.build_deletion_plan(plan["order_number"])
        still_unreversed = set(reversed_entry_ids) - {
            row_key[0] for row_key in (rebuilt.get("_row_keys_by_table") or {}).get(BOTTLE_LEDGER_TABLE_NAME, [])
        }
        if still_unreversed:
            # A reversal that outlives the rows it cancels IS the drift, sign
            # flipped. Refuse rather than delete half of a pair.
            raise RuntimeError(
                f"Bottle-ledger reversal entries {sorted(still_unreversed)} are not "
                "part of the rebuilt deletion plan; deleting now would leave the "
                "place's stored balance and its ledger permanently divergent"
            )
        return rebuilt

    def _reverse_bottle_ledger_rows(self, ledger_row_ids: Sequence[int]) -> List[int]:
        """Undo the balance effect of every bottle ledger row the plan will delete.

        Driven off the PLAN's own row ids rather than off ``order_id``: the
        reflection reaches ``bottle_ledger`` through two different FKs
        (``order_id`` directly, and ``delivery_id`` via the order's delivery), and
        a row swept in through the second one moves the place's balance exactly
        as much as one swept in through the first.

        One reversal per (order_id, delivery_id, FROZEN scope, attribution
        address). Two properties are load-bearing:

        * The reversal is stamped with the SAME ``order_id``/``delivery_id`` as
          the rows it cancels, so whichever FK made the originals children of the
          order makes the reversal a child too, and the rebuilt plan sweeps the
          pair away together. A reversal that outlived its originals would be the
          very drift this exists to prevent, with the sign flipped.
        * The entries are booked to the scope the ORIGINAL rows are stamped to,
          not to whatever place the address maps to today, so the ``+n`` and the
          ``-n`` of one physical handover always land in the same ledger. This is
          the same freezing policy ``OrderEditService._frozen_bottle_scope``
          applies to a post-delivery correction, and ``bottle_fines`` has always
          applied.

        Written through ``_create_ledger_entry`` — the shared funnel — so rung 1
        of the place lock ladder is taken (``resolve_scope_for_write``), the
        balance row moves under its own ``FOR UPDATE``, and the reversal is an
        auditable ledger row rather than a silent UPDATE. Nothing here commits:
        the reversal and the deletion share the caller's transaction.

        Returns the ids of the reversal entries created.
        """
        from business_app.models.bottle import BottleLedger
        from business_app.services.bottle_scope import BottleScope
        from business_app.services.bottle_tracking_service import BottleTrackingService
        from business_app.utils.exceptions import ValidationError
        from shared.enums import BottleLedgerEventType

        row_ids = [int(row_id) for row_id in ledger_row_ids]
        if not row_ids:
            return []

        rows = BottleLedger.query.filter(BottleLedger.id.in_(row_ids)).order_by(BottleLedger.id.asc()).all()
        if not rows:
            return []

        # (order_id, delivery_id, address_group_id, address_id) -> [net qty, user_id]
        buckets: Dict[Tuple[Any, ...], List[Any]] = {}
        for row in rows:
            key = (row.order_id, row.delivery_id, row.address_group_id, row.address_id)
            bucket = buckets.setdefault(key, [Decimal("0.00"), row.user_id])
            bucket[0] += Decimal(str(row.quantity or 0))

        bottle_service = BottleTrackingService()
        reversal_entry_ids: List[int] = []

        # Sorted on a None-safe key: the buckets are keyed by nullable FKs, and
        # `sorted` on raw tuples raises as soon as one row carries `order_id=None`.
        for key in sorted(buckets, key=lambda k: tuple(-1 if part is None else part for part in k)):
            order_id, delivery_id, address_group_id, address_id = key
            net_quantity, user_id = buckets[key]
            if net_quantity == 0:
                continue

            # Rung 1 on the address the entries are attributed to, then write to
            # the FROZEN scope those entries carry.
            bottle_service.resolve_scope_for_write(address_id)
            if address_group_id is None:
                scope = BottleScope.for_address(address_id)
            else:
                scope = BottleScope.for_group(address_group_id)

            reference = f"Order #{order_id}" if order_id is not None else f"Delivery #{delivery_id}"
            try:
                entry = bottle_service._create_ledger_entry(
                    user_id=user_id,
                    address_id=address_id,
                    event_type=BottleLedgerEventType.ADMIN_ADJUSTMENT,
                    quantity=-net_quantity,
                    order_id=order_id,
                    delivery_id=delivery_id,
                    notes=f"{reference} deleted; reversing its bottle ledger effect",
                    idempotency_key=(
                        f"order_delete_reversal:{order_id or 0}:{delivery_id or 0}:"
                        f"{address_group_id or 0}:{address_id}"
                    ),
                    scope=scope,
                    metadata={"source": "order_deletion", "reversed_quantity": str(net_quantity)},
                )
            except ValidationError:
                # `assert_reachable` refuses to mint a balance row for a place
                # with no members: the order was delivered to a group that has
                # since been dissolved and there is nowhere honest to book the
                # reversal. Refuse the deletion rather than drift the place.
                logger.exception(
                    "Refusing to delete %s: its bottle ledger cannot be reversed onto %s",
                    reference,
                    scope,
                )
                raise

            reversal_entry_ids.append(entry.id)

        return reversal_entry_ids

    def _reflect_metadata(self) -> MetaData:
        metadata = MetaData()
        metadata.reflect(bind=db.engine)
        return metadata

    def _fetch_root_row_keys(self, root_table, order_number: str) -> Set[RowKey]:
        pk_columns = self._primary_key_columns(root_table)
        stmt = select(*pk_columns).where(root_table.c[self.ROOT_ORDER_NUMBER_COLUMN] == order_number)
        rows = db.session.execute(stmt).all()
        return {tuple(row) for row in rows}

    def _fetch_child_row_keys(self, *, parent_table, parent_row_keys: Set[RowKey], fk_constraint) -> Set[RowKey]:
        child_table = fk_constraint.table
        child_pk_columns = self._primary_key_columns(child_table)

        local_columns = [element.parent for element in fk_constraint.elements]
        referenced_columns = [element.column for element in fk_constraint.elements]

        parent_fk_values = self._fetch_parent_fk_values(
            parent_table=parent_table,
            parent_row_keys=parent_row_keys,
            referenced_columns=referenced_columns,
        )
        if not parent_fk_values:
            return set()

        where_condition = self._build_key_condition(local_columns, parent_fk_values)
        stmt = select(*child_pk_columns).where(where_condition)
        rows = db.session.execute(stmt).all()
        return {tuple(row) for row in rows}

    def _fetch_parent_fk_values(
        self,
        *,
        parent_table,
        parent_row_keys: Set[RowKey],
        referenced_columns: Sequence,
    ) -> Set[RowKey]:
        if not parent_row_keys:
            return set()

        parent_pk_columns = self._primary_key_columns(parent_table)
        parent_where = self._build_key_condition(parent_pk_columns, parent_row_keys)
        stmt = select(*referenced_columns).where(parent_where)
        rows = db.session.execute(stmt).all()

        values: Set[RowKey] = set()
        for row in rows:
            value_tuple = tuple(row)
            if any(value is None for value in value_tuple):
                continue
            values.add(value_tuple)
        return values

    def _build_referenced_by_map(self, metadata: MetaData) -> Dict[str, List[Any]]:
        referenced_by: Dict[str, List[Any]] = defaultdict(list)
        for table in metadata.tables.values():
            for fk_constraint in table.foreign_key_constraints:
                parent_table_name = fk_constraint.referred_table.name
                referenced_by[parent_table_name].append(fk_constraint)
        return referenced_by

    def _build_deletion_order(self, *, metadata: MetaData, table_names_with_rows: Set[str]) -> List[str]:
        dependency_graph: Dict[str, Set[str]] = {table_name: set() for table_name in table_names_with_rows}
        in_degree: Dict[str, int] = {table_name: 0 for table_name in table_names_with_rows}

        for child_table_name in table_names_with_rows:
            child_table = metadata.tables[child_table_name]
            for fk_constraint in child_table.foreign_key_constraints:
                parent_table_name = fk_constraint.referred_table.name
                if parent_table_name not in table_names_with_rows or parent_table_name == child_table_name:
                    continue
                if child_table_name in dependency_graph[parent_table_name]:
                    continue

                dependency_graph[parent_table_name].add(child_table_name)
                in_degree[child_table_name] += 1

        queue: deque[str] = deque(sorted([name for name, degree in in_degree.items() if degree == 0]))
        topological_order: List[str] = []

        while queue:
            parent_table_name = queue.popleft()
            topological_order.append(parent_table_name)
            for child_table_name in sorted(dependency_graph[parent_table_name]):
                in_degree[child_table_name] -= 1
                if in_degree[child_table_name] == 0:
                    queue.append(child_table_name)

        if len(topological_order) != len(table_names_with_rows):
            remaining_tables = sorted(table_names_with_rows - set(topological_order))
            logger.warning(
                "Cycle detected while computing order-deletion plan. Falling back to table-name order for: %s",
                remaining_tables,
            )
            topological_order.extend(remaining_tables)

        deletion_order = list(reversed(topological_order))
        if self.ROOT_TABLE_NAME in deletion_order:
            deletion_order = [name for name in deletion_order if name != self.ROOT_TABLE_NAME] + [self.ROOT_TABLE_NAME]
        return deletion_order

    def _primary_key_columns(self, table) -> List[Any]:
        pk_columns = list(table.primary_key.columns)
        if not pk_columns:
            raise RuntimeError(f"Table '{table.name}' has no primary key; cannot build deterministic deletion plan")
        return pk_columns

    def _build_key_condition(self, columns: Sequence, row_keys: Sequence[RowKey]):
        if not row_keys:
            raise ValueError("row_keys cannot be empty when building a key condition")
        if len(columns) == 1:
            return columns[0].in_([row_key[0] for row_key in row_keys])
        return tuple_(*columns).in_(list(row_keys))
