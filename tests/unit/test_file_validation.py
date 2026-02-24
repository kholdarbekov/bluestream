"""Unit tests for file upload validation utilities."""

import importlib
import io
import re
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from business_app.utils.exceptions import FileValidationError


def _png_bytes(width=10, height=10):
    buffer = io.BytesIO()
    image = Image.new("RGB", (width, height), color="red")
    image.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


@pytest.fixture
def file_validation_module(app):
    module_name = "business_app.utils.file_validation"
    with app.app_context():
        if module_name in sys.modules:
            module = importlib.reload(sys.modules[module_name])
        else:
            module = importlib.import_module(module_name)
        return module


@pytest.mark.unit
class TestFileValidator:
    def test_validate_filename_rejects_invalid_and_dangerous_names(self, file_validation_module):
        validator = file_validation_module.FileValidator()

        with pytest.raises(FileValidationError):
            validator.validate_filename("")

        with pytest.raises(FileValidationError):
            validator.validate_filename("../etc/passwd")

        with pytest.raises(FileValidationError):
            validator.validate_filename("payload.py")

        with pytest.raises(FileValidationError):
            validator.validate_filename("shell.php.jpg")

    def test_validate_filename_allows_category_and_sanitizes(self, file_validation_module):
        validator = file_validation_module.FileValidator()

        safe = validator.validate_filename("My Product Image!!.PNG", allowed_categories=["images"])
        assert safe.endswith(".PNG") or safe.endswith(".png")
        assert " " not in safe

        with pytest.raises(FileValidationError):
            validator.validate_filename("report.pdf", allowed_categories=["images"])

    def test_generate_safe_path_sanitizes_components(self, file_validation_module):
        validator = file_validation_module.FileValidator()
        safe_path = validator.generate_safe_path("avatar.png", "user..uploads/admin?", user_id=42)

        assert ".." not in safe_path
        assert "/42/" in safe_path
        assert safe_path.endswith(".png")
        assert re.match(r"^user_uploads/admin_/42/\d{4}/\d{2}/avatar_[a-f0-9]{8}\.png$", safe_path)

    def test_validate_file_content_detects_mime_mismatch(self, monkeypatch, file_validation_module):
        validator = file_validation_module.FileValidator()
        file_obj = _png_bytes()

        monkeypatch.setattr(file_validation_module.magic, "from_buffer", lambda *_args, **_kwargs: "image/png")

        with pytest.raises(FileValidationError):
            validator.validate_file_content(file_obj, "document.pdf", expected_category="documents")

    def test_validate_file_content_for_image_success(self, monkeypatch, file_validation_module):
        validator = file_validation_module.FileValidator()
        file_obj = _png_bytes()

        monkeypatch.setattr(file_validation_module.magic, "from_buffer", lambda *_args, **_kwargs: "image/png")
        result = validator.validate_file_content(file_obj, "photo.png", expected_category="images")

        assert result["is_safe"] is True
        assert result["mime_type"] == "image/png"
        assert result["file_extension"] == ".png"
        assert result["size"] > 0
        assert result["image_info"]["width"] == 10

    def test_validate_file_content_threat_checks(self, file_validation_module):
        validator = file_validation_module.FileValidator()

        with pytest.raises(FileValidationError):
            validator._check_embedded_threats(b"MZxxxx", {"warnings": []})

        results = {"warnings": []}
        validator._check_embedded_threats(b"<script>alert(1)</script>", results)
        assert any("HTML/JavaScript content detected" in warning for warning in results["warnings"])

    def test_validate_document_content_and_virus_scan_hook(self, file_validation_module):
        validator = file_validation_module.FileValidator()
        results = {"warnings": []}

        header = b"This doc has macros and vba plus javascript: and <script>"
        validator._validate_document_content(header, results)
        assert any("macros" in warning.lower() for warning in results["warnings"])
        assert any("suspicious content" in warning.lower() for warning in results["warnings"])

        file_obj = io.BytesIO(b"safe file")
        validator._scan_for_viruses(file_obj, results)
        assert results["virus_scanned"] is True

    def test_validate_upload_file_orchestrates_filename_content_and_path(self, monkeypatch, file_validation_module):
        fake_results = {"mime_type": "text/plain", "file_extension": ".txt", "size": 10, "is_safe": True, "warnings": []}
        file_obj = io.BytesIO(b"hello world")

        monkeypatch.setattr(file_validation_module.file_validator, "validate_filename", lambda *_args, **_kwargs: "safe.txt")
        monkeypatch.setattr(file_validation_module.file_validator, "validate_file_content", lambda *_args, **_kwargs: fake_results)
        monkeypatch.setattr(file_validation_module.file_validator, "generate_safe_path", lambda *_args, **_kwargs: "general/safe.txt")

        output = file_validation_module.validate_upload_file(
            file_obj,
            "original.txt",
            allowed_categories=["documents"],
            expected_category="documents",
        )

        assert output["original_filename"] == "original.txt"
        assert output["safe_filename"] == "safe.txt"
        assert output["safe_path"] == "general/safe.txt"
        assert output["validation_results"] == fake_results

    def test_internal_helpers(self, file_validation_module):
        validator = file_validation_module.FileValidator()
        assert validator._contains_path_traversal("../x") is True
        assert validator._contains_path_traversal("uploads/images/file.png") is False
        assert validator._extract_extension("archive.tar.gz") == ".gz"
        assert validator._sanitize_path_component("..bad/path?") == "_bad_path_"
        assert validator._get_max_file_size("images", ".png") == validator.MAX_FILE_SIZES["images"]
        assert validator._get_max_file_size(None, ".unknown") == validator.MAX_FILE_SIZES["default"]
