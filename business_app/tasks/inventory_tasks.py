"""
Inventory management tasks for the BlueStream platform
"""

import logging
from datetime import datetime, timezone

from celery import shared_task

from business_app.services.inventory_service import get_inventory_service
from business_app.models.product import Product
from business_app.models.user import User
from business_app.services.notification_service import NotificationService
from business_app.utils.audit_logger import audit_logger, AuditEventType, AuditSeverity
from business_app.utils.helpers import get_current_language
from shared.enums import UserRole, UserStatus

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def cleanup_expired_inventory_reservations(self):
    """
    Clean up expired inventory reservations
    This should be run every 15 minutes
    """
    try:
        logger.info("Starting cleanup of expired inventory reservations")

        result = get_inventory_service().cleanup_expired_reservations()

        if result["success"]:
            cleaned_count = result.get("cleaned_count", 0)
            logger.info(f"Successfully cleaned up {cleaned_count} expired inventory reservations")

            # Log cleanup activity
            audit_logger.log_event(
                event_type=AuditEventType.SYSTEM_MAINTENANCE,
                action="inventory_reservations_cleanup",
                severity=AuditSeverity.LOW,
                resource_type="inventory_system",
                description=f"Cleaned up {cleaned_count} expired inventory reservations",
                additional_data={"cleaned_count": cleaned_count},
            )

            return {"success": True, "cleaned_count": cleaned_count}
        else:
            logger.error(f"Failed to cleanup expired reservations: {result.get('reason')}")
            return {"success": False, "error": result.get("reason")}

    except Exception as exc:
        logger.error(f"Cleanup expired reservations task failed: {exc}")
        raise self.retry(exc=exc, countdown=60)


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def send_low_stock_alert_task(self, product_id: int):
    """
    Send low stock alert for a product
    """
    try:
        logger.info(f"Sending low stock alert for product {product_id}")

        product = Product.query.get(product_id)
        if not product:
            logger.error(f"Product {product_id} not found for low stock alert")
            return {"success": False, "error": "Product not found"}

        # Get inventory status
        inventory_status = get_inventory_service().get_inventory_status(product_id)

        # Prepare notification data
        language = get_current_language()
        notification_data = {
            "product_id": product.id,
            "product_name": product.get_translated("name", language),
            "sku": product.sku,
            "current_stock": inventory_status["current_stock"],
            "available_quantity": inventory_status["available_quantity"],
            "min_stock_level": inventory_status["min_stock_level"],
            "is_out_of_stock": inventory_status["is_out_of_stock"],
        }

        # Send notification to administrators/managers across their preferred
        # channels (email/telegram/etc, per NotificationService.send_notification
        # -- the only real dispatch entry point; there is no raw
        # recipient_email/chat_id API on NotificationService).
        notification_service = NotificationService()
        admin_users = User.query.filter(
            User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.status == UserStatus.ACTIVE
        ).all()

        for admin in admin_users:
            try:
                notification_service.send_notification(
                    admin.id,
                    "low_stock_alert",
                    template_data=notification_data,
                )
            except Exception as e:
                logger.error(f"Failed to send low stock alert to admin {admin.id}: {e}")

        # Log the alert
        audit_logger.log_event(
            event_type=AuditEventType.INVENTORY_UPDATED,
            action="low_stock_alert_sent",
            severity=AuditSeverity.MEDIUM,
            resource_type="product_inventory",
            resource_id=str(product_id),
            description=f"Low stock alert sent for {product.get_translated('name', language)}",
            additional_data=notification_data,
        )

        logger.info(f"Low stock alert sent for product {product_id}")
        return {"success": True, "product_id": product_id}

    except Exception as exc:
        logger.error(f"Low stock alert task failed for product {product_id}: {exc}")
        raise self.retry(exc=exc, countdown=120)


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def generate_inventory_report_task(self, report_type: str = "daily"):
    """
    Generate inventory report
    """
    try:
        logger.info(f"Generating {report_type} inventory report")

        # Get all active products
        products = Product.query.filter_by(is_active=True).all()

        low_stock_products = []
        out_of_stock_products = []
        total_inventory_value = 0

        language = get_current_language()
        for product in products:
            try:
                inventory_status = get_inventory_service().get_inventory_status(product.id)

                if inventory_status["is_out_of_stock"]:
                    out_of_stock_products.append(
                        {
                            "id": product.id,
                            "name": product.get_translated("name", language),
                            "sku": product.sku,
                            "stock": inventory_status["current_stock"],
                        }
                    )
                elif inventory_status["is_low_stock"]:
                    low_stock_products.append(
                        {
                            "id": product.id,
                            "name": product.get_translated("name", language),
                            "sku": product.sku,
                            "stock": inventory_status["current_stock"],
                            "min_level": inventory_status["min_stock_level"],
                        }
                    )

                # Calculate inventory value
                if product.base_price and inventory_status["current_stock"]:
                    total_inventory_value += float(product.base_price) * inventory_status["current_stock"]

            except Exception as e:
                logger.error(f"Error processing product {product.id} for inventory report: {e}")
                continue

        # Prepare report data
        report_data = {
            "report_type": report_type,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_products": len(products),
            "low_stock_count": len(low_stock_products),
            "out_of_stock_count": len(out_of_stock_products),
            "total_inventory_value": total_inventory_value,
            "low_stock_products": low_stock_products[:10],  # Top 10
            "out_of_stock_products": out_of_stock_products[:10],  # Top 10
        }

        # Send report to administrators/managers via the real notification
        # dispatch API (NotificationService.send_notification) -- see
        # send_low_stock_alert_task above for why this replaced the
        # nonexistent send_email_notification/ADMIN_EMAILS path.
        admin_users = User.query.filter(
            User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.status == UserStatus.ACTIVE
        ).all()

        if admin_users:
            notification_service = NotificationService()

            for admin in admin_users:
                try:
                    notification_service.send_notification(
                        admin.id,
                        "inventory_report",
                        template_data=report_data,
                    )
                except Exception as e:
                    logger.error(f"Failed to send inventory report to admin {admin.id}: {e}")

        # Log report generation
        audit_logger.log_event(
            event_type=AuditEventType.DATA_EXPORT,
            action="inventory_report_generated",
            severity=AuditSeverity.MEDIUM,
            resource_type="inventory_system",
            description=f"Generated {report_type} inventory report",
            additional_data={
                "report_type": report_type,
                "total_products": len(products),
                "low_stock_count": len(low_stock_products),
                "out_of_stock_count": len(out_of_stock_products),
            },
        )

        logger.info(f"Successfully generated {report_type} inventory report")
        return {
            "success": True,
            "report_type": report_type,
            "summary": {
                "total_products": len(products),
                "low_stock_count": len(low_stock_products),
                "out_of_stock_count": len(out_of_stock_products),
                "total_inventory_value": total_inventory_value,
            },
        }

    except Exception as exc:
        logger.error(f"Inventory report generation failed: {exc}")
        raise self.retry(exc=exc, countdown=300)


@shared_task(bind=True, max_retries=3, time_limit=600, soft_time_limit=540)
def auto_reorder_products_task(self):
    """
    Automatically create reorder suggestions for products below minimum stock
    """
    try:
        logger.info("Checking for products that need reordering")

        # Get products that are below minimum stock level
        products_to_reorder = []
        language = get_current_language()

        products = Product.query.filter(
            Product.is_active == True, Product.min_stock_level.isnot(None), Product.max_stock_level.isnot(None)
        ).all()

        for product in products:
            try:
                inventory_status = get_inventory_service().get_inventory_status(product.id)

                # Check if product needs reordering
                if (
                    inventory_status["available_quantity"] <= product.min_stock_level
                    and product.max_stock_level > product.min_stock_level
                ):

                    suggested_quantity = product.max_stock_level - inventory_status["current_stock"]

                    products_to_reorder.append(
                        {
                            "product_id": product.id,
                            "product_name": product.get_translated("name", language),
                            "sku": product.sku,
                            "current_stock": inventory_status["current_stock"],
                            "available_quantity": inventory_status["available_quantity"],
                            "min_stock_level": product.min_stock_level,
                            "max_stock_level": product.max_stock_level,
                            "suggested_quantity": suggested_quantity,
                        }
                    )

            except Exception as e:
                logger.error(f"Error checking reorder for product {product.id}: {e}")
                continue

        if products_to_reorder:
            # Send reorder suggestions to administrators/managers via the
            # real notification dispatch API -- see send_low_stock_alert_task
            # above for why this replaced the nonexistent
            # send_email_notification/ADMIN_EMAILS path.
            admin_users = User.query.filter(
                User.role.in_([UserRole.ADMIN, UserRole.MANAGER]), User.status == UserStatus.ACTIVE
            ).all()

            if admin_users:
                notification_service = NotificationService()

                report_data = {
                    "products_to_reorder": products_to_reorder,
                    "total_products": len(products_to_reorder),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }

                for admin in admin_users:
                    try:
                        notification_service.send_notification(
                            admin.id,
                            "reorder_suggestions",
                            template_data=report_data,
                        )
                    except Exception as e:
                        logger.error(f"Failed to send reorder suggestions to admin {admin.id}: {e}")

            # Log reorder suggestions
            audit_logger.log_event(
                event_type=AuditEventType.INVENTORY_UPDATED,
                action="auto_reorder_suggestions_generated",
                severity=AuditSeverity.MEDIUM,
                resource_type="inventory_system",
                description=f"Generated reorder suggestions for {len(products_to_reorder)} products",
                additional_data={"products_count": len(products_to_reorder)},
            )

        logger.info(f"Auto-reorder check completed: {len(products_to_reorder)} products need reordering")
        return {
            "success": True,
            "products_to_reorder_count": len(products_to_reorder),
            "products_to_reorder": products_to_reorder[:5],  # Return first 5 for logging
        }

    except Exception as exc:
        logger.error(f"Auto-reorder check failed: {exc}")
        raise self.retry(exc=exc, countdown=600)
