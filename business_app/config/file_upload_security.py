"""
File Upload Security Configuration
Centralized security settings for all file upload functionality
"""

from typing import Dict, Set, Any

# =============================================================================
# GLOBAL SECURITY SETTINGS
# =============================================================================

# Enable enhanced security validation by default
ENABLE_ENHANCED_VALIDATION = True

# Enable virus scanning (requires ClamAV or similar)
ENABLE_VIRUS_SCANNING = False

# Maximum file sizes by category (in bytes)
MAX_FILE_SIZES = {
    "images": 10 * 1024 * 1024,  # 10MB
    "documents": 25 * 1024 * 1024,  # 25MB
    "archives": 50 * 1024 * 1024,  # 50MB
    "media": 100 * 1024 * 1024,  # 100MB
    "delivery_photos": 5 * 1024 * 1024,  # 5MB for delivery photos
    "default": 16 * 1024 * 1024,  # 16MB default
}

# Allowed file extensions by category
ALLOWED_EXTENSIONS = {
    "images": {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"},
    "delivery_photos": {".jpg", ".jpeg", ".png"},  # Strict for delivery photos
    "documents": {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".txt"},
    "archives": {".zip"},  # Very restricted
    "media": {".mp3", ".mp4", ".wav"},  # Basic media types only
}

# Completely forbidden extensions (never allow these)
FORBIDDEN_EXTENSIONS = {
    # Executables
    ".exe",
    ".bat",
    ".cmd",
    ".com",
    ".scr",
    ".pif",
    ".msi",
    ".app",
    ".deb",
    ".rpm",
    ".dmg",
    ".pkg",
    ".run",
    # Scripts
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".vbs",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".rb",
    ".pl",
    ".php",
    ".asp",
    ".aspx",
    ".jsp",
    ".cfm",
    ".go",
    ".rs",
    # System files
    ".dll",
    ".so",
    ".dylib",
    ".sys",
    ".drv",
    ".ocx",
    # Configuration files
    ".htaccess",
    ".htpasswd",
    ".config",
    ".ini",
    ".conf",
    ".env",
    # Archive formats that could contain scripts
    ".jar",
    ".war",
    ".ear",
    ".tar.gz",
    ".tar.bz2",
    # Database files
    ".sql",
    ".db",
    ".sqlite",
    ".mdb",
    ".accdb",
    # Other dangerous formats
    ".iso",
    ".vhd",
    ".vmdk",
    ".ova",
    ".ovf",
}

# =============================================================================
# ENDPOINT-SPECIFIC CONFIGURATIONS
# =============================================================================

ENDPOINT_CONFIGS = {
    # Delivery photo uploads - very strict
    "/api/v1/delivery/upload-photo": {
        "allowed_categories": ["delivery_photos"],
        "expected_category": "delivery_photos",
        "max_file_size": MAX_FILE_SIZES["delivery_photos"],
        "allowed_extensions": ALLOWED_EXTENSIONS["delivery_photos"],
        "require_image_validation": True,
        "auto_resize": True,
        "max_dimensions": (1920, 1080),
        "strip_metadata": True,
        "require_authentication": True,
        "require_role": "delivery_driver",
    },
    # Admin product image uploads
    "/api/v1/admin/products/upload-image": {
        "allowed_categories": ["images"],
        "expected_category": "images",
        "max_file_size": MAX_FILE_SIZES["images"],
        "allowed_extensions": ALLOWED_EXTENSIONS["images"],
        "require_image_validation": True,
        "auto_resize": True,
        "max_dimensions": (2048, 2048),
        "require_authentication": True,
        "require_role": "admin",
    },
    # User profile picture uploads
    "/api/v1/users/upload-avatar": {
        "allowed_categories": ["images"],
        "expected_category": "images",
        "max_file_size": 2 * 1024 * 1024,  # 2MB for avatars
        "allowed_extensions": {".jpg", ".jpeg", ".png"},
        "require_image_validation": True,
        "auto_resize": True,
        "max_dimensions": (512, 512),
        "strip_metadata": True,
        "require_authentication": True,
    },
    # Document uploads (invoices, reports, etc.)
    "/api/v1/admin/documents/upload": {
        "allowed_categories": ["documents"],
        "expected_category": "documents",
        "max_file_size": MAX_FILE_SIZES["documents"],
        "allowed_extensions": ALLOWED_EXTENSIONS["documents"],
        "require_authentication": True,
        "require_role": "admin",
    },
}

# =============================================================================
# SECURITY VALIDATION RULES
# =============================================================================

VALIDATION_RULES = {
    # File content validation
    "require_mime_type_verification": True,
    "require_magic_number_check": True,
    "check_embedded_executables": True,
    "scan_for_suspicious_content": True,
    # Path security
    "prevent_path_traversal": True,
    "sanitize_filenames": True,
    "generate_unique_names": True,
    "use_date_based_folders": True,
    # Image-specific security
    "strip_exif_data": True,
    "validate_image_dimensions": True,
    "check_for_polyglot_files": True,
    "verify_image_integrity": True,
    # Document-specific security
    "scan_for_macros": True,
    "check_for_embedded_scripts": True,
    "validate_document_structure": True,
}

# =============================================================================
# MONITORING AND LOGGING
# =============================================================================

MONITORING_CONFIG = {
    # Log all upload attempts
    "log_all_uploads": True,
    "log_validation_failures": True,
    "log_security_warnings": True,
    # Alert thresholds
    "failed_upload_threshold": 10,  # Alert after 10 failed uploads from same IP
    "suspicious_file_threshold": 3,  # Alert after 3 suspicious files from same user
    # Audit settings
    "audit_all_uploads": True,
    "retain_upload_logs_days": 90,
    "alert_on_forbidden_extensions": True,
}

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def get_endpoint_config(endpoint_path: str) -> Dict[str, Any]:
    """Get security configuration for specific endpoint"""
    return ENDPOINT_CONFIGS.get(endpoint_path, {})


def is_extension_allowed(extension: str, category: str = None) -> bool:
    """Check if file extension is allowed for given category"""
    extension = extension.lower()

    # Always forbid dangerous extensions
    if extension in FORBIDDEN_EXTENSIONS:
        return False

    # Check category-specific allowlist
    if category and category in ALLOWED_EXTENSIONS:
        return extension in ALLOWED_EXTENSIONS[category]

    # Check if extension is in any allowed category
    for cat_extensions in ALLOWED_EXTENSIONS.values():
        if extension in cat_extensions:
            return True

    return False


def get_max_file_size(category: str = None, endpoint: str = None) -> int:
    """Get maximum file size for category or endpoint"""
    if endpoint and endpoint in ENDPOINT_CONFIGS:
        return ENDPOINT_CONFIGS[endpoint].get("max_file_size", MAX_FILE_SIZES["default"])

    if category and category in MAX_FILE_SIZES:
        return MAX_FILE_SIZES[category]

    return MAX_FILE_SIZES["default"]


def should_enable_enhanced_validation() -> bool:
    """Check if enhanced validation should be enabled"""
    return ENABLE_ENHANCED_VALIDATION


def get_allowed_extensions(category: str = None) -> Set[str]:
    """Get allowed extensions for category"""
    if category and category in ALLOWED_EXTENSIONS:
        return ALLOWED_EXTENSIONS[category]

    # Return all allowed extensions
    all_extensions = set()
    for extensions in ALLOWED_EXTENSIONS.values():
        all_extensions.update(extensions)

    return all_extensions


# =============================================================================
# RUNTIME VALIDATION
# =============================================================================


def validate_config():
    """Validate security configuration at startup"""
    errors = []

    # Check that forbidden extensions don't overlap with allowed
    for category, allowed in ALLOWED_EXTENSIONS.items():
        overlap = allowed.intersection(FORBIDDEN_EXTENSIONS)
        if overlap:
            errors.append(f"Category '{category}' has forbidden extensions: {overlap}")

    # Check file size limits are reasonable
    for category, size in MAX_FILE_SIZES.items():
        if size > 1024 * 1024 * 1024:  # 1GB
            errors.append(f"File size limit for '{category}' is too large: {size}")
        if size <= 0:
            errors.append(f"File size limit for '{category}' must be positive: {size}")

    # Validate endpoint configurations
    for endpoint, config in ENDPOINT_CONFIGS.items():
        if "max_file_size" in config and config["max_file_size"] <= 0:
            errors.append(f"Invalid max_file_size for endpoint '{endpoint}'")

    if errors:
        raise ValueError(f"File upload security configuration errors: {'; '.join(errors)}")


# Validate configuration on import
validate_config()
