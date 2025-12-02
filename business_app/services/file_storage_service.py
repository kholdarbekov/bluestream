"""
File storage service for the Water Business Platform
Supports both local and cloud (S3) storage
"""
import os
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any, BinaryIO
from urllib.parse import urljoin
from flask import current_app, url_for
from werkzeug.utils import secure_filename
from PIL import Image
import boto3
from botocore.exceptions import ClientError, NoCredentialsError

from business_app.utils.exceptions import FileStorageError, ConfigurationError
from business_app.utils.helpers import generate_file_path, sanitize_filename, get_file_extension


class FileStorageService:
    """Service for handling file uploads and storage"""
    
    def __init__(self):
        self.storage_type = current_app.config.get('STORAGE_TYPE', 'local')
        self.upload_folder = current_app.config.get('UPLOAD_FOLDER', 'uploads/')
        self.max_file_size = current_app.config.get('MAX_CONTENT_LENGTH', 16 * 1024 * 1024)
        self.allowed_extensions = current_app.config.get('ALLOWED_EXTENSIONS', set())
        
        # Initialize storage backend
        if self.storage_type == 's3':
            self._init_s3_client()
        else:
            self._init_local_storage()
    
    def _init_s3_client(self):
        """Initialize S3 client"""
        try:
            self.s3_client = boto3.client(
                's3',
                aws_access_key_id=current_app.config.get('AWS_ACCESS_KEY_ID'),
                aws_secret_access_key=current_app.config.get('AWS_SECRET_ACCESS_KEY'),
                region_name=current_app.config.get('AWS_REGION', 'us-east-1')
            )
            self.s3_bucket = current_app.config.get('AWS_S3_BUCKET')
            
            if not self.s3_bucket:
                raise ConfigurationError("AWS S3 bucket not configured")
            
        except Exception as e:
            raise ConfigurationError(f"Failed to initialize S3 client: {e}")
    
    def _init_local_storage(self):
        """Initialize local storage"""
        self.upload_path = os.path.join(current_app.root_path, self.upload_folder)
        
        # Create upload directory if it doesn't exist
        os.makedirs(self.upload_path, exist_ok=True)
        
        # Create subdirectories
        subdirs = ['images', 'documents', 'delivery_photos', 'user_avatars', 'temp']
        for subdir in subdirs:
            os.makedirs(os.path.join(self.upload_path, subdir), exist_ok=True)
    
    def upload_file(self, file: BinaryIO, filename: str, folder: str = 'general',
                   user_id: int = None, skip_validation: bool = False, **metadata) -> Dict[str, Any]:
        """
        Upload file to storage

        Args:
            file: File object to upload
            filename: Original filename
            folder: Storage folder
            user_id: User ID for organizing files
            skip_validation: Skip validation (for already-validated processed files)
            **metadata: Additional metadata

        Returns:
            Dictionary with file information

        Raises:
            FileStorageError: If upload fails
        """
        try:
            # Validate file with enhanced security (unless already validated)
            if not skip_validation:
                validation_result = self._validate_file(file, filename, folder)

                # Use validated information
                file_info = {
                    'original_filename': validation_result['original_filename'],
                    'filename': validation_result['safe_filename'],
                    'file_path': validation_result['safe_path'],
                    'folder': folder,
                    'extension': validation_result['validation_results']['file_extension'],
                    'user_id': user_id,
                    'validation_results': validation_result['validation_results']
                }
            else:
                # For processed files (BytesIO), skip validation and generate path
                from business_app.utils.file_validation import file_validator
                safe_path = file_validator.generate_safe_path(filename, folder, user_id)
                file_ext = os.path.splitext(filename)[1].lower()

                file_info = {
                    'original_filename': filename,
                    'filename': os.path.basename(safe_path),
                    'file_path': safe_path,
                    'folder': folder,
                    'extension': file_ext,
                    'user_id': user_id,
                    'validation_results': {'already_validated': True}
                }

            # Upload based on storage type
            if self.storage_type == 's3':
                return self._upload_to_s3(file, file_info, metadata)
            else:
                return self._upload_to_local(file, file_info, metadata)

        except Exception as e:
            raise FileStorageError(f"File upload failed: {e}")
    
    def upload_image(self, file: BinaryIO, filename: str, folder: str = 'images',
                    user_id: int = None, resize: bool = True, max_width: int = 1920,
                    max_height: int = 1080, quality: int = 85) -> Dict[str, Any]:
        """
        Upload and process image file
        
        Args:
            file: Image file object
            filename: Original filename
            folder: Storage folder
            user_id: User ID
            resize: Whether to resize image
            max_width: Maximum width for resizing
            max_height: Maximum height for resizing
            quality: JPEG quality (1-100)
        
        Returns:
            Dictionary with image information including thumbnails
        """
        try:
            # Validate image file with enhanced security
            validation_result = self._validate_file(file, filename, 'images')
            
            # Process image
            processed_image = self._process_image(
                file, resize, max_width, max_height, quality
            )
            
            # Upload original/processed image (skip validation - already validated above)
            file_info = self.upload_file(
                processed_image['main'], filename, folder, user_id,
                skip_validation=True
            )

            # Upload thumbnails (skip validation - already validated)
            thumbnails = {}
            for size_name, thumbnail_data in processed_image.get('thumbnails', {}).items():
                thumb_filename = f"thumb_{size_name}_{filename}"
                thumb_info = self.upload_file(
                    thumbnail_data, thumb_filename, f"{folder}/thumbnails", user_id,
                    skip_validation=True
                )
                thumbnails[size_name] = thumb_info
            
            file_info['thumbnails'] = thumbnails
            file_info['is_image'] = True
            
            return file_info
            
        except Exception as e:
            raise FileStorageError(f"Image upload failed: {e}")
    
    def delete_file(self, file_path: str) -> bool:
        """
        Delete file from storage
        
        Args:
            file_path: File path to delete
        
        Returns:
            Success status
        """
        try:
            if self.storage_type == 's3':
                return self._delete_from_s3(file_path)
            else:
                return self._delete_from_local(file_path)
        except Exception as e:
            current_app.logger.error(f"File deletion failed: {e}")
            return False
    
    def get_file_url(self, file_path: str, expires_in: int = 3600) -> str:
        """
        Get file URL
        
        Args:
            file_path: File path
            expires_in: URL expiration time in seconds (for S3)
        
        Returns:
            File URL
        """
        if self.storage_type == 's3':
            return self._get_s3_url(file_path, expires_in)
        else:
            return self._get_local_url(file_path)
    
    def get_file_info(self, file_path: str) -> Optional[Dict[str, Any]]:
        """
        Get file information
        
        Args:
            file_path: File path
        
        Returns:
            File information dictionary or None
        """
        try:
            if self.storage_type == 's3':
                return self._get_s3_file_info(file_path)
            else:
                return self._get_local_file_info(file_path)
        except Exception:
            return None
    
    def copy_file(self, source_path: str, dest_path: str) -> bool:
        """Copy file to new location"""
        try:
            if self.storage_type == 's3':
                return self._copy_s3_file(source_path, dest_path)
            else:
                return self._copy_local_file(source_path, dest_path)
        except Exception as e:
            current_app.logger.error(f"File copy failed: {e}")
            return False
    
    def move_file(self, source_path: str, dest_path: str) -> bool:
        """Move file to new location"""
        try:
            if self.copy_file(source_path, dest_path):
                return self.delete_file(source_path)
            return False
        except Exception as e:
            current_app.logger.error(f"File move failed: {e}")
            return False
    
    # Private methods for validation
    def _validate_file(self, file: BinaryIO, filename: str, expected_category: str = None):
        """Enhanced file validation with comprehensive security checks"""
        from business_app.utils.file_validation import validate_upload_file, FileValidationError

        try:
            # Use comprehensive validation
            validation_result = validate_upload_file(
                file=file,
                filename=filename,
                allowed_categories=None,  # Will use service config
                expected_category=expected_category
            )
            
            # Check against service-specific allowed extensions if configured
            if self.allowed_extensions:
                extension = validation_result['validation_results']['file_extension'].split('.')[-1].lower()
                if extension not in self.allowed_extensions:
                    raise FileStorageError(
                        f"File type '{extension}' not allowed by service configuration. "
                        f"Allowed types: {', '.join(self.allowed_extensions)}"
                    )
            
            # Check against service-specific file size limit
            file_size = validation_result['validation_results']['size']
            if file_size > self.max_file_size:
                raise FileStorageError(
                    f"File too large. Service maximum size: {self.max_file_size / (1024*1024):.1f}MB"
                )
            
            # Log any warnings
            warnings = validation_result['validation_results'].get('warnings', [])
            if warnings:
                current_app.logger.warning(f"File upload warnings for {filename}: {warnings}")
            
            return validation_result
            
        except FileValidationError as e:
            raise FileStorageError(f"File validation failed: {e}")
        except Exception as e:
            current_app.logger.error(f"File validation error: {e}")
            raise FileStorageError(f"File validation failed: {e}")
    
    def _validate_image_file(self, file: BinaryIO, filename: str):
        """Validate image file"""
        self._validate_file(file, filename)
        
        # Additional image validation
        try:
            file.seek(0)
            with Image.open(file) as img:
                img.verify()
            file.seek(0)
        except Exception:
            raise FileStorageError("Invalid image file")
    
    def _generate_file_info(self, filename: str, folder: str, user_id: int) -> Dict[str, str]:
        """Generate file information"""
        # Sanitize filename
        safe_filename = secure_filename(sanitize_filename(filename))
        
        # Generate unique filename
        name, ext = os.path.splitext(safe_filename)
        unique_filename = f"{name}_{uuid.uuid4().hex[:8]}{ext}"
        
        # Generate file path
        if user_id:
            file_path = f"{folder}/{user_id}/{unique_filename}"
        else:
            file_path = f"{folder}/{unique_filename}"
        
        return {
            'original_filename': filename,
            'filename': unique_filename,
            'file_path': file_path,
            'folder': folder,
            'extension': ext.lower(),
            'user_id': user_id
        }
    
    def _process_image(self, file: BinaryIO, resize: bool, max_width: int,
                      max_height: int, quality: int) -> Dict[str, Any]:
        """Process image (resize, create thumbnails)"""
        file.seek(0)
        
        with Image.open(file) as img:
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'LA', 'P'):
                img = img.convert('RGB')
            
            processed = {'main': None, 'thumbnails': {}}
            
            # Resize main image if needed
            if resize and (img.width > max_width or img.height > max_height):
                img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
            
            # Save main image
            from io import BytesIO
            main_buffer = BytesIO()
            img.save(main_buffer, format='JPEG', quality=quality, optimize=True)
            main_buffer.seek(0)
            processed['main'] = main_buffer
            
            # Create thumbnails
            thumbnail_sizes = {
                'small': (150, 150),
                'medium': (300, 300),
                'large': (600, 600)
            }
            
            for size_name, (thumb_width, thumb_height) in thumbnail_sizes.items():
                if img.width > thumb_width or img.height > thumb_height:
                    thumb_img = img.copy()
                    thumb_img.thumbnail((thumb_width, thumb_height), Image.Resampling.LANCZOS)
                    
                    thumb_buffer = BytesIO()
                    thumb_img.save(thumb_buffer, format='JPEG', quality=quality, optimize=True)
                    thumb_buffer.seek(0)
                    processed['thumbnails'][size_name] = thumb_buffer
            
            return processed
    
    # Local storage methods
    def _upload_to_local(self, file: BinaryIO, file_info: Dict[str, str],
                        metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Upload file to local storage"""
        file_path = os.path.join(self.upload_path, file_info['file_path'])
        
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        # Save file
        with open(file_path, 'wb') as f:
            file.seek(0)
            f.write(file.read())
        
        # Get file stats
        stat = os.stat(file_path)
        
        return {
            'file_path': file_info['file_path'],
            'filename': file_info['filename'],
            'original_filename': file_info['original_filename'],
            'size': stat.st_size,
            'content_type': self._get_content_type(file_info['extension']),
            'storage_type': 'local',
            'url': self._get_local_url(file_info['file_path']),
            'uploaded_at': datetime.now(timezone.utc).isoformat(),
            **metadata
        }
    
    def _delete_from_local(self, file_path: str) -> bool:
        """Delete file from local storage"""
        full_path = os.path.join(self.upload_path, file_path)
        if os.path.exists(full_path):
            os.remove(full_path)
            return True
        return False
    
    def _get_local_url(self, file_path: str) -> str:
        """Get local file URL"""
        from flask import request, has_request_context
        import os

        # Check if we have a configured base URL (for production/staging)
        base_url = current_app.config.get('BASE_URL')
        if base_url:
            url = f"{base_url}/uploads/{file_path}"
            current_app.logger.info(f"Generated URL from BASE_URL config: {url}")
            return url

        # Try to use request context for accurate URL generation
        if has_request_context():
            # Check for forwarded host (proxy scenarios)
            host = request.headers.get('X-Forwarded-Host') or request.host
            scheme = request.headers.get('X-Forwarded-Proto') or request.scheme

            # Log all relevant headers for debugging
            current_app.logger.info(f"Request headers - Host: {request.host}, X-Forwarded-Host: {request.headers.get('X-Forwarded-Host')}, Referer: {request.headers.get('Referer')}")

            url = f"{scheme}://{host}/uploads/{file_path}"
            current_app.logger.info(f"Generated URL from request context - scheme: {scheme}, host: {host}, url: {url}")
            return url
        else:
            url = url_for('uploaded_file', filename=file_path, _external=True)
            current_app.logger.info(f"Generated URL from url_for (no request context): {url}")
            return url
    
    def _get_local_file_info(self, file_path: str) -> Dict[str, Any]:
        """Get local file information"""
        full_path = os.path.join(self.upload_path, file_path)
        if not os.path.exists(full_path):
            return None
        
        stat = os.stat(full_path)
        return {
            'file_path': file_path,
            'size': stat.st_size,
            'modified_at': datetime.fromtimestamp(stat.st_mtime).isoformat(),
            'storage_type': 'local',
            'url': self._get_local_url(file_path)
        }
    
    def _copy_local_file(self, source_path: str, dest_path: str) -> bool:
        """Copy local file"""
        import shutil
        source_full = os.path.join(self.upload_path, source_path)
        dest_full = os.path.join(self.upload_path, dest_path)
        
        if os.path.exists(source_full):
            os.makedirs(os.path.dirname(dest_full), exist_ok=True)
            shutil.copy2(source_full, dest_full)
            return True
        return False
    
    # S3 storage methods
    def _upload_to_s3(self, file: BinaryIO, file_info: Dict[str, str],
                     metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Upload file to S3"""
        try:
            file.seek(0)
            
            extra_args = {
                'ContentType': self._get_content_type(file_info['extension']),
                'Metadata': {k: str(v) for k, v in metadata.items()}
            }
            
            self.s3_client.upload_fileobj(
                file, self.s3_bucket, file_info['file_path'], ExtraArgs=extra_args
            )
            
            # Get file size
            file.seek(0, 2)
            size = file.tell()
            
            return {
                'file_path': file_info['file_path'],
                'filename': file_info['filename'],
                'original_filename': file_info['original_filename'],
                'size': size,
                'content_type': extra_args['ContentType'],
                'storage_type': 's3',
                'bucket': self.s3_bucket,
                'url': self._get_s3_url(file_info['file_path']),
                'uploaded_at': datetime.now(timezone.utc).isoformat(),
                **metadata
            }
            
        except ClientError as e:
            raise FileStorageError(f"S3 upload failed: {e}")
    
    def _delete_from_s3(self, file_path: str) -> bool:
        """Delete file from S3"""
        try:
            self.s3_client.delete_object(Bucket=self.s3_bucket, Key=file_path)
            return True
        except ClientError:
            return False
    
    def _get_s3_url(self, file_path: str, expires_in: int = 3600) -> str:
        """Get S3 file URL"""
        try:
            return self.s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': self.s3_bucket, 'Key': file_path},
                ExpiresIn=expires_in
            )
        except ClientError:
            return ''
    
    def _get_s3_file_info(self, file_path: str) -> Dict[str, Any]:
        """Get S3 file information"""
        try:
            response = self.s3_client.head_object(Bucket=self.s3_bucket, Key=file_path)
            return {
                'file_path': file_path,
                'size': response['ContentLength'],
                'content_type': response.get('ContentType'),
                'modified_at': response['LastModified'].isoformat(),
                'storage_type': 's3',
                'bucket': self.s3_bucket,
                'url': self._get_s3_url(file_path)
            }
        except ClientError:
            return None
    
    def _copy_s3_file(self, source_path: str, dest_path: str) -> bool:
        """Copy S3 file"""
        try:
            copy_source = {'Bucket': self.s3_bucket, 'Key': source_path}
            self.s3_client.copy_object(
                CopySource=copy_source, Bucket=self.s3_bucket, Key=dest_path
            )
            return True
        except ClientError:
            return False
    
    def _get_content_type(self, extension: str) -> str:
        """Get content type from file extension"""
        content_types = {
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.png': 'image/png',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.pdf': 'application/pdf',
            '.doc': 'application/msword',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.txt': 'text/plain',
            '.csv': 'text/csv',
            '.json': 'application/json',
            '.xml': 'application/xml',
            '.zip': 'application/zip',
            '.rar': 'application/x-rar-compressed'
        }
        
        return content_types.get(extension.lower(), 'application/octet-stream')


# Global file storage service instance removed to avoid application context issues
# Use get_file_storage_service() function from API files instead