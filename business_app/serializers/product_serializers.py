"""
Product Serializers for the Water Business Platform using Pydantic v2
This file contains Pydantic models for product-related data serialization
"""
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, ConfigDict
from pydantic.alias_generators import to_camel
from business_app.models.product import Product, ProductCategory

logger = logging.getLogger(__name__)


class ProductCategorySchema(BaseModel):
    """Product category schema"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    name: str
    slug: Optional[str] = None
    icon_url: Optional[str] = None
    parent_id: Optional[int] = None


class ProductSpecificationsSchema(BaseModel):
    """Product specifications schema"""
    volume: Optional[float] = None
    volume_unit: Optional[str] = None
    weight: Optional[float] = None
    weight_unit: Optional[str] = None
    dimensions: Optional[Dict[str, Any]] = None
    material: Optional[str] = None
    color: Optional[str] = None
    brand: Optional[str] = None


class ProductPricingSchema(BaseModel):
    """Product pricing information"""
    base_price: Decimal
    current_price: Decimal
    discount_amount: Decimal
    discount_percentage: float
    quantity: int = Field(default=1)
    total_price: Decimal
    currency: str = Field(default="UZS")
    is_discounted: bool
    
    @field_validator('base_price', 'current_price', 'discount_amount', 'total_price')
    @classmethod
    def validate_prices(cls, v):
        return float(v)


class ProductMediaSchema(BaseModel):
    """Product media information"""
    main_image: Optional[str] = None
    image_urls: List[str] = Field(default_factory=list)
    video_url: Optional[str] = None
    gallery_images: List[str] = Field(default_factory=list)


class ProductInventorySchema(BaseModel):
    """Product inventory information"""
    stock_quantity: Optional[int] = None
    track_inventory: bool = Field(default=True)
    min_stock_level: Optional[int] = None
    is_low_stock: bool = Field(default=False)
    is_in_stock: bool = Field(default=True)
    restock_date: Optional[datetime] = None


class ProductRatingsSchema(BaseModel):
    """Product ratings information"""
    average_rating: float = Field(default=0.0)
    review_count: int = Field(default=0)
    rating_distribution: Dict[str, int] = Field(default_factory=dict)


class ProductFlagsSchema(BaseModel):
    """Product flags/status information"""
    is_active: bool = Field(default=True)
    is_featured: bool = Field(default=False)
    is_new: bool = Field(default=False)
    is_bestseller: bool = Field(default=False)
    is_organic: bool = Field(default=False)
    is_premium: bool = Field(default=False)


class ProductSalesSchema(BaseModel):
    """Product sales information"""
    total_sold: int = Field(default=0)
    view_count: int = Field(default=0)
    popularity_score: float = Field(default=0.0)


class ProductSeoSchema(BaseModel):
    """Product SEO information"""
    slug: Optional[str] = None
    meta_title: Optional[str] = None
    meta_description: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)


class ProductDatesSchema(BaseModel):
    """Product date information"""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    published_at: Optional[datetime] = None


class ProductUserDataSchema(BaseModel):
    """User-specific product data"""
    is_favorited: bool = Field(default=False)
    purchase_history: Dict[str, Any] = Field(default_factory=dict)
    personalized_price: bool = Field(default=False)
    recommended_quantity: int = Field(default=1)


class ProductDiscountSchema(BaseModel):
    """Product discount information"""
    type: str
    name: str
    description: Optional[str] = None
    discount_value: Decimal
    discount_type: str
    conditions: Optional[str] = None
    
    @field_validator('discount_value')
    @classmethod
    def validate_discount_value(cls, v):
        return float(v)


class ProductSchema(BaseModel):
    """Main product schema for API responses"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    name: str
    description: Optional[str] = None
    short_description: Optional[str] = None
    sku: str
    barcode: Optional[str] = None
    category: Optional[ProductCategorySchema] = None
    pricing: ProductPricingSchema
    media: ProductMediaSchema
    specifications: ProductSpecificationsSchema
    inventory: ProductInventorySchema
    ratings: ProductRatingsSchema
    flags: ProductFlagsSchema
    sales: ProductSalesSchema
    seo: ProductSeoSchema
    dates: ProductDatesSchema
    user_data: Optional[ProductUserDataSchema] = None
    discounts: List[ProductDiscountSchema] = Field(default_factory=list)


class ProductListSchema(BaseModel):
    """Schema for product list responses"""
    products: List[ProductSchema]
    total: int
    page: int
    per_page: int
    pages: int


class ProductCategoryFullSchema(BaseModel):
    """Full product category schema with subcategories and products"""
    model_config = ConfigDict(from_attributes=True, alias_generator=to_camel)
    
    id: int
    name: str
    description: Optional[str] = None
    slug: Optional[str] = None
    icon_url: Optional[str] = None
    image_url: Optional[str] = None
    sort_order: int = Field(default=0)
    is_active: bool = Field(default=True)
    parent_id: Optional[int] = None
    level: int = Field(default=0)
    product_count: int = Field(default=0)
    created_at: Optional[datetime] = None
    subcategories: List['ProductCategoryFullSchema'] = Field(default_factory=list)
    products: Optional[List[ProductSchema]] = None


class ProductSearchMetadataSchema(BaseModel):
    """Product search metadata"""
    query: str
    total_results: int
    search_time: float
    suggestions: List[str] = Field(default_factory=list)
    did_you_mean: Optional[str] = None


class ProductSearchResultSchema(BaseModel):
    """Product search result schema"""
    products: List[ProductSchema] = Field(default_factory=list)
    categories: List[ProductCategoryFullSchema] = Field(default_factory=list)
    filters: Dict[str, Any] = Field(default_factory=dict)
    pagination: Dict[str, Any] = Field(default_factory=dict)
    search_metadata: ProductSearchMetadataSchema


class ProductComparisonAttributeSchema(BaseModel):
    """Product comparison attribute"""
    key: str
    name: str
    type: str  # currency, number, rating, text, boolean


class ProductComparisonSchema(BaseModel):
    """Product comparison schema"""
    products: List[ProductSchema]
    comparison_attributes: List[ProductComparisonAttributeSchema]
    best_value: Optional[int] = None  # Product ID with best value
    highest_rated: Optional[int] = None  # Product ID with highest rating


class CreateProductRequest(BaseModel):
    """Create product request schema"""
    name: str = Field(..., min_length=3, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    short_description: Optional[str] = Field(None, max_length=500)
    sku: str = Field(..., min_length=3, max_length=50)
    barcode: Optional[str] = Field(None, max_length=50)
    category_id: Optional[int] = None
    base_price: Decimal = Field(..., gt=0)
    volume: Optional[float] = Field(None, gt=0)
    volume_unit: Optional[str] = None
    weight: Optional[float] = Field(None, gt=0)
    weight_unit: Optional[str] = None
    material: Optional[str] = None
    color: Optional[str] = None
    brand: Optional[str] = None
    stock_quantity: Optional[int] = Field(None, ge=0)
    track_inventory: bool = Field(default=True)
    min_stock_level: Optional[int] = Field(None, ge=0)
    is_active: bool = Field(default=True)
    is_featured: bool = Field(default=False)
    
    @field_validator('base_price')
    @classmethod
    def validate_base_price(cls, v):
        return float(v)


class UpdateProductRequest(BaseModel):
    """Update product request schema"""
    name: Optional[str] = Field(None, min_length=3, max_length=255)
    description: Optional[str] = Field(None, max_length=2000)
    short_description: Optional[str] = Field(None, max_length=500)
    base_price: Optional[Decimal] = Field(None, gt=0)
    category_id: Optional[int] = None
    volume: Optional[float] = Field(None, gt=0)
    volume_unit: Optional[str] = None
    weight: Optional[float] = Field(None, gt=0)
    weight_unit: Optional[str] = None
    material: Optional[str] = None
    color: Optional[str] = None
    brand: Optional[str] = None
    stock_quantity: Optional[int] = Field(None, ge=0)
    track_inventory: Optional[bool] = None
    min_stock_level: Optional[int] = Field(None, ge=0)
    is_active: Optional[bool] = None
    is_featured: Optional[bool] = None
    
    @field_validator('base_price')
    @classmethod
    def validate_base_price(cls, v):
        if v is not None:
            return float(v)
        return v


class ProductResponseSchema(BaseModel):
    """Standard product response schema"""
    success: bool
    message: str
    product: Optional[ProductSchema] = None
    errors: Optional[List[str]] = None


class ProductBulkUpdateRequest(BaseModel):
    """Bulk product update request schema"""
    product_ids: List[int] = Field(..., min_items=1, max_items=100)
    updates: Dict[str, Any] = Field(..., min_items=1)
    
    @field_validator('product_ids')
    @classmethod
    def validate_product_ids(cls, v):
        if len(v) != len(set(v)):
            raise ValueError('Product IDs must be unique')
        return v


# Export all schemas for easy importing
__all__ = [
    'ProductSchema',
    'ProductListSchema',
    'ProductCategorySchema',
    'ProductCategoryFullSchema',
    'ProductSearchResultSchema',
    'ProductComparisonSchema',
    'CreateProductRequest',
    'UpdateProductRequest',
    'ProductResponseSchema',
    'ProductBulkUpdateRequest',
    'ProductPricingSchema',
    'ProductMediaSchema',
    'ProductSpecificationsSchema',
    'ProductInventorySchema',
    'ProductRatingsSchema',
    'ProductFlagsSchema',
    'ProductSalesSchema',
    'ProductSeoSchema'
]


def serialize_product(product: Product, language: str = 'uz', user=None, quantity: int = 1) -> Dict[str, Any]:
    """
    Serialize a product object to dictionary using Pydantic
    
    Args:
        product: Product model instance
        language: Language code for localized content
        user: Current user for personalized data
        quantity: Quantity for bulk pricing calculations
        
    Returns:
        Serialized product data
    """
    try:
        # Build the product data manually since we need to calculate pricing and other dynamic data
        base_price = float(product.base_price)
        current_price = calculate_product_price(product, user, quantity)
        
        product_data = {
            'id': product.id,
            'name': get_localized_field(product, 'name', language),
            'description': get_localized_field(product, 'description', language),
            'short_description': get_localized_field(product, 'short_description', language),
            'sku': product.sku,
            'barcode': product.barcode,
            'category': serialize_product_category(product.category, language) if product.category else None,
            'pricing': {
                'base_price': base_price,
                'current_price': current_price,
                'discount_amount': base_price - current_price,
                'discount_percentage': round(((base_price - current_price) / base_price * 100), 2) if base_price > 0 else 0,
                'quantity': quantity,
                'total_price': current_price * quantity,
                'currency': 'UZS',
                'is_discounted': current_price < base_price
            },
            'media': {
                'images': product.images,
                # 'image_urls': product.image_urls or [],
                # 'video_url': product.video_url,
                # 'gallery_images': product.gallery_images or []
            },
            'specifications': {
                'volume': product.volume,
                'volume_unit': product.volume_unit,
                'weight': product.weight,
                'weight_unit': product.weight_unit,
                'dimensions': {
                    'length': getattr(product, 'length', None),
                    'width': getattr(product, 'width', None),
                    'height': getattr(product, 'height', None),
                    'unit': getattr(product, 'dimension_unit', None)
                },
                # 'material': product.material,
                # 'color': product.color,
                # 'brand': product.brand
            },
            'inventory': {
                'stock_quantity': product.stock_quantity if product.track_inventory else None,
                'track_inventory': product.track_inventory,
                'min_stock_level': product.min_stock_level,
                'is_low_stock': is_product_low_stock(product),
                'is_in_stock': is_product_in_stock(product),
                # 'restock_date': product.restock_date.isoformat() if product.restock_date else None
            },
            'ratings': {
                # 'average_rating': float(product.average_rating) if product.average_rating else 0.0,
                # 'review_count': product.review_count or 0,
                'rating_distribution': get_rating_distribution(product)
            },
            'flags': {
                'is_active': product.is_active,
                'is_featured': product.is_featured,
                'is_new': is_new_product(product),
                'is_bestseller': getattr(product, 'is_bestseller', False),
                'is_organic': getattr(product, 'is_organic', False),
                'is_premium': getattr(product, 'is_premium', False)
            },
            # 'sales': {
            #     # 'total_sold': product.total_sold or 0,
            #     # 'view_count': product.view_count or 0,
            #     # 'popularity_score': calculate_popularity_score(product)
            # },
            'seo': {
                'slug': getattr(product, 'slug', None),
                'meta_title': get_localized_field(product, 'meta_title', language),
                'meta_description': get_localized_field(product, 'meta_description', language),
                'keywords': getattr(product, 'keywords', [])
            },
            'dates': {
                'created_at': product.created_at.isoformat() if product.created_at else None,
                'updated_at': product.updated_at.isoformat() if product.updated_at else None,
                'published_at': getattr(product, 'published_at', None)
            }
        }
        
        # Add user-specific data if user is provided
        if user:
            product_data['user_data'] = {
                'is_favorited': is_favorited_by_user(product, user),
                'purchase_history': get_user_purchase_history(product, user),
                'personalized_price': current_price != base_price,
                'recommended_quantity': get_recommended_quantity(product, user)
            }
        
        # Add applicable discounts
        # product_data['discounts'] = get_applicable_discounts(product, user, quantity)
        
        return product_data
        
    except Exception as e:
        logger.error(f"Error serializing product ID {product.id}: {e}")
        # Fallback to basic serialization if complex logic fails
        return {
            'id': product.id,
            'name': product.name,
            'sku': product.sku,
            'base_price': float(product.base_price),
            'is_active': product.is_active,
            'created_at': product.created_at.isoformat() if product.created_at else None
        }


def serialize_product_list(products: List, language: str = 'uz', user=None) -> List[Dict[str, Any]]:
    """
    Serialize a list of products
    
    Args:
        products: List of product model instances
        language: Language code for localized content
        user: Current user for personalized data
        
    Returns:
        List of serialized product data
    """
    return [serialize_product(product, language, user) for product in products]


def serialize_product_category(category, language: str = 'uz') -> Dict[str, Any]:
    """
    Serialize a product category
    
    Args:
        category: ProductCategory model instance
        language: Language code for localized content
        
    Returns:
        Serialized category data
    """
    if not category:
        return None
        
    return {
        'id': category.id,
        'name': get_localized_field(category, 'name', language),
        'slug': getattr(category, 'slug', None),
        'icon_url': category.icon_url,
        'parent_id': getattr(category, 'parent_id', None)  # Categories don't have hierarchy yet
    }


# Helper functions
def get_localized_field(obj, field_name: str, language: str) -> Optional[str]:
    """Get localized field value"""
    # Check if the object has TranslatableMixin's get_translated method
    if hasattr(obj, 'get_translated'):
        return obj.get_translated(field_name, language)

    # Legacy: Try to get localized version from direct fields (name_ru, name_en, etc.)
    localized_field = f"{field_name}_{language}"
    if hasattr(obj, localized_field):
        localized_value = getattr(obj, localized_field)
        if localized_value:
            return localized_value

    # Fall back to default field
    return getattr(obj, field_name, None)


def calculate_product_price(product, user, quantity: int) -> float:
    """Calculate product price with discounts"""
    base_price = float(product.base_price)
    
    # Apply bulk discounts
    if hasattr(product, 'price_rules') and product.price_rules:
        for rule in product.price_rules:
            if rule.is_active and quantity >= rule.min_quantity:
                if rule.max_quantity is None or quantity <= rule.max_quantity:
                    if rule.discount_type == 'percentage':
                        discount = base_price * (rule.discount_value / 100)
                    else:
                        discount = rule.discount_value
                    return max(0, base_price - discount)
    
    # Apply user-specific discounts (VIP, loyalty tier, etc.)
    if user:
        discount_percentage = 0
        
        # VIP discount
        if getattr(user, 'is_vip', False):
            discount_percentage += 5  # 5% VIP discount
        
        # Loyalty tier discount
        if hasattr(user, 'loyalty_tier'):
            tier_discounts = {'bronze': 0, 'silver': 2, 'gold': 5, 'platinum': 10}
            discount_percentage += tier_discounts.get(user.loyalty_tier, 0)
        
        if discount_percentage > 0:
            discount = base_price * (discount_percentage / 100)
            return max(0, base_price - discount)
    
    return base_price


def is_product_low_stock(product) -> bool:
    """Check if product is low in stock"""
    if not product.track_inventory:
        return False
    return (product.stock_quantity or 0) <= (product.min_stock_level or 0)


def is_product_in_stock(product) -> bool:
    """Check if product is in stock"""
    if not product.track_inventory:
        return True
    return (product.stock_quantity or 0) > 0


def is_new_product(product) -> bool:
    """Check if product is new (created within last 30 days)"""
    if not product.created_at:
        return False
    
    days_since_creation = (datetime.now(timezone.utc) - product.created_at).days
    return days_since_creation <= 30


def get_rating_distribution(product) -> Dict[str, int]:
    """Get rating distribution"""
    # This would typically query the review table
    # For now, return placeholder data
    return {
        '5': 0,
        '4': 0,
        '3': 0,
        '2': 0,
        '1': 0
    }


def calculate_popularity_score(product) -> float:
    """Calculate product popularity score"""
    view_count = product.view_count or 0
    total_sold = product.total_sold or 0
    rating = product.average_rating or 0
    review_count = product.review_count or 0
    
    # Weighted popularity score
    score = (
        view_count * 0.1 +
        total_sold * 1.0 +
        rating * review_count * 0.5
    )
    
    return round(score, 2)


def is_favorited_by_user(product, user) -> bool:
    """Check if product is favorited by user"""
    # This would typically check user favorites
    return False


def get_user_purchase_history(product, user) -> Dict[str, Any]:
    """Get user's purchase history for this product"""
    # This would typically query order history
    return {
        'total_purchased': 0,
        'last_purchase_date': None,
        'average_quantity': 0
    }


def get_recommended_quantity(product, user) -> int:
    """Get recommended quantity based on user history"""
    # This would use ML/analytics to suggest quantity
    return 1


def get_applicable_discounts(product, user, quantity: int) -> List[Dict[str, Any]]:
    """Get applicable discounts for the product"""
    discounts = []
    
    # Bulk discount
    if hasattr(product, 'price_rules') and product.price_rules:
        for rule in product.price_rules:
            if (rule.is_active and 
                quantity >= rule.min_quantity and 
                (rule.max_quantity is None or quantity <= rule.max_quantity)):
                
                discounts.append({
                    'type': 'bulk',
                    'name': rule.name,
                    'description': rule.description,
                    'discount_value': rule.discount_value,
                    'discount_type': rule.discount_type,
                    'conditions': f"Min quantity: {rule.min_quantity}"
                })
    
    return discounts