"""Serve large `/app/static/` files gzipped, transparently.

Why
---
Streamlit's static endpoint hands the file straight to ``FileResponse``
with no ``Content-Encoding``. Chrome's per-resource disk cache slot is
roughly 64 MB; anything bigger triggers ``ERR_CACHE_WRITE_FAILURE``,
which on our codepath manifests as Mol* logging *"Failed to download
data"* and the viewport staying blank.

PDB text compresses ~10× with gzip, so a 358 MB ``viewer.pdb`` becomes
a 35 MB gzipped wire payload — well inside the cache slot. The browser
sees ``Content-Encoding: gzip`` and decompresses transparently before
handing bytes to JS, so the Mol* bridge code stays unchanged.

How
---
We monkey-patch ``FileResponse`` in
``streamlit.web.server.starlette.starlette_routes``. The endpoint
closure resolves that name at request time, so re-binding it on the
module takes effect for every subsequent request.

Our subclass:
1. Sees the original path (e.g. ``viewer.pdb``).
2. If the file is bigger than ``GZIP_MIN_BYTES`` (default 32 MB),
   looks for a ``<path>.gz`` sibling.
3. If the ``.gz`` is missing, generates it lazily (level-1 gzip on the
   same path) so subsequent requests are O(stat).
4. Returns a real ``FileResponse`` pointed at the ``.gz`` file but
   keeps the original ``Content-Type`` (so Mol* still sees
   ``chemical/x-pdb`` after decompression) and adds
   ``Content-Encoding: gzip`` + ``Vary: Accept-Encoding``.

Small files fall through unchanged. The patch is idempotent + defensive
(any error falls back to the upstream behaviour).
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import threading

logger = logging.getLogger(__name__)


# Files below this size aren't worth gzipping: the round-trip overhead
# (one-time compress + browser decompress) outweighs the cache benefit.
GZIP_MIN_BYTES = 32 * 1024 * 1024  # 32 MB

# Lock so concurrent first-time requests don't race writing the same .gz.
_gen_lock = threading.Lock()


def _ensure_gz_sibling(path: str) -> str | None:
    """Return the path of a gzipped sibling for ``path``, creating it if needed.

    Returns ``None`` if the file is missing or generation fails — caller
    then falls back to serving the uncompressed file.
    """
    if not os.path.isfile(path):
        return None
    gz_path = path + ".gz"
    if os.path.isfile(gz_path):
        # Refresh stale gz if the source has been rewritten (mtime newer
        # than the gz means the source changed since last compression).
        try:
            if os.path.getmtime(path) <= os.path.getmtime(gz_path):
                return gz_path
        except OSError:
            return gz_path  # best-effort; serve what we have

    with _gen_lock:
        # Re-check under the lock — another thread may have just finished.
        if os.path.isfile(gz_path) and (
            os.path.getmtime(path) <= os.path.getmtime(gz_path)
        ):
            return gz_path
        tmp_path = gz_path + ".tmp"
        try:
            with open(path, "rb") as src, gzip.open(tmp_path, "wb", compresslevel=1) as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            os.replace(tmp_path, gz_path)
            logger.info(
                "gzip: built %s (%.1f MB → %.1f MB)",
                gz_path,
                os.path.getsize(path) / (1024 * 1024),
                os.path.getsize(gz_path) / (1024 * 1024),
            )
        except OSError as e:
            logger.warning("gzip: could not build %s (%s); falling back", gz_path, e)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return None
        return gz_path


def _find_static_endpoint():
    """Locate the running app's ``_app_static_endpoint`` closure.

    Streamlit's static endpoint is a closure created inside
    ``build_app_static_routes`` at server-build time; ``FileResponse``
    is captured as a free variable from a function-local import. To
    swap that reference we have to find the closure in the running
    Starlette app's route table and rewrite its cell.

    We use the garbage collector to find the live ``Starlette`` instance
    instead of fighting Streamlit's private object graph — this is
    refactor-resilient (works regardless of where Streamlit stashes
    the app reference).
    """
    try:
        import gc

        from starlette.applications import Starlette

        for obj in gc.get_objects():
            if not isinstance(obj, Starlette):
                continue
            routes = getattr(obj, "routes", None) or []
            for route in routes:
                ep = getattr(route, "endpoint", None)
                if ep is not None and getattr(ep, "__name__", "") == "_app_static_endpoint":
                    return ep
    except Exception as e:  # noqa: BLE001
        logger.warning("could not locate _app_static_endpoint (%s)", e)
    return None


def install_gzip_static() -> None:
    """Monkey-patch Streamlit's static endpoint to serve large files gzipped.

    Idempotent. Defensive — any import error logs + carries on with the
    upstream behaviour (so a Streamlit refactor doesn't break startup).
    """
    try:
        from streamlit.web.server.starlette import starlette_routes
        from starlette.responses import FileResponse
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Could not install gzip-static patch (%s) — large viewer files "
            "may hit browser cache limits and fail to load", e,
        )
        return

    if getattr(starlette_routes, "_gzip_static_installed", False):
        return  # idempotent guard

    class _GzipAwareFileResponse(FileResponse):
        """Drop-in for ``FileResponse`` that handles large static files
        sensibly for the browser:

        1. **Pre-gzipped sibling** — if a ``<path>.gz`` exists, serve
           that with ``Content-Encoding: gzip`` so the wire payload is
           small. Streamlit's ``SelectiveGZipMiddleware`` already does
           on-the-fly compression, but it forces chunked encoding which
           amplifies the next problem.
        2. **No-store for big files** — set ``Cache-Control: no-store``
           so the browser doesn't try to write the response to its disk
           cache. Chrome's per-resource cache slot is ~10–40 MB depending
           on cache size; a 50+ MB response triggers
           ``ERR_CACHE_WRITE_FAILURE``, and for *chunked* responses (which
           on-the-fly gzip produces) that closes the body mid-stream
           before the browser delivers it to JS — Mol* then logs
           "Failed to download data" and the viewport stays blank.
           ``no-store`` bypasses the cache write entirely; the bytes flow
           straight to JS.
        """

        def __init__(self, path, *args, media_type=None, **kwargs):
            served_path = str(path)
            extra_headers: dict[str, str] = {}
            try:
                size = os.path.getsize(served_path)
            except OSError:
                size = 0
            if size >= GZIP_MIN_BYTES:
                # (1) Prefer a pre-gzipped sibling when it exists.
                gz = _ensure_gz_sibling(served_path)
                if gz is not None:
                    served_path = gz
                    extra_headers["content-encoding"] = "gzip"
                    extra_headers["vary"] = "Accept-Encoding"
                # (2) Forbid caching so cache-write failures can't truncate
                #     the body. Applies to both the pre-gzipped path AND
                #     the on-the-fly-gzipped fallback (where Streamlit's
                #     middleware compresses but emits chunked encoding).
                extra_headers["cache-control"] = "no-store"

            headers = dict(kwargs.pop("headers", None) or {})
            headers.update(extra_headers)
            if headers:
                kwargs["headers"] = headers
            super().__init__(served_path, *args, media_type=media_type, **kwargs)

    starlette_routes.FileResponse = _GzipAwareFileResponse
    starlette_routes._gzip_static_installed = True

    # Critical: the endpoint closure captured `FileResponse` as a free
    # variable from a function-local import inside `build_app_static_routes`.
    # Module-level patching above doesn't reach it; rewrite the closure
    # cell directly so the running endpoint uses our subclass.
    endpoint = _find_static_endpoint()
    if endpoint is not None and endpoint.__closure__:
        free_vars = endpoint.__code__.co_freevars
        if "FileResponse" in free_vars:
            idx = free_vars.index("FileResponse")
            try:
                endpoint.__closure__[idx].cell_contents = _GzipAwareFileResponse
                logger.warning(
                    "gzip-static patch installed (threshold=%d MB) — "
                    "closure FileResponse rewritten",
                    GZIP_MIN_BYTES // (1024 * 1024),
                )
                return
            except (TypeError, ValueError) as e:
                logger.warning(
                    "could not rewrite endpoint closure cell (%s); "
                    "the patch won't take effect", e,
                )
        else:
            logger.warning(
                "endpoint closure free vars don't include FileResponse "
                "(got %s) — Streamlit may have refactored; patch ineffective",
                free_vars,
            )
    else:
        logger.warning(
            "could not find _app_static_endpoint in the running app; "
            "gzip-static patch installed at module level only (may not "
            "take effect for the static route)",
        )
