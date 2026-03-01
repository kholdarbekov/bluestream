"""
Translations API endpoints for admin UI i18n support
Serves translations from database in i18next-compatible format
"""
from flask import Blueprint, jsonify
from business_app.services.admin_ui_translation_service import AdminUiTranslationService
from business_app.utils.rate_limiting import exempt_from_rate_limit
import logging

logger = logging.getLogger(__name__)

translations_bp = Blueprint('translations', __name__)


@translations_bp.route('/<language>/<namespace>', methods=['GET'])
@exempt_from_rate_limit
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
        result = AdminUiTranslationService.get_translations(language, namespace)

        # Log for debugging
        logger.info(f"Served {len(result)} translations for {language}/{namespace}")

        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error loading translations for {language}/{namespace}: {e}")
        # Return empty object on error so i18next doesn't break
        return jsonify({}), 200


@translations_bp.route('/reload', methods=['POST'])
@exempt_from_rate_limit
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
@exempt_from_rate_limit
def get_namespaces():
    """
    Get list of available translation namespaces

    Returns:
        List of namespace names
    """
    try:
        namespaces = AdminUiTranslationService.get_namespaces()

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
