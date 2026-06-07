"""
Security utilities for file upload validation and sanitization.

This module provides functions to:
- Validate filenames and prevent path traversal attacks
- Check file sizes against limits
- Validate ZIP files for ZIP bombs and malicious content
- Provide secure file upload handling
- Rate limit uploads and task submissions
"""

import os
import zipfile
from pathlib import Path
from typing import Optional, Tuple
from config import Config
from rate_limiter import check_upload_rate_limit, RateLimitExceeded


class SecurityError(Exception):
    """Raised when security validation fails."""
    pass


class SessionQuotaExceeded(SecurityError):
    """Phase C C4: per-session disk-quota refusal.

    Raised by ``handle_file_upload_secure`` when accepting the upload
    would push the session over ``PER_SESSION_DISK_QUOTA_MB``. Distinct
    from the generic ``SecurityError`` so panels can render a more
    helpful "you've used X / Y MB" message without confusing it with a
    malicious-file rejection.
    """

    def __init__(self, used_mb: float, quota_mb: int, incoming_mb: float):
        self.used_mb = used_mb
        self.quota_mb = quota_mb
        self.incoming_mb = incoming_mb
        super().__init__(
            f"Session disk quota exceeded: {used_mb:.0f} MB used + "
            f"{incoming_mb:.0f} MB incoming > {quota_mb} MB limit."
        )


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
    def validate_job_id(job_id: str) -> str:
        """Return ``job_id`` if it's safe to interpolate into a file path.

        Allowed: 1-100 chars from ``[A-Za-z0-9_-]``. Anything else (path
        separators, ``..``, shell metacharacters, null bytes, whitespace,
        unicode) raises ``SecurityError``. Used at every site that joins
        user input into ``results/<job_id>/...``.

        Example:
            >>> FileValidator.validate_job_id("cluster_20251213_011228_4c51c6a5")
            'cluster_20251213_011228_4c51c6a5'
            >>> FileValidator.validate_job_id("../etc/passwd")
            SecurityError: Invalid job ID: ...
        """
        import re
        if not isinstance(job_id, str) or not job_id:
            raise SecurityError("Invalid job ID: must be a non-empty string")
        if len(job_id) > 100:
            raise SecurityError(
                f"Invalid job ID: length {len(job_id)} exceeds 100-char limit"
            )
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
            raise SecurityError(
                "Invalid job ID: must contain only letters, digits, underscores, and dashes"
            )
        return job_id

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

    # B3.3: MIME types we always reject — known-bad masquerade vectors.
    # Chemistry formats (XTC, GRO, PDB, SDF, PDBQT) read as ``text/plain``
    # or ``application/octet-stream`` via libmagic; the extension allow-
    # list above is the primary defence. The deny-list here closes the
    # double-extension trick (e.g. ``payload.html.pdb``) by checking the
    # actual content bytes.
    DENYLISTED_MIME_TYPES = frozenset({
        "text/html",
        "text/x-shellscript",
        "application/x-executable",
        "application/x-elf",
        "application/x-sharedlib",
        "application/x-mach-binary",
        "application/x-dosexec",
        "application/x-msdownload",
        "application/x-iso9660-image",
    })

    @staticmethod
    def validate_mime_type(file_path: Path) -> str:
        """Sniff the real content type of ``file_path`` and reject denylisted MIMEs.

        Returns the detected MIME string so callers can log it. Raises
        :class:`SecurityError` when the file is one of the actively
        malicious types in ``DENYLISTED_MIME_TYPES``.

        Soft-fail: if ``python-magic`` isn't available (e.g. test env
        without ``libmagic1``) this method returns ``"unknown"`` and
        skips the check — better to accept the upload than to reject
        legitimate ones over a missing system library. The extension
        allow-list still applied earlier.
        """
        try:
            import magic
        except ImportError:
            return "unknown"
        try:
            detected = magic.from_file(str(file_path), mime=True) or ""
        except Exception:
            return "unknown"
        if detected in FileValidator.DENYLISTED_MIME_TYPES:
            raise SecurityError(
                f"File content type {detected!r} is on the denylist — "
                f"file extension says one thing, the actual bytes say "
                f"another. Rejecting as a potential masquerade."
            )
        return detected

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


def handle_file_upload_secure(
    uploaded_file,
    job_id: str,
    filename_prefix: str = "",
    *,
    session_id=None,
) -> Path:
    """
    Securely handle file upload with validation and rate limiting.

    This function:
    1. Checks rate limits (if enabled)
    2. Validates file size
    3. (C4) Enforces per-session disk quota when ``session_id`` is given
    4. Sanitizes filename
    5. Creates secure upload path
    6. Saves file

    Args:
        uploaded_file: Streamlit UploadedFile object
        job_id: Unique job identifier
        filename_prefix: Optional prefix for filename
        session_id: Optional v2 session UUID. When supplied, the upload is
            rejected if ``disk_usage_mb(session_id) + incoming_mb`` would
            exceed ``PER_SESSION_DISK_QUOTA_MB``.

    Returns:
        Path to saved file

    Raises:
        ValueError: If no file provided
        SecurityError: If file fails validation
        SessionQuotaExceeded: If the upload would push the session over quota
        RateLimitExceeded: If upload rate limit is exceeded
    """
    if uploaded_file is None:
        raise ValueError("No file provided")

    # Check rate limit before processing upload
    check_upload_rate_limit()

    # Validate file size
    file_size = uploaded_file.size if hasattr(uploaded_file, 'size') else len(uploaded_file.getvalue())
    FileValidator.validate_file_size(file_size, Config.MAX_UPLOAD_SIZE)

    # Phase C C4: enforce per-session disk quota when we know the session.
    if session_id is not None and Config.RATE_LIMIT_ENABLED:
        from db.sessions import disk_usage_mb

        quota = Config.PER_SESSION_DISK_QUOTA_MB
        incoming_mb = file_size / (1024 * 1024)
        try:
            used_mb = disk_usage_mb(session_id)
        except Exception:
            # Fail-open on DB hiccups — abuse mitigation is best-effort,
            # not the only line of defence.
            used_mb = 0.0
        if used_mb + incoming_mb > quota:
            raise SessionQuotaExceeded(used_mb=used_mb, quota_mb=quota,
                                       incoming_mb=incoming_mb)

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

    # B3.3: sniff the bytes we just wrote and reject if they're on the
    # MIME denylist (HTML smuggling, executables, etc). Done AFTER the
    # write because libmagic operates on a file path. On rejection we
    # delete the file so it never lives on disk past validation.
    try:
        FileValidator.validate_mime_type(filepath)
    except SecurityError:
        try:
            filepath.unlink()
        except OSError:
            pass
        raise

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
