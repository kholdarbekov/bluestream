"""
Blog Model
Manages blog posts/articles for the Water Benefits & Health Tips section
"""
from datetime import datetime, UTC
from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Index, Enum as SQLEnum
from sqlalchemy.orm import relationship
from business_app import db
from business_app.models.base import TimestampMixin
from business_app.models.translatable import TranslatableMixin, translatable
import enum


class BlogStatus(enum.Enum):
    """Blog post status"""
    DRAFT = 'draft'
    PUBLISHED = 'published'
    ARCHIVED = 'archived'


class BlogCategory(enum.Enum):
    """Blog categories"""
    HEALTH_TIPS = 'health_tips'
    WATER_BENEFITS = 'water_benefits'
    COMPANY_NEWS = 'company_news'
    QUALITY_ASSURANCE = 'quality_assurance'
    LIFESTYLE = 'lifestyle'
    ENVIRONMENT = 'environment'


@translatable('title', 'excerpt', 'content', 'author_name', 'meta_title', 'meta_description')
class BlogPost(db.Model, TimestampMixin, TranslatableMixin):
    """
    Blog post model with multilingual support
    Stores blog posts for Water Benefits & Health Tips section
    """
    __tablename__ = 'blog_posts'

    id = Column(Integer, primary_key=True)

    # Content fields (default/fallback in Uzbek)
    title = Column(String(255), nullable=False, index=True)
    slug = Column(String(300), nullable=False, unique=True, index=True)
    excerpt = Column(Text, nullable=True)  # Short summary
    content = Column(Text, nullable=False)  # Full blog content

    # Author information
    author_name = Column(String(100), nullable=True)  # Display name
    author_id = Column(Integer, ForeignKey('users.id'), nullable=True)  # Optional link to user

    # Categorization
    category = Column(SQLEnum(BlogCategory, name='blog_category', values_callable=lambda x: [e.value for e in x]),
                     nullable=False, default=BlogCategory.HEALTH_TIPS)
    tags = Column(String(500), nullable=True)  # Comma-separated tags

    # Media
    featured_image = Column(Text, nullable=True)  # Stores image URL or file path
    image_alt_text = Column(String(255), nullable=True)

    # Publishing
    status = Column(SQLEnum(BlogStatus, name='blog_status', values_callable=lambda x: [e.value for e in x]),
                   nullable=False, default=BlogStatus.DRAFT, index=True)
    published_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Display settings
    is_featured = Column(Boolean, default=False)  # Show on homepage
    sort_order = Column(Integer, default=0)  # For manual ordering
    view_count = Column(Integer, default=0)

    # SEO
    meta_title = Column(String(100), nullable=True)
    meta_description = Column(Text, nullable=True)

    # Relationships
    author = relationship('User', backref='blog_posts', foreign_keys=[author_id])

    # Indexes
    __table_args__ = (
        Index('idx_blog_status_published', 'status', 'published_at'),
        Index('idx_blog_featured', 'is_featured', 'published_at'),
        Index('idx_blog_category', 'category', 'published_at'),
    )

    @property
    def is_published(self):
        """Check if blog post is published"""
        return (
            self.status == BlogStatus.PUBLISHED and
            self.published_at is not None and
            self.published_at <= datetime.now(UTC)
        )

    def publish(self):
        """Publish the blog post"""
        if self.status != BlogStatus.PUBLISHED:
            self.status = BlogStatus.PUBLISHED
            if not self.published_at:
                self.published_at = datetime.now(UTC)

    def unpublish(self):
        """Unpublish the blog post"""
        self.status = BlogStatus.DRAFT

    def archive(self):
        """Archive the blog post"""
        self.status = BlogStatus.ARCHIVED

    def increment_views(self):
        """Increment view count"""
        self.view_count += 1

    def get_category_display(self, language=None):
        """Get translated category name"""
        category_translations = {
            'health_tips': {
                'en': 'Health Tips',
                'ru': 'Советы по здоровью',
                'uz': 'Sog\'liq maslahatlari'
            },
            'water_benefits': {
                'en': 'Water Benefits',
                'ru': 'Польза воды',
                'uz': 'Suv foydalari'
            },
            'company_news': {
                'en': 'Company News',
                'ru': 'Новости компании',
                'uz': 'Kompaniya yangiliklari'
            },
            'quality_assurance': {
                'en': 'Quality Assurance',
                'ru': 'Контроль качества',
                'uz': 'Sifat nazorati'
            },
            'lifestyle': {
                'en': 'Lifestyle',
                'ru': 'Образ жизни',
                'uz': 'Turmush tarzi'
            },
            'environment': {
                'en': 'Environment',
                'ru': 'Экология',
                'uz': 'Atrof-muhit'
            }
        }

        from business_app.utils.helpers import get_current_language
        if language is None:
            language = get_current_language()

        category_key = self.category.value if hasattr(self.category, 'value') else str(self.category)
        return category_translations.get(category_key, {}).get(language, category_key)

    def to_dict(self, language=None, include_all_translations=False):
        """Convert to dictionary with multilingual support"""
        result = self.to_dict_multilingual(language, include_all_translations)

        # Add blog-specific fields
        result.update({
            'slug': self.slug,
            'category': self.category.value if hasattr(self.category, 'value') else str(self.category),
            'category_display': self.get_category_display(language),
            'status': self.status.value if hasattr(self.status, 'value') else str(self.status),
            'tags': self.tags.split(',') if self.tags else [],
            'featured_image': self.featured_image,
            'image_alt_text': self.image_alt_text,
            'is_featured': self.is_featured,
            'is_published': self.is_published,
            'published_at': self.published_at.isoformat() if self.published_at else None,
            'view_count': self.view_count,
            'sort_order': self.sort_order,
            'author': {
                'id': self.author.id if self.author else None,
                'name': self.author_name or (self.author.full_name if self.author else 'Admin')
            }
        })

        return result

    def to_summary_dict(self, language=None):
        """Convert to summary dictionary (for list views)"""
        result = {
            'id': self.id,
            'title': self.get_translated('title', language),
            'slug': self.slug,
            'excerpt': self.get_translated('excerpt', language),
            'category': self.category.value if hasattr(self.category, 'value') else str(self.category),
            'category_display': self.get_category_display(language),
            'featured_image': self.featured_image,
            'image_alt_text': self.image_alt_text,
            'is_featured': self.is_featured,
            'published_at': self.published_at.isoformat() if self.published_at else None,
            'view_count': self.view_count,
            'author': {
                'name': self.author_name or (self.author.full_name if self.author else 'Admin')
            },
            'created_at': self.created_at.isoformat() if self.created_at else None
        }
        return result

    def __repr__(self):
        return f'<BlogPost {self.id}: {self.title}>'
