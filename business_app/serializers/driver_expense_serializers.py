"""Driver expense query strings (spec 2026-11-17 §6.8). Request models only; `DriverExpenseReview` builds every
published field."""

from datetime import datetime
from typing import Literal, Optional, Tuple

from business_app.serializers.sales_serializers import PageQuery
from business_app.utils.local_windows import parse_date_range, window_bounds
from shared.staff_constants import DRIVER_EXPENSE_CATEGORIES, DRIVER_EXPENSE_STATUSES


class DriverExpenseListQuery(PageQuery):
    """`GET /admin/driver-expenses`'s query string, typed once (`model_validate(request.args.to_dict())`).

    `page`/`per_page` clamp through `PageQuery`. An unknown status or category is a pydantic error, which
    `handle_api_exception` answers with 400. No dates means no period: the whole history.
    """

    status: Literal[(*DRIVER_EXPENSE_STATUSES, "all")] = "pending"
    driver_id: Optional[int] = None
    category: Optional[Literal[DRIVER_EXPENSE_CATEGORIES]] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    def recorded_window(self) -> Optional[Tuple[datetime, datetime]]:
        """`[start, end)` in UTC for the Tashkent days the claims were recorded on, or None when no date was given.

        The dates parse through `parse_date_range` (uncapped, as the cash report), the one ISO parser: a bad date or
        an end before the start is its coded 400, and a half-given range fills the other end its way.
        """
        if not (self.start_date or "").strip() and not (self.end_date or "").strip():
            return None
        return window_bounds(*parse_date_range(self.start_date, self.end_date, capped=False))
