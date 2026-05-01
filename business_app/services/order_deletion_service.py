"""
Hard-delete orders and all dependent rows discovered through foreign keys.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any, Dict, List, Sequence, Set, Tuple

from sqlalchemy import MetaData, delete, select, tuple_

from business_app import db

logger = logging.getLogger(__name__)

RowKey = Tuple[Any, ...]


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
