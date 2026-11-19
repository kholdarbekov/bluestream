"""The receipt photo of a driver cash expense, streamed from Telegram through the STAFF bot (spec 2026-11-17 §6.8).

The file id is read from the claim's row, never from the request, so the bot token can only ever fetch a file the
claim recorded. A photo Telegram can no longer serve (a dead file id, no token, an outage) is a 404 the admin page
draws as "Receipt unavailable", never a 500. Same pattern as the visit photos (`VisitService.stream_photo`).
"""

from flask import Response, current_app

from business_app import db
from business_app.models.driver_expense import DriverCashExpense
from business_app.services.telegram_file_proxy import TelegramFileProxy
from business_app.utils.exceptions import AttachmentUnavailableError, NotFoundError
from business_app.utils.telegram_tokens import get_staff_bot_token


def stream_receipt(expense_id: int) -> Response:
    expense = db.session.get(DriverCashExpense, expense_id)
    if expense is None:
        raise NotFoundError("Driver expense not found", error_code="DRIVER_EXPENSE_NOT_FOUND")
    if expense.receipt_file_id is None:
        raise NotFoundError("This expense has no receipt", error_code="DRIVER_EXPENSE_NO_RECEIPT")
    proxy = TelegramFileProxy(get_staff_bot_token(), cache_namespace="driver_expense_receipt")
    try:
        return proxy.stream(
            expense.receipt_file_id,
            mime="image/jpeg",
            filename=f"expense_{expense.id}_receipt.jpg",
        )
    except AttachmentUnavailableError as exc:
        # str(exc) is already scrubbed of the token by the proxy.
        current_app.logger.warning("Driver expense %s receipt unavailable: %s", expense.id, exc)
        raise NotFoundError("Receipt is no longer available", error_code="DRIVER_EXPENSE_RECEIPT_UNAVAILABLE") from exc
