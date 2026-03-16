#!/usr/bin/env python3
"""Delete an order and all dependent relations by `order_number`."""

from __future__ import annotations

import argparse
import json

from business_app import create_app
from business_app.services.order_deletion_service import OrderDeletionService


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Hard-delete an order and all related rows by order_number.'
    )
    parser.add_argument(
        'order_number',
        help='Order number to delete (example: TG_000042_26)',
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Execute deletion. Without this flag, the script runs in dry-run mode.',
    )
    parser.add_argument(
        '--yes',
        action='store_true',
        help='Skip interactive confirmation when --apply is used.',
    )
    return parser


def _print_result(result: dict) -> None:
    printable = {k: v for k, v in result.items() if not k.startswith('_')}
    print(json.dumps(printable, indent=2, default=str))


def main() -> int:
    args = _build_parser().parse_args()

    app = create_app()
    with app.app_context():
        service = OrderDeletionService()
        plan = service.build_deletion_plan(args.order_number)

        if not plan['found']:
            _print_result(
                {
                    'found': False,
                    'applied': False,
                    'order_number': args.order_number,
                    'message': 'Order was not found.',
                }
            )
            return 1

        preview = {
            'found': True,
            'applied': False,
            'order_number': plan['order_number'],
            'order_ids': plan['order_ids'],
            'rows_by_table': plan['rows_by_table'],
            'deletion_order': plan['deletion_order'],
            'total_rows': plan['total_rows'],
        }

        if not args.apply:
            _print_result(preview)
            return 0

        if not args.yes:
            confirmation = input(
                f"Type DELETE to permanently remove order '{args.order_number}' and related records: "
            ).strip()
            if confirmation != 'DELETE':
                print('Aborted. No data was deleted.')
                return 0

        deleted_rows_by_table = service.execute_deletion_plan(plan)
        _print_result(
            {
                **preview,
                'applied': True,
                'deleted_rows_by_table': deleted_rows_by_table,
                'deleted_total_rows': sum(deleted_rows_by_table.values()),
            }
        )
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
