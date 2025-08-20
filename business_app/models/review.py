from datetime import datetime, timedelta
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum, JSON, Index
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.hybrid import hybrid_property
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
import uuid
from business_app import db
from business_app.models import TimestampMixin


class Review(db.Model, TimestampMixin):
    __tablename__ = 'reviews'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey('orders.id'), nullable=True, index=True)
    
    rating = Column(Integer, nullable=False)  # 1-5 stars
    title = Column(String(200), nullable=True)
    comment = Column(Text, nullable=True)
    
    # Review moderation
    is_approved = Column(Boolean, default=False)
    is_featured = Column(Boolean, default=False)
    moderator_notes = Column(Text, nullable=True)
    
    # Review metadata
    helpful_count = Column(Integer, default=0)
    photos = Column(JSON, default=[])
    
    user = relationship('User', back_populates='reviews')
    # Removed back_populates since Product model doesn't have reviews relationship
    # product = relationship('Product', back_populates='reviews')
    order = relationship('Order')
    
    def to_dict(self):
        return {
            'id': self.id,
            'rating': self.rating,
            'title': self.title,
            'comment': self.comment,
            'is_approved': self.is_approved,
            'is_featured': self.is_featured,
            'helpful_count': self.helpful_count,
            'photos': self.photos,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'user': {
                'id': self.user.id,
                'name': self.user.full_name
            } if self.user else None,
            'product': {
                'id': self.product.id,
                'name': self.product.name
            } if self.product else None
        }
