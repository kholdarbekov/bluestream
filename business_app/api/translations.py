"""
Translations API endpoints for admin UI i18n support
Serves translations from database in i18next-compatible format
"""

from flask import Blueprint, jsonify
from business_app.services.admin_ui_translation_service import AdminUiTranslationService
from business_app.utils.rate_limiting import exempt_from_rate_limit
import logging

logger = logging.getLogger(__name__)

translations_bp = Blueprint("translations", __name__)


@translations_bp.route("/<language>/<namespace>", methods=["GET"])
@exempt_from_rate_limit
def get_translations(language, namespace):
    """
    Get translations for a specific language and one-or-many namespaces.

    Single-namespace mode (back-compat) — `GET /<lng>/common` returns a flat
    dict the way i18next's HttpBackend expects by default::

        { "key1": "value1", "key2": "value2", ... }

    Multi-namespace mode — `GET /<lng>/common+navigation+dashboard` returns
    the nested shape that i18next-http-backend expects when
    `allowMultiLoading: true` is enabled on the frontend::

        { "<lng>": { "common": {...}, "navigation": {...}, "dashboard": {...} } }

    Multi-namespace mode collapses what was previously 14 sequential HTTP
    requests on every admin UI cold load into a single request, eliminating
    the fan-out that triggered nginx rate-limit 503s on the public site.

    Args:
        language: Language code (uz, en, ru)
        namespace: Single namespace OR `+`-joined list of namespaces

    Returns:
        JSON dict in single- or multi-namespace shape depending on input.
    """
    try:
        # Multi-namespace path: i18next-http-backend joins namespaces with
        # `+` (the default `multiSeparator`). Split, fetch each, and wrap in
        # the `{lng: {ns: {...}}}` envelope it expects.
        if "+" in namespace:
            ns_list = [ns for ns in namespace.split("+") if ns]
            bundles = {ns: AdminUiTranslationService.get_translations(language, ns) for ns in ns_list}
            total_keys = sum(len(b) for b in bundles.values())
            logger.info(
                f"Served {total_keys} translations across {len(bundles)} namespaces "
                f"for {language}/{'+'.join(ns_list)}"
            )
            return jsonify({language: bundles}), 200

        # Single-namespace path: unchanged behaviour.
        result = AdminUiTranslationService.get_translations(language, namespace)
        logger.info(f"Served {len(result)} translations for {language}/{namespace}")
        return jsonify(result), 200

    except Exception as e:
        logger.error(f"Error loading translations for {language}/{namespace}: {e}")
        # Return empty object on error so i18next doesn't break. Shape
        # matches the request mode so the frontend's parser doesn't choke.
        if "+" in namespace:
            return jsonify({language: {}}), 200
        return jsonify({}), 200


@translations_bp.route("/reload", methods=["POST"])
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
        return jsonify({"success": True, "message": "Translation reload triggered successfully"}), 200

    except Exception as e:
        logger.error(f"Error triggering translation reload: {e}")
        return jsonify({"success": False, "message": "Failed to trigger translation reload", "error": str(e)}), 500


@translations_bp.route("/namespaces", methods=["GET"])
@exempt_from_rate_limit
def get_namespaces():
    """
    Get list of available translation namespaces

    Returns:
        List of namespace names
    """
    try:
        namespaces = AdminUiTranslationService.get_namespaces()

        return jsonify({"success": True, "namespaces": namespaces}), 200

    except Exception as e:
        logger.error(f"Error getting namespaces: {e}")
        return jsonify({"success": False, "message": "Failed to get namespaces", "error": str(e)}), 500
