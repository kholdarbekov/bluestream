"""
Translations API endpoints for admin UI i18n support
Serves translations from database in i18next-compatible format
"""
from flask import Blueprint, jsonify, request
from business_app.models.translation import Translation
import logging

logger = logging.getLogger(__name__)

translations_bp = Blueprint('translations', __name__)


@translations_bp.route('/<language>/<namespace>', methods=['GET'])
def get_translations(language, namespace):
    """
    Get translations for a specific language and namespace in i18next format

    This endpoint serves translations from the database in the format expected by i18next:
    {
        "key1": "value1",
        "key2": "value2",
        ...
    }

    Args:
        language: Language code (uz, en, ru)
        namespace: Translation namespace/category (common, dashboard, orders, etc.)

    Returns:
        JSON object with key-value pairs
    """
    try:
        # Map i18next namespaces to our translation categories
        # For admin UI, we use 'ui' category with subcategories
        namespace_mapping = {
            'common': 'ui',
            'navigation': 'ui_navigation',
            'dashboard': 'ui_dashboard',
            'orders': 'ui_orders',
            'products': 'ui_products',
            'users': 'ui_users',
            'settings': 'ui_settings',
            'profile': 'ui_profile',
            'analytics': 'ui_analytics',
            'blog': 'ui_blog',
            'delivery': 'ui_delivery',
            'loyalty': 'ui_loyalty',
            'login': 'ui_login'
        }

        category = namespace_mapping.get(namespace, f'ui_{namespace}')

        # Query translations from database
        translations = Translation.query.filter_by(
            language=language,
            category=category,
            is_active=True
        ).all()

        # Convert to i18next format (flat key-value object)
        result = {}
        for translation in translations:
            # Remove category prefix from key for cleaner usage
            # e.g., "ui.common.welcome" -> "welcome"
            key = translation.key
            if '.' in key:
                # Keep the full key structure for now
                # We'll use the full key in the frontend
                result[key] = translation.value
            else:
                result[key] = translation.value

        # Log for debugging
        logger.info(f"Served {len(result)} translations for {language}/{namespace}")

        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error loading translations for {language}/{namespace}: {e}")
        # Return empty object on error so i18next doesn't break
        return jsonify({}), 200


@translations_bp.route('/reload', methods=['POST'])
def reload_translations():
    """
    Trigger translation reload in admin UI

    This endpoint can be called from the admin UI to force a reload of translations
    after they've been updated in the database.

    Returns:
        Success message
    """
    try:
        # This endpoint just confirms the request
        # The actual reload happens on the frontend when it calls i18n.reloadResources()
        return jsonify({
            'success': True,
            'message': 'Translation reload triggered successfully'
        }), 200

    except Exception as e:
        logger.error(f"Error triggering translation reload: {e}")
        return jsonify({
            'success': False,
            'message': 'Failed to trigger translation reload',
            'error': str(e)
        }), 500


@translations_bp.route('/namespaces', methods=['GET'])
def get_namespaces():
    """
    Get list of available translation namespaces

    Returns:
        List of namespace names
    """
    try:
        # Get all unique UI categories from database
        from sqlalchemy import distinct
        from business_app import db

        categories = db.session.query(distinct(Translation.category)).filter(
            Translation.category.like('ui%'),
            Translation.is_active == True
        ).all()

        # Map back to namespace names
        namespaces = []
        for (category,) in categories:
            if category == 'ui':
                namespaces.append('common')
            elif category.startswith('ui_'):
                namespaces.append(category[3:])  # Remove 'ui_' prefix

        return jsonify({
            'success': True,
            'namespaces': namespaces
        }), 200

    except Exception as e:
        logger.error(f"Error getting namespaces: {e}")
        return jsonify({
            'success': False,
            'message': 'Failed to get namespaces',
            'error': str(e)
        }), 500
