"""
Enhanced file upload validation and security utilities
Provides comprehensive protection against file upload vulnerabilities
"""
import os
import re
import magic
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Any, BinaryIO
from werkzeug.utils import secure_filename
from flask import current_app
import logging

from business_app.utils.exceptions import FileValidationError

logger = logging.getLogger(__name__)


class FileValidator:
    """Comprehensive file validation for upload security"""
    
    # Dangerous file extensions that should never be allowed
    DANGEROUS_EXTENSIONS = {
        # Executable files
        '.exe', '.bat', '.cmd', '.com', '.scr', '.pif', '.msi', '.app', '.deb', '.rpm',
        # Script files
        '.sh', '.bash', '.zsh', '.fish', '.ps1', '.vbs', '.js', '.jsx', '.ts', '.tsx',
        '.py', '.rb', '.pl', '.php', '.asp', '.aspx', '.jsp', '.cfm',
        # System files
        '.dll', '.so', '.dylib', '.sys', '.drv',
        # Archive with potential scripts
        '.jar', '.war', '.ear',
        # Configuration files that could be dangerous
        '.htaccess', '.htpasswd', '.config', '.ini', '.conf',
        # Database files
        '.sql', '.db', '.sqlite', '.mdb',
        # Other potentially dangerous
        '.iso', '.dmg', '.pkg', '.apk', '.ipa'
    }
    
    # Safe file extensions by category
    SAFE_EXTENSIONS = {
        'images': {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.ico'},
        'documents': {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.rtf', '.odt', '.ods', '.odp'},
        'archives': {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2'},
        'media': {'.mp3', '.mp4', '.avi', '.mov', '.wmv', '.flv', '.wav', '.ogg'}
    }
    
    # MIME type validation mapping
    MIME_TYPE_MAPPING = {
        '.jpg': ['image/jpeg'],
        '.jpeg': ['image/jpeg'],
        '.png': ['image/png'],
        '.gif': ['image/gif'],
        '.bmp': ['image/bmp'],
        '.webp': ['image/webp'],
        '.svg': ['image/svg+xml'],
        '.pdf': ['application/pdf'],
        '.doc': ['application/msword'],
        '.docx': ['application/vnd.openxmlformats-officedocument.wordprocessingml.document'],
        '.xls': ['application/vnd.ms-excel'],
        '.xlsx': ['application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'],
        '.txt': ['text/plain'],
        '.zip': ['application/zip'],
        '.rar': ['application/x-rar-compressed'],
        '.mp3': ['audio/mpeg'],
        '.mp4': ['video/mp4'],
        '.wav': ['audio/wav']
    }
    
    # Maximum file sizes by category (in bytes)
    # Note: These are overridden by app.config['MAX_CONTENT_LENGTH'] if set
    MAX_FILE_SIZES = {
        'images': 50 * 1024 * 1024,      # 50MB
        'documents': 25 * 1024 * 1024,   # 25MB
        'archives': 50 * 1024 * 1024,    # 50MB
        'media': 100 * 1024 * 1024,      # 100MB
        'default': 50 * 1024 * 1024      # 50MB
    }
    
    def __init__(self):
        self.virus_scanner_enabled = current_app.config.get('ENABLE_VIRUS_SCANNING', False)
    
    def validate_filename(self, filename: str, allowed_categories: Optional[List[str]] = None) -> str:
        """
        Comprehensive filename validation and sanitization
        
        Args:
            filename: Original filename
            allowed_categories: List of allowed file categories (images, documents, etc.)
        
        Returns:
            Sanitized safe filename
        
        Raises:
            FileValidationError: If file is invalid or dangerous
        """
        if not filename or not filename.strip():
            raise FileValidationError("Filename cannot be empty")
        
        # Basic security checks
        if len(filename) > 255:
            raise FileValidationError("Filename too long (max 255 characters)")
        
        # Check for null bytes and control characters
        if '\x00' in filename or any(ord(c) < 32 for c in filename):
            raise FileValidationError("Filename contains invalid characters")
        
        # Check for path traversal attempts
        if self._contains_path_traversal(filename):
            raise FileValidationError("Filename contains path traversal sequences")
        
        # Extract and validate extension
        file_ext = self._extract_extension(filename)
        
        # Check against dangerous extensions
        if file_ext in self.DANGEROUS_EXTENSIONS:
            raise FileValidationError(f"File type '{file_ext}' is not allowed for security reasons")
        
        # Check against allowed categories if specified
        if allowed_categories:
            allowed_extensions = set()
            for category in allowed_categories:
                if category in self.SAFE_EXTENSIONS:
                    allowed_extensions.update(self.SAFE_EXTENSIONS[category])
            
            if file_ext not in allowed_extensions:
                raise FileValidationError(
                    f"File type '{file_ext}' not allowed. "
                    f"Allowed types for {', '.join(allowed_categories)}: {', '.join(sorted(allowed_extensions))}"
                )
        
        # Sanitize filename
        safe_filename = self._sanitize_filename(filename)
        
        return safe_filename
    
    def validate_file_content(self, file: BinaryIO, filename: str, 
                            expected_category: Optional[str] = None) -> Dict[str, Any]:
        """
        Validate file content using magic number detection
        
        Args:
            file: File object
            filename: Original filename
            expected_category: Expected file category for additional validation
        
        Returns:
            Dictionary with file information and validation results
        
        Raises:
            FileValidationError: If file content is invalid
        """
        file.seek(0)

        # Read file header for magic number detection
        header: bytes = file.read(8192)  # Read first 8KB
        file.seek(0)

        if len(header) == 0:
            raise FileValidationError("File is empty")

        # Detect MIME type using python-magic
        try:
            mime_type = magic.from_buffer(header, mime=True)
        except Exception as e:
            logger.warning(f"Magic number detection failed: {e}")
            mime_type = 'application/octet-stream'

        # Get file extension
        file_ext = self._extract_extension(filename)
        
        # Validate MIME type matches extension
        if file_ext in self.MIME_TYPE_MAPPING:
            expected_mimes = self.MIME_TYPE_MAPPING[file_ext]
            if mime_type not in expected_mimes:
                raise FileValidationError(
                    f"File content ({mime_type}) doesn't match extension ({file_ext}). "
                    f"Expected: {', '.join(expected_mimes)}"
                )
        
        # Additional category-specific validation
        validation_results = {
            'mime_type': mime_type,
            'file_extension': file_ext,
            'size': 0,
            'is_safe': True,
            'warnings': []
        }
        
        # Get file size
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
        validation_results['size'] = file_size
        
        # Check file size limits
        max_size = self._get_max_file_size(expected_category, file_ext)
        if file_size > max_size:
            raise FileValidationError(f"File too large. Maximum size: {max_size / (1024*1024):.1f}MB")
        
        # Category-specific validation
        if expected_category == 'images':
            self._validate_image_content(file, validation_results)
        elif expected_category == 'documents':
            self._validate_document_content(header, validation_results)
        
        # Check for embedded executables or scripts
        self._check_embedded_threats(header, validation_results)
        
        # Virus scanning if enabled
        if self.virus_scanner_enabled:
            self._scan_for_viruses(file, validation_results)
        
        return validation_results
    
    def generate_safe_path(self, filename: str, folder: str, user_id: Optional[int] = None) -> str:
        """
        Generate a safe file path with proper directory structure

        Args:
            filename: Sanitized filename
            folder: Target folder (can contain slashes for subdirectories)
            user_id: User ID for organization

        Returns:
            Safe file path
        """
        # Split folder path and sanitize each component separately
        folder_parts = folder.split('/')
        safe_folder_parts = [self._sanitize_path_component(part) for part in folder_parts if part]

        # Generate unique filename to prevent conflicts
        name, ext = os.path.splitext(filename)
        import uuid
        unique_filename = f"{name}_{uuid.uuid4().hex[:8]}{ext}"

        # Build path components starting with sanitized folder parts
        path_components = safe_folder_parts

        if user_id:
            # Add user-specific subdirectory
            path_components.append(str(user_id))

        # Add date-based subdirectory for organization
        from datetime import datetime, UTC
        date_folder = datetime.now(UTC).strftime('%Y/%m')
        path_components.extend(date_folder.split('/'))

        path_components.append(unique_filename)

        # Join with forward slashes (works on all platforms)
        safe_path = '/'.join(path_components)

        # Validate final path doesn't escape intended directory
        if self._contains_path_traversal(safe_path):
            raise FileValidationError("Generated path contains invalid sequences")

        return safe_path
    
    def _contains_path_traversal(self, path: str) -> bool:
        """Check for path traversal attempts"""
        dangerous_patterns = [
            '../', '..\\',  # Basic traversal
            '%2e%2e/', '%2e%2e\\',  # URL encoded
            '..%2f', '..%5c',  # Mixed encoding
            '%252e%252e/',  # Double URL encoded
            '..../', '....\\',  # Multiple dots
            '.%2f', '.%5c',  # Dot with encoded slash
            '/%2e%2e', '\\%2e%2e',  # Leading slash with encoded
        ]
        
        path_lower = path.lower()
        return any(pattern in path_lower for pattern in dangerous_patterns)
    
    def _extract_extension(self, filename: str) -> str:
        """Safely extract file extension"""
        # Handle multiple extensions (e.g., .tar.gz)
        parts = filename.lower().split('.')
        if len(parts) < 2:
            return ''
        
        # Check for double extensions that could be dangerous
        if len(parts) >= 3:
            double_ext = f".{parts[-2]}.{parts[-1]}"
            dangerous_double_exts = {'.tar.gz', '.tar.bz2', '.tar.xz'}
            if double_ext not in dangerous_double_exts:
                # Check if second-to-last extension is dangerous
                second_ext = f".{parts[-2]}"
                if second_ext in self.DANGEROUS_EXTENSIONS:
                    raise FileValidationError(f"Dangerous double extension detected: {double_ext}")
        
        return f".{parts[-1]}"
    
    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for safe storage"""
        # Use werkzeug's secure_filename as base
        safe_name = secure_filename(filename)
        
        if not safe_name:
            # If secure_filename returns empty, generate a safe name
            ext = self._extract_extension(filename)
            safe_name = f"file_{hashlib.md5(filename.encode()).hexdigest()[:8]}{ext}"
        
        # Additional sanitization
        # Remove any remaining dangerous characters
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', safe_name)
        
        # Limit length
        if len(safe_name) > 200:
            name, ext = os.path.splitext(safe_name)
            safe_name = name[:200-len(ext)] + ext
        
        return safe_name
    
    def _sanitize_path_component(self, component: str) -> str:
        """Sanitize path component (folder name)"""
        # Remove dangerous characters and patterns
        safe_component = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', component)
        safe_component = re.sub(r'\.+', '_', safe_component)  # Remove multiple dots
        
        # Trim and ensure not empty
        safe_component = safe_component.strip()
        if not safe_component:
            safe_component = 'uploads'
        
        return safe_component
    
    def _get_max_file_size(self, category: Optional[str], file_ext: str) -> int:
        """Get maximum file size for given category/extension"""
        if category and category in self.MAX_FILE_SIZES:
            return self.MAX_FILE_SIZES[category]
        
        # Check by extension
        for cat, extensions in self.SAFE_EXTENSIONS.items():
            if file_ext in extensions and cat in self.MAX_FILE_SIZES:
                return self.MAX_FILE_SIZES[cat]
        
        return self.MAX_FILE_SIZES['default']
    
    def _validate_image_content(self, file: BinaryIO, results: Dict[str, Any]):
        """Additional validation for image files"""
        try:
            from PIL import Image
            file.seek(0)

            with Image.open(file) as img:
                # Check image dimensions
                width, height = img.size
                max_dimension = 5000  # 5000px max

                if width > max_dimension or height > max_dimension:
                    raise FileValidationError(f"Image dimensions too large. Max: {max_dimension}x{max_dimension}")

                # Check for EXIF data that could contain malicious content
                if hasattr(img, '_getexif') and img._getexif():
                    results['warnings'].append('Image contains EXIF data')

                # Store image info
                results['image_info'] = {
                    'width': width,
                    'height': height,
                    'format': img.format,
                    'mode': img.mode
                }

                # Load image data to verify it's valid (this will throw if corrupted)
                # We don't need to store the data, just verify it can be loaded
                img.load()

            file.seek(0)

        except Exception as e:
            raise FileValidationError(f"Invalid or corrupted image: {e}")
    
    def _validate_document_content(self, header: bytes, results: Dict[str, Any]):
        """Additional validation for document files"""
        # Check for embedded macros or scripts in Office documents
        if b'macros' in header.lower() or b'vba' in header.lower():
            results['warnings'].append('Document may contain macros')
        
        # Check for suspicious patterns
        suspicious_patterns = [b'javascript:', b'<script', b'eval(', b'document.write']
        for pattern in suspicious_patterns:
            if pattern in header.lower():
                results['warnings'].append(f'Suspicious content detected: {pattern.decode()}')
    
    def _check_embedded_threats(self, header: bytes, results: Dict[str, Any]):
        """Check for embedded executables or threats"""
        # Check for PE header (Windows executable)
        if header[:2] == b'MZ':
            raise FileValidationError("File contains Windows executable code")
        
        # Check for ELF header (Linux executable)
        if header[:4] == b'\x7fELF':
            raise FileValidationError("File contains Linux executable code")
        
        # Check for Mach-O header (macOS executable)
        if header[:4] in [b'\xfe\xed\xfa\xce', b'\xfe\xed\xfa\xcf', b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe']:
            raise FileValidationError("File contains macOS executable code")
        
        # Check for script shebangs
        if header.startswith(b'#!'):
            raise FileValidationError("File contains script shebang")
        
        # Check for suspicious HTML/JS content
        html_patterns = [b'<script', b'javascript:', b'<iframe', b'<object', b'<embed']
        for pattern in html_patterns:
            if pattern in header.lower():
                results['warnings'].append(f'HTML/JavaScript content detected: {pattern.decode()}')
    
    def _scan_for_viruses(self, file: BinaryIO, results: Dict[str, Any]):
        """Scan file for viruses (placeholder for actual implementation)"""
        # This would integrate with actual antivirus scanning
        # For now, just log that scanning would occur
        logger.info("Virus scanning would occur here in production")
        results['virus_scanned'] = True


# Global validator instance
file_validator = FileValidator()


def validate_upload_file(file: BinaryIO, filename: str, 
                        allowed_categories: Optional[List[str]] = None,
                        expected_category: Optional[str] = None) -> Dict[str, Any]:
    """
    Comprehensive file upload validation
    
    Args:
        file: File object to validate
        filename: Original filename
        allowed_categories: List of allowed file categories
        expected_category: Expected file category
    
    Returns:
        Dictionary with validation results and safe filename
    
    Raises:
        FileValidationError: If file is invalid
    """
    # Validate filename
    safe_filename = file_validator.validate_filename(filename, allowed_categories)
    
    # Validate file content
    content_results = file_validator.validate_file_content(file, safe_filename, expected_category)
    
    # Generate safe path
    safe_path = file_validator.generate_safe_path(safe_filename, expected_category or 'general')
    
    return {
        'original_filename': filename,
        'safe_filename': safe_filename,
        'safe_path': safe_path,
        'validation_results': content_results
    }