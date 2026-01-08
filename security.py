"""
Security utilities for file upload validation and sanitization.

This module provides functions to:
- Validate filenames and prevent path traversal attacks
- Check file sizes against limits
- Validate ZIP files for ZIP bombs and malicious content
- Provide secure file upload handling
"""

import os
import zipfile
from pathlib import Path
from typing import Optional, Tuple
from config import Config


class SecurityError(Exception):
    """Raised when security validation fails."""
    pass


class FileValidator:
    """Validates uploaded files for security threats."""

    @staticmethod
    def validate_filename(filename: str) -> str:
        """
        Validate and sanitize filename.

        Removes path components, checks for dangerous patterns,
        and validates file extension.

        Args:
            filename: Original filename from upload

        Returns:
            Sanitized filename (basename only)

        Raises:
            SecurityError: If filename is invalid or dangerous

        Example:
            >>> FileValidator.validate_filename("../../../etc/passwd")
            SecurityError: Filename contains dangerous pattern: ..

            >>> FileValidator.validate_filename("trajectory.xtc")
            'trajectory.xtc'
        """
        # Remove path components (prevent directory traversal)
        safe_name = Path(filename).name

        # Check for null bytes
        if '\0' in safe_name:
            raise SecurityError("Filename contains null bytes")

        # Check for dangerous patterns
        dangerous_patterns = ['..', '~', '$', '`', '|', ';', '&', '\n', '\r']
        for pattern in dangerous_patterns:
            if pattern in safe_name:
                raise SecurityError(f"Filename contains dangerous pattern: {pattern}")

        # Check if filename is empty
        if not safe_name or safe_name in ('.', '..'):
            raise SecurityError("Invalid filename")

        # Validate extension
        ext = Path(safe_name).suffix.lower()
        if ext not in Config.ALLOWED_UPLOAD_EXTENSIONS:
            raise SecurityError(
                f"File extension '{ext}' not allowed. "
                f"Allowed extensions: {', '.join(sorted(Config.ALLOWED_UPLOAD_EXTENSIONS))}"
            )

        return safe_name

    @staticmethod
    def validate_file_size(file_size: int, max_size: Optional[int] = None) -> None:
        """
        Validate file size against limit.

        Args:
            file_size: Size in bytes
            max_size: Maximum allowed size in bytes (defaults to Config.MAX_UPLOAD_SIZE)

        Raises:
            SecurityError: If file is too large

        Example:
            >>> FileValidator.validate_file_size(1024)  # 1 KB - OK
            >>> FileValidator.validate_file_size(10 * 1024**3)  # 10 GB
            SecurityError: File too large...
        """
        if max_size is None:
            max_size = Config.MAX_UPLOAD_SIZE

        if file_size <= 0:
            raise SecurityError("File size must be positive")

        if file_size > max_size:
            size_mb = file_size / (1024 * 1024)
            max_mb = max_size / (1024 * 1024)
            raise SecurityError(
                f"File too large: {size_mb:.1f} MB exceeds limit of {max_mb:.1f} MB"
            )

    @staticmethod
    def validate_zip_file(zip_path: Path) -> Tuple[int, int]:
        """
        Validate ZIP file for ZIP bombs and path traversal.

        Checks:
        - Path traversal attempts in ZIP entries
        - Compression ratio (detects ZIP bombs)
        - Total uncompressed size

        Args:
            zip_path: Path to ZIP file

        Returns:
            Tuple of (compressed_size, uncompressed_size) in bytes

        Raises:
            SecurityError: If ZIP file is dangerous

        Example:
            >>> FileValidator.validate_zip_file(Path("safe.zip"))
            (1024, 2048)

            >>> FileValidator.validate_zip_file(Path("bomb.zip"))
            SecurityError: Potential ZIP bomb detected...
        """
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                # Check for path traversal in ZIP entries
                for member in zf.namelist():
                    # Normalize path and check for traversal
                    normalized = os.path.normpath(member)

                    # Check for absolute paths or parent directory references
                    if normalized.startswith('..') or normalized.startswith('/') or normalized.startswith('\\'):
                        raise SecurityError(
                            f"ZIP contains path traversal attempt: {member}"
                        )

                    # Check for drive letters on Windows (e.g., C:\)
                    if len(normalized) > 1 and normalized[1] == ':':
                        raise SecurityError(
                            f"ZIP contains absolute path: {member}"
                        )

                # Check compression ratio (ZIP bomb detection)
                compressed_size = sum(info.compress_size for info in zf.infolist())
                uncompressed_size = sum(info.file_size for info in zf.infolist())

                # Prevent division by zero
                if compressed_size == 0:
                    if uncompressed_size > 0:
                        raise SecurityError("ZIP file has suspicious compression ratio")
                    return (0, 0)

                ratio = uncompressed_size / compressed_size

                # Warn if compression ratio > 100:1 (likely ZIP bomb)
                if ratio > 100:
                    raise SecurityError(
                        f"Potential ZIP bomb detected: "
                        f"compression ratio {ratio:.1f}:1 exceeds safe limit of 100:1"
                    )

                # Check uncompressed size
                if uncompressed_size > Config.MAX_ZIP_SIZE:
                    size_gb = uncompressed_size / (1024**3)
                    max_gb = Config.MAX_ZIP_SIZE / (1024**3)
                    raise SecurityError(
                        f"ZIP uncompressed size {size_gb:.2f} GB "
                        f"exceeds limit of {max_gb:.2f} GB"
                    )

                return compressed_size, uncompressed_size

        except zipfile.BadZipFile:
            raise SecurityError("Invalid or corrupted ZIP file")
        except Exception as e:
            if isinstance(e, SecurityError):
                raise
            raise SecurityError(f"Error validating ZIP file: {e}")

    @staticmethod
    def safe_extract_zip(zip_path: Path, extract_to: Path) -> None:
        """
        Safely extract ZIP file with validation.

        Args:
            zip_path: Path to ZIP file
            extract_to: Directory to extract to

        Raises:
            SecurityError: If extraction fails or is unsafe

        Example:
            >>> FileValidator.safe_extract_zip(
            ...     Path("ligands.zip"),
            ...     Path("/tmp/extract")
            ... )
        """
        # Validate ZIP first
        FileValidator.validate_zip_file(zip_path)

        # Create extraction directory
        extract_to.mkdir(parents=True, exist_ok=True)

        # Extract safely
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_to)


def handle_file_upload_secure(uploaded_file, job_id: str, filename_prefix: str = "") -> Path:
    """
    Securely handle file upload with validation.

    This function:
    1. Validates file size
    2. Sanitizes filename
    3. Creates secure upload path
    4. Saves file

    Args:
        uploaded_file: Streamlit UploadedFile object
        job_id: Unique job identifier
        filename_prefix: Optional prefix for filename

    Returns:
        Path to saved file

    Raises:
        ValueError: If no file provided
        SecurityError: If file fails validation

    Example:
        >>> from streamlit.runtime.uploaded_file_manager import UploadedFile
        >>> path = handle_file_upload_secure(uploaded_file, "abc123", "traj_")
        >>> print(path)
        PosixPath('/app/uploads/abc123/traj_trajectory.xtc')
    """
    if uploaded_file is None:
        raise ValueError("No file provided")

    # Validate file size
    file_size = uploaded_file.size if hasattr(uploaded_file, 'size') else len(uploaded_file.getvalue())
    FileValidator.validate_file_size(file_size, Config.MAX_UPLOAD_SIZE)

    # Validate and sanitize filename
    safe_filename = FileValidator.validate_filename(uploaded_file.name)

    # Add prefix if provided
    if filename_prefix:
        safe_filename = f"{filename_prefix}{safe_filename}"

    # Get secure path using Config
    filepath = Config.get_upload_path(job_id, safe_filename)

    # Save file
    with open(filepath, "wb") as f:
        f.write(uploaded_file.getbuffer())

    return filepath


# Convenience function for checking if a path is safe
def is_safe_path(basedir: Path, path: Path) -> bool:
    """
    Check if a path is safely within a base directory.

    Args:
        basedir: Base directory that should contain the path
        path: Path to check

    Returns:
        True if path is safely within basedir

    Example:
        >>> is_safe_path(Path("/app/uploads"), Path("/app/uploads/job123/file.txt"))
        True

        >>> is_safe_path(Path("/app/uploads"), Path("/etc/passwd"))
        False
    """
    try:
        # Resolve both paths to absolute
        basedir = basedir.resolve()
        path = path.resolve()

        # Check if path is relative to basedir
        return str(path).startswith(str(basedir))
    except (ValueError, OSError):
        return False
