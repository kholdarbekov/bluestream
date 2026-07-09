"""Read-only verifier for Click's /payment/status enum (spec 2026-07-08, defect #7).

Prints the RAW payment_status code Click returns for a real payment, alongside
what the current code mapping and the documented enum would each conclude.
Makes NO database writes.

Run on the host against the running stack (scripts/ is not mounted into the
business_app container):

    docker compose exec -T business_app python - --order-number TG_000123_45 \
        < scripts/verify_click_payment_status_enum.py

Since `docker compose exec ... python - < script.py` cannot receive argv, an
env-var fallback is also supported:

    docker compose exec -T -e VERIFY_ORDER_NUMBER=TG_000123_45 business_app \
        python - < scripts/verify_click_payment_status_enum.py
"""

import argparse
import os
import sys


def main() -> int:
    if len(sys.argv) == 1 and (os.environ.get("VERIFY_PAYMENT_ID") or os.environ.get("VERIFY_ORDER_NUMBER")):
        sys.argv += (
            ["--payment-id", os.environ["VERIFY_PAYMENT_ID"]]
            if os.environ.get("VERIFY_PAYMENT_ID")
            else ["--order-number", os.environ["VERIFY_ORDER_NUMBER"]]
        )

    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--payment-id", type=int, help="payments.id of a Click payment to check")
    group.add_argument("--order-number", type=str, help="order_number of a Click-paid order")
    args = parser.parse_args()

    from business_app import create_app, db
    from business_app.models.order import Order
    from business_app.models.payment import Payment

    app = create_app()
    with app.app_context():
        if app.config.get("CLICK_TEST_MODE"):
            print("CLICK_TEST_MODE is enabled — this environment cannot verify the live enum.")
            return 2

        if args.payment_id:
            payment = Payment.query.get(args.payment_id)
        else:
            order = Order.query.filter_by(order_number=args.order_number).first()
            payment = Payment.query.filter_by(order_id=order.id).first() if order else None
        if payment is None:
            print("Payment not found.")
            return 1

        from business_app.services.click_payment_provider_service import ClickPaymentProviderService

        service = ClickPaymentProviderService()
        try:
            result = service.check_payment_status(payment)
        finally:
            db.session.rollback()  # read-only guarantee: discard any flushed state

        raw_code = result.get("payment_status_code")
        print(f"payment_id={payment.id} local_status={payment.status}")
        print(f"RAW Click payment_status code: {raw_code!r}")
        print(
            f"Current code mapping says:     {result.get('status')!r}  "
            "(2=completed; 0/1=pending; <0=cancelled/failed)"
        )
        docs_enum = {0: "created", 1: "processing", 2: "success"}
        try:
            docs_says = docs_enum.get(int(raw_code), "error(<0)" if int(raw_code) < 0 else "unknown")
        except (TypeError, ValueError):
            docs_says = "unparseable"
        print(f"Documented enum would say:     {docs_says!r}  (0=created, 1=processing, 2=success)")
        print("\nIf these disagree for a KNOWN-PAID payment, the mapping fix follow-up is GO.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
