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
from business_app.models.translatable import TranslatableMixin, translatable
import enum

class ProductCategoryEnum(enum.Enum):
    DRINKING_WATER = 'drinking_water'
    SPARKLING_WATER = 'sparkling_water'
    FLAVORED_WATER = 'flavored_water'
    ALKALINE_WATER = 'alkaline_water'
    DISTILLED_WATER = 'distilled_water'
    SPRING_WATER = 'spring_water'

class ProductSizeEnum(enum.Enum):
    SIZE_5L = '5L'
    SIZE_10L = '10L'
    SIZE_19L = '19L'


@translatable('name', 'description')
class ProductCategory(db.Model, TimestampMixin, TranslatableMixin):
    __tablename__ = 'product_categories'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)  # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)   # Default/fallback description (Uzbek)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    icon_url = Column(String(255), nullable=True)
    
    
    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        return self.to_dict_multilingual(language, include_all_translations)

@translatable('name', 'description', 'short_description', 'ingredients', 'meta_title', 'meta_description')
class Product(db.Model, TimestampMixin, TranslatableMixin):
    __tablename__ = 'products'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)           # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)            # Default/fallback description (Uzbek)
    short_description = Column(String(500), nullable=True)  # Default/fallback short description (Uzbek)
    sku = Column(String(100), nullable=True)
    
    # Pricing
    base_price = Column(Numeric(precision=10, scale=2), nullable=False)
    discount_price = Column(Numeric(precision=10, scale=2), nullable=True)
    
    # Product details
    category_id = Column(Integer, ForeignKey('product_categories.id'), nullable=False)
    size = Column(Enum(ProductSizeEnum, name='product_size_enum', values_callable=lambda x: [e.value for e in x]), nullable=False)
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
    ingredients = Column(Text, nullable=True)           # Default/fallback ingredients (Uzbek)
    barcode = Column(String(100), nullable=True)
    
    # SEO and metadata
    slug = Column(String(255), nullable=True)
    meta_title = Column(String(200), nullable=True)     # Default/fallback meta title (Uzbek)
    meta_description = Column(Text, nullable=True)      # Default/fallback meta description (Uzbek)
    
    # Relationships
    category = relationship('ProductCategory', backref='products')
    
    def calculate_price(self, user=None, quantity=1):
        """Calculate dynamic price based on user and quantity"""
        final_price = self.discount_price if self.discount_price else self.base_price
        return max(final_price, 0)

    def to_dict(self, user=None, quantity=1, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        result = self.to_dict_multilingual(language, include_all_translations)
        
        # Add product-specific fields
        result.update({
            'base_price': float(self.base_price) if self.base_price else 0,
            'current_price': float(self.calculate_price(user, quantity)),
            'discount_price': float(self.discount_price) if self.discount_price else None,
            'category': self.category.to_dict(language) if self.category else None,
            'size': self.size.value if self.size else None,
            'volume': float(self.volume) if self.volume else None,
            'weight': float(self.weight) if self.weight else None,
            'images': self.images or [],
            'nutrition_facts': self.nutrition_facts or {},
        })
        
        return result
    

@translatable('name', 'description')
class PriceRule(db.Model, TimestampMixin, TranslatableMixin):
    __tablename__ = 'price_rules'
    
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey('products.id'), nullable=False, index=True)
    rule_type = Column(Enum(PriceRuleType, name='price_rule_type'), nullable=False)
    name = Column(String(100), nullable=False)        # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)         # Default/fallback description (Uzbek)
    
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
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_until = Column(DateTime(timezone=True), nullable=True)
    
    # Relationship removed - Product model doesn't have price_rules relationship
    # product = relationship('Product', back_populates='price_rules')
