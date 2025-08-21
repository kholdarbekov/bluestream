from datetime import datetime, timedelta
from decimal import Decimal
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum, JSON, Index, Numeric
from sqlalchemy.orm import relationship, backref
from sqlalchemy.ext.hybrid import hybrid_property
from werkzeug.security import generate_password_hash, check_password_hash
from flask_sqlalchemy import SQLAlchemy
from business_app import db
from business_app.utils.constants import PriceRuleType
from business_app.models import TimestampMixin
import enum

class ProductCategoryEnum(enum.Enum):
    DRINKING_WATER = 'drinking_water'
    SPARKLING_WATER = 'sparkling_water'
    FLAVORED_WATER = 'flavored_water'
    ALKALINE_WATER = 'alkaline_water'
    DISTILLED_WATER = 'distilled_water'
    SPRING_WATER = 'spring_water'

class ProductSizeEnum(enum.Enum):
    SIZE_05L = '0.5L'
    SIZE_1L = '1L'
    SIZE_15L = '1.5L'
    SIZE_5L = '5L'
    SIZE_19L = '19L'


class ProductCategory(db.Model, TimestampMixin):
    __tablename__ = 'product_categories'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    name_ru = Column(String(100), nullable=True)
    name_en = Column(String(100), nullable=True)
    description = Column(Text, nullable=True)
    description_ru = Column(Text, nullable=True)
    description_en = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    icon_url = Column(String(255), nullable=True)
    
    def to_dict(self, language='uz'):
        return {
            'id': self.id,
            'name': self.name,
            'name_ru': self.name_ru,
            'name_en': self.name_en,
            'description': self.description,
            'description_ru': self.description_ru,
            'description_en': self.description_en,
            'is_active': self.is_active,
            'sort_order': self.sort_order,
            'icon_url': self.icon_url
        }

class Product(db.Model, TimestampMixin):
    __tablename__ = 'products'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    short_description = Column(String(500), nullable=True)
    sku = Column(String(100), nullable=True)
    
    # Pricing
    base_price = Column(Numeric(precision=10, scale=2), nullable=False)
    cost_price = Column(Numeric(precision=10, scale=2), nullable=True)
    discount_price = Column(Numeric(precision=10, scale=2), nullable=True)
    
    # Product details
    category_id = Column(Integer, ForeignKey('product_categories.id'), nullable=False)
    size = Column(Enum(ProductSizeEnum), nullable=False)
    volume = Column(Float, nullable=True)
    volume_unit = Column(String(10), default='L')
    weight = Column(Float, nullable=True)
    weight_unit = Column(String(10), default='kg')
    is_active = Column(Boolean, default=True)
    is_featured = Column(Boolean, default=False)
    requires_prescription = Column(Boolean, default=False)
    
    # Inventory
    track_inventory = Column(Boolean, default=True)
    stock_quantity = Column(Integer, default=0)
    min_stock_level = Column(Integer, default=0)
    max_stock_level = Column(Integer, default=1000)
    
    # Media and content
    images = Column(JSON, default=[])
    
    # Content
    nutrition_facts = Column(JSON, default={})
    ingredients = Column(Text, nullable=True)
    barcode = Column(String(100), nullable=True)
    
    # SEO and metadata
    slug = Column(String(255), nullable=True)
    meta_title = Column(String(200), nullable=True)
    meta_description = Column(Text, nullable=True)
    
    # Relationships
    category = relationship('ProductCategory', backref='products')
    
    def calculate_price(self, user=None, quantity=1):
        """Calculate dynamic price based on user and quantity"""
        # Use discount_price if available, otherwise use base_price
        final_price = self.discount_price if self.discount_price else self.base_price
        
        # TODO: Add price rules logic when price_rules relationship is properly configured
        # For now, just return the base or discounted price
        
        return max(final_price, 0)  # Ensure price is not negative
    
    def update_rating(self):
        """Update average rating based on reviews"""
        # TODO: Implement when reviews relationship is properly configured
        pass
        
    def to_dict(self, user=None, quantity=1):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'short_description': self.short_description,
            'sku': self.sku,
            'base_price': float(self.base_price) if self.base_price else 0,
            'current_price': float(self.calculate_price(user, quantity)),
            'cost_price': float(self.cost_price) if self.cost_price else None,
            'discount_price': float(self.discount_price) if self.discount_price else None,
            'category_id': self.category_id,
            'category': self.category.to_dict() if self.category else None,
            'size': self.size.value if self.size else None,
            'volume': float(self.volume) if self.volume else None,
            'volume_unit': self.volume_unit,
            'weight': float(self.weight) if self.weight else None,
            'weight_unit': self.weight_unit,
            'is_active': self.is_active,
            'is_featured': self.is_featured,
            'requires_prescription': self.requires_prescription,
            'track_inventory': self.track_inventory,
            'stock_quantity': self.stock_quantity,
            'images': self.images or [],
            'nutrition_facts': self.nutrition_facts or {},
            'ingredients': self.ingredients,
            'barcode': self.barcode,
            'slug': self.slug,
            'meta_title': self.meta_title,
            'meta_description': self.meta_description
        }
    

class PriceRule(db.Model, TimestampMixin):
    __tablename__ = 'price_rules'
    
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    rule_type = Column(Enum(PriceRuleType), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    
    # Rule conditions
    min_quantity = Column(Integer, default=1)
    max_quantity = Column(Integer, nullable=True)
    min_order_value = Column(Numeric(precision=10, scale=2), nullable=True)
    customer_type = Column(String(50), nullable=True)  # vip, regular, etc.
    
    # Discount details
    discount_type = Column(String(20), default='percentage')  # percentage or fixed
    discount_value = Column(Numeric(precision=10, scale=2), nullable=False)
    
    # Validity
    is_active = Column(Boolean, default=True)
    valid_from = Column(DateTime, nullable=True)
    valid_until = Column(DateTime, nullable=True)
    
    # Relationship removed - Product model doesn't have price_rules relationship
    # product = relationship('Product', back_populates='price_rules')
