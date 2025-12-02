"""
Blog API endpoints
Manages blog posts for Water Benefits & Health Tips section
"""
from flask import Blueprint, request, current_app, g
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_, or_, func, desc
from datetime import datetime, UTC

from business_app.models.blog import BlogPost, BlogStatus, BlogCategory
from business_app.models.user import User
from business_app.utils.helpers import get_current_language
from business_app.utils.decorators import validate_json, cache_response, admin_required
from business_app import db

# Import API response helpers
from business_app.utils.api_responses import (
    success_response, error_response, paginated_response, created_response,
    not_found_response, validation_error_response, forbidden_response,
    conflict_response, internal_error_response
)

blog_bp = Blueprint('blog', __name__)


@blog_bp.route('/posts', methods=['GET'])
@cache_response(300)  # Cache for 5 minutes
def get_blog_posts():
    """
    Get all published blog posts
    Query params:
    - page: Page number (default: 1)
    - per_page: Items per page (default: 10, max: 50)
    - category: Filter by category
    - featured: Filter featured posts (true/false)
    - language: Language code (uz, ru, en)
    """
    try:
        page = request.args.get('page', 1, type=int)
        per_page = min(request.args.get('per_page', 10, type=int), 50)
        category = request.args.get('category', None)
        featured = request.args.get('featured', None)
        language = request.args.get('language', get_current_language())

        # Base query - only published posts
        query = BlogPost.query.filter(
            BlogPost.status == BlogStatus.PUBLISHED,
            BlogPost.published_at <= datetime.now(UTC)
        )

        # Apply filters
        if category:
            try:
                category_enum = BlogCategory(category)
                query = query.filter(BlogPost.category == category_enum)
            except ValueError:
                return error_response('Invalid category', status_code=400)

        if featured is not None:
            is_featured = featured.lower() in ['true', '1', 'yes']
            query = query.filter(BlogPost.is_featured == is_featured)

        # Order by featured first, then by published date
        query = query.order_by(
            desc(BlogPost.is_featured),
            desc(BlogPost.published_at)
        )

        # Paginate
        pagination = query.paginate(
            page=page,
            per_page=per_page,
            error_out=False
        )

        # Serialize posts
        posts = [post.to_summary_dict(language) for post in pagination.items]

        return paginated_response(
            data=posts,
            page=pagination.page,
            per_page=pagination.per_page,
            total=pagination.total,
            total_pages=pagination.pages
        )

    except Exception as e:
        current_app.logger.error(f"Error fetching blog posts: {str(e)}")
        return internal_error_response()


@blog_bp.route('/posts/<slug>', methods=['GET'])
@cache_response(600)  # Cache for 10 minutes
def get_blog_post(slug):
    """
    Get a single blog post by slug
    Query params:
    - language: Language code (uz, ru, en)
    """
    try:
        language = request.args.get('language', get_current_language())

        # Find post by slug
        post = BlogPost.query.filter(
            BlogPost.slug == slug,
            BlogPost.status == BlogStatus.PUBLISHED,
            BlogPost.published_at <= datetime.now(UTC)
        ).first()

        if not post:
            return not_found_response('Blog post not found')

        # Increment view count
        post.increment_views()
        db.session.commit()

        return success_response(
            data=post.to_dict(language),
            message='Blog post retrieved successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Error fetching blog post {slug}: {str(e)}")
        return internal_error_response()


@blog_bp.route('/posts/featured', methods=['GET'])
@cache_response(300)  # Cache for 5 minutes
def get_featured_posts():
    """
    Get featured blog posts for homepage
    Query params:
    - limit: Number of posts (default: 3, max: 10)
    - language: Language code (uz, ru, en)
    """
    try:
        limit = min(request.args.get('limit', 3, type=int), 10)
        language = request.args.get('language', get_current_language())

        # Get featured published posts
        posts = BlogPost.query.filter(
            BlogPost.status == BlogStatus.PUBLISHED,
            BlogPost.is_featured == True,
            BlogPost.published_at <= datetime.now(UTC)
        ).order_by(
            desc(BlogPost.sort_order),
            desc(BlogPost.published_at)
        ).limit(limit).all()

        # Serialize posts
        posts_data = [post.to_summary_dict(language) for post in posts]

        return success_response(
            data=posts_data,
            message=f'{len(posts_data)} featured posts retrieved successfully'
        )

    except Exception as e:
        current_app.logger.error(f"Error fetching featured posts: {str(e)}")
        return internal_error_response()
