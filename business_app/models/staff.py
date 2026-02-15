"""
Staff-related models for the Water Business Platform.
Includes StaffActivityLog for tracking staff actions.
"""
from datetime import datetime, UTC
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON, Index
from business_app import db
from business_app.models import TimestampMixin


class StaffActivityLog(db.Model, TimestampMixin):
    """Track staff actions for auditing and analytics"""
    __tablename__ = 'staff_activity_log'
    __table_args__ = (
        Index('idx_staff_activity_user', 'user_id'),
        Index('idx_staff_activity_action', 'action'),
        Index('idx_staff_activity_created', 'created_at'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    action = Column(String(100), nullable=False)
    # Actions: 'delivery_accepted', 'delivery_status_updated', 'order_created',
    #          'user_created', 'order_preparing'
    entity_type = Column(String(50), nullable=True)  # 'order', 'delivery', 'user'
    entity_id = Column(Integer, nullable=True)
    metadata_ = Column('metadata', JSON, default=dict)  # Additional context (old_status, new_status, etc.)

    user = db.relationship('User', backref='staff_activity_logs')

    def __repr__(self):
        return f'<StaffActivityLog {self.id}: {self.action} by user {self.user_id}>'

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'action': self.action,
            'entity_type': self.entity_type,
            'entity_id': self.entity_id,
            'metadata': self.metadata_,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
