from decimal import Decimal
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, Enum, JSON, Index, Numeric
from sqlalchemy.orm import relationship
from business_app import db
from business_app.utils.constants import PriceRuleType
from shared.enums import MarkingCodeStatus
from business_app.models import TimestampMixin
from business_app.models.translatable import TranslatableMixin, translatable
import enum


class ProductCategoryEnum(enum.Enum):
    DRINKING_WATER = "drinking_water"
    SPARKLING_WATER = "sparkling_water"
    FLAVORED_WATER = "flavored_water"
    ALKALINE_WATER = "alkaline_water"
    DISTILLED_WATER = "distilled_water"
    SPRING_WATER = "spring_water"


class ProductSizeEnum(enum.Enum):
    SIZE_5L = "5L"
    SIZE_10L = "10L"
    SIZE_19L = "19L"


@translatable("name", "description")
class ProductCategory(db.Model, TimestampMixin, TranslatableMixin):
    __tablename__ = "product_categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)  # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)  # Default/fallback description (Uzbek)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    icon_url = Column(String(255), nullable=True)

    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        return self.to_dict_multilingual(language, include_all_translations)


@translatable("name", "description", "short_description", "ingredients", "meta_title", "meta_description")
class Product(db.Model, TimestampMixin, TranslatableMixin):
    __tablename__ = "products"
    __table_args__ = (
        Index("idx_products_active_category", "is_active", "category_id"),
        Index("idx_products_active_featured", "is_active", "is_featured"),
        Index("idx_products_active_base_price", "is_active", "base_price"),
        Index("idx_products_slug", "slug"),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)  # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)  # Default/fallback description (Uzbek)
    short_description = Column(String(500), nullable=True)  # Default/fallback short description (Uzbek)
    sku = Column(String(100), nullable=True)

    # Pricing
    base_price = Column(Numeric(precision=10, scale=2), nullable=False)
    discount_price = Column(Numeric(precision=10, scale=2), nullable=True)

    # Product details
    category_id = Column(Integer, ForeignKey("product_categories.id"), nullable=False)
    size = Column(
        Enum(ProductSizeEnum, name="product_size_enum", values_callable=lambda x: [e.value for e in x]), nullable=False
    )
    volume = Column(Float, nullable=True)
    volume_unit = Column(String(10), default="L")
    weight = Column(Float, nullable=True)
    weight_unit = Column(String(10), default="kg")
    is_active = Column(Boolean, default=True)
    is_featured = Column(Boolean, default=False)
    requires_prescription = Column(Boolean, default=False)

    # Inventory
    track_inventory = Column(Boolean, default=True)
    stock_quantity = Column(Integer, default=0)
    min_stock_level = Column(Integer, default=0)
    max_stock_level = Column(Integer, default=1000)
    is_tryout_eligible = Column(Boolean, nullable=False, default=True, index=True)
    tracks_returnable_bottles = Column(Boolean, nullable=False, default=False, index=True)
    returnable_bottles_per_unit = Column(Numeric(precision=12, scale=2), nullable=False, default=Decimal("0.00"))
    expire_days = Column(Integer, nullable=True, default=180)
    # Per-product purchase minimum (cart/order rule). Distinct from PriceRule.min_quantity,
    # which is for bulk-discount tiers, and from min_stock_level, which is a restock threshold.
    min_order_quantity = Column(Integer, nullable=False, default=1, server_default="1")

    # Media and content
    images = Column(JSON, default=[])

    # Content
    nutrition_facts = Column(JSON, default={})
    ingredients = Column(Text, nullable=True)  # Default/fallback ingredients (Uzbek)
    barcode = Column(String(100), nullable=True)

    # SEO and metadata
    slug = Column(String(255), nullable=True)
    meta_title = Column(String(200), nullable=True)  # Default/fallback meta title (Uzbek)
    meta_description = Column(Text, nullable=True)  # Default/fallback meta description (Uzbek)

    # Relationships
    category = relationship("ProductCategory", backref="products")
    fiscal_profile = relationship(
        "ProductFiscalProfile",
        back_populates="product",
        uselist=False,
        cascade="all, delete-orphan",
    )
    marking_codes = relationship(
        "ProductMarkingCode",
        back_populates="product",
        cascade="all, delete-orphan",
    )

    @property
    def fiscalization_enabled(self) -> bool:
        return bool(self.fiscal_profile and self.fiscal_profile.fiscalization_enabled)

    @property
    def requires_marking_codes(self) -> bool:
        return bool(self.fiscal_profile and self.fiscal_profile.requires_marking_codes)

    @property
    def spic(self):
        return self.fiscal_profile.spic if self.fiscal_profile else None

    @property
    def package_code(self):
        return self.fiscal_profile.package_code if self.fiscal_profile else None

    @property
    def units(self):
        return self.fiscal_profile.units if self.fiscal_profile else None

    @property
    def vat_percent(self):
        return self.fiscal_profile.vat_percent if self.fiscal_profile else None

    def calculate_price(self, user=None, quantity=1):
        """Calculate dynamic price based on user and quantity"""
        final_price = self.discount_price if self.discount_price else self.base_price
        return max(final_price, 0)

    def to_dict(self, user=None, quantity=1, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        result = self.to_dict_multilingual(language, include_all_translations)

        # Add product-specific fields
        result.update(
            {
                "base_price": float(self.base_price) if self.base_price else 0,
                "current_price": float(self.calculate_price(user, quantity)),
                "discount_price": float(self.discount_price) if self.discount_price else None,
                "category": self.category.to_dict(language) if self.category else None,
                "size": self.size.value if self.size else None,
                "volume": float(self.volume) if self.volume else None,
                "weight": float(self.weight) if self.weight else None,
                "images": self.images or [],
                "nutrition_facts": self.nutrition_facts or {},
                "is_tryout_eligible": bool(self.is_tryout_eligible),
                "tracks_returnable_bottles": bool(self.tracks_returnable_bottles),
                "returnable_bottles_per_unit": float(self.returnable_bottles_per_unit or 0),
                "expire_days": self.expire_days,
                "min_order_quantity": int(self.min_order_quantity or 1),
                "fiscal_profile": self.fiscal_profile.to_dict() if self.fiscal_profile else None,
            }
        )

        return result


class ProductFiscalProfile(db.Model, TimestampMixin):
    __tablename__ = "product_fiscal_profiles"
    __table_args__ = (
        Index("idx_product_fiscal_profiles_enabled", "fiscalization_enabled"),
        Index("idx_product_fiscal_profiles_marking_required", "requires_marking_codes"),
    )

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, unique=True, index=True)
    spic = Column(String(100), nullable=True)
    package_code = Column(String(100), nullable=True)
    units = Column(String(50), nullable=True)
    vat_percent = Column(Numeric(precision=5, scale=2), nullable=False, default=Decimal("0.00"))
    fiscalization_enabled = Column(Boolean, nullable=False, default=False)
    requires_marking_codes = Column(Boolean, nullable=False, default=False)
    extra_data = Column(JSON, nullable=False, default=dict)

    product = relationship("Product", back_populates="fiscal_profile")

    def to_dict(self):
        return {
            "id": self.id,
            "product_id": self.product_id,
            "spic": self.spic,
            "package_code": self.package_code,
            "units": self.units,
            "vat_percent": float(self.vat_percent or 0),
            "fiscalization_enabled": bool(self.fiscalization_enabled),
            "requires_marking_codes": bool(self.requires_marking_codes),
            "extra_data": self.extra_data or {},
        }


class ProductMarkingCode(db.Model, TimestampMixin):
    __tablename__ = "product_marking_codes"
    __table_args__ = (
        Index("idx_product_marking_codes_product_status", "product_id", "status"),
        Index("idx_product_marking_codes_status_created", "status", "created_at"),
        Index(
            "idx_pmc_product_status_preutil",
            "product_id",
            "status",
            "tax_committee_utilised_at",
        ),
    )

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True)
    code = Column(String(255), nullable=False, unique=True, index=True)
    status = Column(
        Enum(
            MarkingCodeStatus,
            name="product_marking_code_status",
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        default=MarkingCodeStatus.AVAILABLE,
        index=True,
    )
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reserved_at = Column(DateTime(timezone=True), nullable=True)
    used_at = Column(DateTime(timezone=True), nullable=True)
    archived_at = Column(DateTime(timezone=True), nullable=True)
    # NULL → not yet registered with the Tax Committee. NOT NULL → pre-utilised
    # by the proactive daily Celery task, allocation skips the synchronous TC call.
    tax_committee_utilised_at = Column(DateTime(timezone=True), nullable=True, index=True)
    notes = Column(Text, nullable=True)
    extra_data = Column(JSON, nullable=False, default=dict)

    product = relationship("Product", back_populates="marking_codes")
    order = relationship("Order", foreign_keys=[order_id], backref="marking_codes")
    created_by_user = relationship("User", foreign_keys=[created_by_user_id])
    allocation_events = relationship(
        "OrderItemMarkingCodeAllocation",
        back_populates="marking_code",
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "product_id": self.product_id,
            "code": self.code,
            "status": self.status.value if hasattr(self.status, "value") else self.status,
            "created_by_user_id": self.created_by_user_id,
            "reserved_at": self.reserved_at.isoformat() if self.reserved_at else None,
            "used_at": self.used_at.isoformat() if self.used_at else None,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "tax_committee_utilised_at": (
                self.tax_committee_utilised_at.isoformat() if self.tax_committee_utilised_at else None
            ),
            "order_id": self.order_id,
            "notes": self.notes,
            "extra_data": self.extra_data or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


@translatable("name", "description")
class PriceRule(db.Model, TimestampMixin, TranslatableMixin):
    __tablename__ = "price_rules"

    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    rule_type = Column(Enum(PriceRuleType, name="price_rule_type"), nullable=False)
    name = Column(String(100), nullable=False)  # Default/fallback name (Uzbek)
    description = Column(Text, nullable=True)  # Default/fallback description (Uzbek)

    # Rule conditions
    min_quantity = Column(Integer, default=1)
    max_quantity = Column(Integer, nullable=True)
    min_order_value = Column(Numeric(precision=10, scale=2), nullable=True)
    customer_type = Column(String(50), nullable=True)  # vip, regular, etc.

    # Discount details
    discount_type = Column(String(20), default="percentage")  # percentage or fixed
    discount_value = Column(Numeric(precision=10, scale=2), nullable=False)

    # Validity
    is_active = Column(Boolean, default=True)
    valid_from = Column(DateTime(timezone=True), nullable=True)
    valid_until = Column(DateTime(timezone=True), nullable=True)

    # Relationship removed - Product model doesn't have price_rules relationship
    # product = relationship('Product', back_populates='price_rules')
