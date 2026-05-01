"""
Base model with timezone-aware datetime handling
"""

from datetime import datetime
from sqlalchemy import Column, DateTime, Integer, inspect
from sqlalchemy.ext.hybrid import hybrid_property
from business_app import db
from business_app.utils.timezone_utils import get_utc_now, ensure_utc


class TimestampMixin:
    """Mixin class for automatic timestamp management with timezone awareness"""

    created_at = Column(DateTime(timezone=True), default=get_utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=get_utc_now, onupdate=get_utc_now, nullable=False)

    @hybrid_property
    def created_at_utc(self):
        """Ensure created_at is always in UTC"""
        return ensure_utc(self.created_at) if self.created_at else None

    @hybrid_property
    def updated_at_utc(self):
        """Ensure updated_at is always in UTC"""
        return ensure_utc(self.updated_at) if self.updated_at else None


class SoftDeleteMixin:
    """Mixin class for soft delete functionality with timezone awareness"""

    deleted_at = Column(DateTime(timezone=True), nullable=True)

    @hybrid_property
    def is_deleted(self):
        """Check if record is soft deleted"""
        return self.deleted_at is not None

    @hybrid_property
    def deleted_at_utc(self):
        """Ensure deleted_at is always in UTC"""
        return ensure_utc(self.deleted_at) if self.deleted_at else None

    def soft_delete(self):
        """Mark record as deleted"""
        self.deleted_at = get_utc_now()

    def restore(self):
        """Restore soft deleted record"""
        self.deleted_at = None


class BaseModel(db.Model, TimestampMixin):
    """Base model class with common functionality"""

    __abstract__ = True

    id = Column(Integer, primary_key=True)

    def to_dict(self, exclude_fields=None, include_relationships=False):
        """
        Convert model instance to dictionary with timezone-aware datetime handling

        Args:
            exclude_fields: List of fields to exclude
            include_relationships: Whether to include relationship data

        Returns:
            dict: Model data as dictionary
        """
        exclude_fields = exclude_fields or []
        result = {}

        # Get all columns
        mapper = inspect(self.__class__)

        for column in mapper.columns:
            if column.name in exclude_fields:
                continue

            value = getattr(self, column.name)

            # Handle datetime fields - ensure they're timezone-aware
            if isinstance(value, datetime):
                value = ensure_utc(value)
                # Convert to ISO format for JSON serialization
                result[column.name] = value.isoformat()
            else:
                result[column.name] = value

        # Include relationships if requested
        if include_relationships:
            for relationship in mapper.relationships:
                if relationship.key in exclude_fields:
                    continue

                related_obj = getattr(self, relationship.key)
                if related_obj is not None:
                    if hasattr(related_obj, "__iter__") and not isinstance(related_obj, str):
                        # One-to-many or many-to-many relationship
                        result[relationship.key] = [
                            obj.to_dict(exclude_fields=exclude_fields) if hasattr(obj, "to_dict") else str(obj)
                            for obj in related_obj
                        ]
                    else:
                        # One-to-one or many-to-one relationship
                        result[relationship.key] = (
                            related_obj.to_dict(exclude_fields=exclude_fields)
                            if hasattr(related_obj, "to_dict")
                            else str(related_obj)
                        )

        return result

    def update_from_dict(self, data, allowed_fields=None):
        """
        Update model instance from dictionary with timezone handling

        Args:
            data: Dictionary with new values
            allowed_fields: List of fields that can be updated
        """
        from business_app.utils.timezone_utils import parse_user_datetime

        if allowed_fields is None:
            # Get all column names except id, created_at, updated_at
            mapper = inspect(self.__class__)
            allowed_fields = [col.name for col in mapper.columns if col.name not in ["id", "created_at", "updated_at"]]

        for field, value in data.items():
            if field not in allowed_fields:
                continue

            if hasattr(self, field):
                # Handle datetime fields
                column = getattr(self.__class__, field, None)
                if column and hasattr(column.property, "columns"):
                    column_type = column.property.columns[0].type
                    if isinstance(column_type, DateTime) and isinstance(value, str):
                        try:
                            # Parse user datetime and convert to UTC
                            value = parse_user_datetime(value)
                        except (ValueError, TypeError):
                            # Skip invalid datetime values
                            continue

                setattr(self, field, value)

    def save(self):
        """Save model instance to database"""
        db.session.add(self)
        db.session.commit()
        return self

    def delete(self):
        """Delete model instance from database"""
        db.session.delete(self)
        db.session.commit()

    @classmethod
    def create(cls, **kwargs):
        """Create new instance with timezone handling"""
        instance = cls()
        instance.update_from_dict(kwargs)
        return instance.save()

    @classmethod
    def get_by_id(cls, id):
        """Get instance by ID"""
        return cls.query.get(id)

    @classmethod
    def get_or_404(cls, id):
        """Get instance by ID or raise 404"""
        return cls.query.get_or_404(id)

    def __repr__(self):
        return f"<{self.__class__.__name__} {self.id}>"


class AuditMixin:
    """Mixin for audit trail functionality"""

    created_by = Column(Integer, nullable=True)  # User ID who created the record
    updated_by = Column(Integer, nullable=True)  # User ID who last updated the record
    version = Column(Integer, default=1, nullable=False)  # Optimistic locking version

    def update_audit_fields(self, user_id=None):
        """Update audit fields before save"""
        from flask import g

        if user_id is None:
            user_id = getattr(g, "current_user_id", None)

        if self.id is None:  # New record
            self.created_by = user_id

        self.updated_by = user_id
        self.version = (self.version or 0) + 1


# Create timezone-aware DateTime column type
def UTCDateTime():
    """Create timezone-aware DateTime column that stores in UTC"""
    return Column(DateTime(timezone=True), default=get_utc_now)


# Helper functions for model queries with timezone handling
def filter_by_date_range(query, date_field, start_date=None, end_date=None, user_timezone=None):
    """
    Filter query by date range with timezone conversion

    Args:
        query: SQLAlchemy query object
        date_field: Model datetime field to filter on
        start_date: Start date (can be string or datetime)
        end_date: End date (can be string or datetime)
        user_timezone: User's timezone for date conversion

    Returns:
        Filtered query
    """
    from business_app.utils.timezone_utils import parse_user_datetime, local_to_utc

    if start_date:
        if isinstance(start_date, str):
            start_date = parse_user_datetime(start_date, source_tz=user_timezone)
        elif isinstance(start_date, datetime):
            start_date = local_to_utc(start_date, source_tz=user_timezone)

        query = query.filter(date_field >= start_date)

    if end_date:
        if isinstance(end_date, str):
            end_date = parse_user_datetime(end_date, source_tz=user_timezone)
        elif isinstance(end_date, datetime):
            end_date = local_to_utc(end_date, source_tz=user_timezone)

        # Add 23:59:59 to end_date if it's just a date
        if end_date.time() == datetime.min.time():
            end_date = end_date.replace(hour=23, minute=59, second=59, microsecond=999999)

        query = query.filter(date_field <= end_date)

    return query


def order_by_date(query, date_field, descending=True):
    """
    Order query by date field

    Args:
        query: SQLAlchemy query object
        date_field: Model datetime field to order by
        descending: Whether to order in descending order

    Returns:
        Ordered query
    """
    if descending:
        return query.order_by(date_field.desc())
    else:
        return query.order_by(date_field.asc())
