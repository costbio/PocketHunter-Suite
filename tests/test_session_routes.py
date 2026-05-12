"""Tests for the pure session-routing helpers."""
from __future__ import annotations

import pytest


def test_resolve_with_no_short_code(db_with_schema):
    from session_routes import resolve_session

    r = resolve_session(None, None)
    assert r.session is None
    assert r.is_editor is False
    assert r.is_expired is False
    assert r.short_code is None


def test_resolve_with_unknown_short_code(db_with_schema):
    from session_routes import resolve_session

    r = resolve_session("ghost-code", None)
    assert r.session is None
    assert r.short_code == "ghost-code"


def test_resolve_known_session_no_secret_is_viewer(db_with_schema):
    from db.sessions import create_session
    from session_routes import resolve_session

    s = create_session()
    r = resolve_session(s.short_code, None)
    assert r.session is not None
    assert r.session.id == s.id
    assert r.is_editor is False
    assert r.is_expired is False


def test_resolve_known_session_with_secret_is_editor(db_with_schema):
    from db.sessions import create_session
    from session_routes import resolve_session

    s = create_session()
    r = resolve_session(s.short_code, s.edit_secret)
    assert r.is_editor is True


def test_resolve_known_session_with_wrong_secret_is_not_editor(db_with_schema):
    from db.sessions import create_session
    from session_routes import resolve_session

    s = create_session()
    r = resolve_session(s.short_code, "totally-wrong-secret")
    assert r.is_editor is False


def test_expired_session_blocks_editor_even_with_correct_secret(db_with_schema):
    from db.sessions import create_session, mark_expired
    from session_routes import resolve_session

    s = create_session()
    mark_expired(s)
    r = resolve_session(s.short_code, s.edit_secret)
    assert r.is_expired is True
    # Expired sessions can be viewed (DB row preserved for audit) but never edited.
    assert r.is_editor is False


def test_base_url_required(monkeypatch):
    from session_routes import base_url

    monkeypatch.delenv("BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="BASE_URL"):
        base_url()


def test_build_session_url_with_and_without_secret(monkeypatch):
    from session_routes import build_session_url

    monkeypatch.setenv("BASE_URL", "https://example.com")
    assert build_session_url("abc") == "https://example.com/?s=abc"
    assert build_session_url("abc", edit_secret="xyz") == "https://example.com/?s=abc&edit=xyz"


def test_base_url_strips_trailing_slash(monkeypatch):
    from session_routes import build_session_url

    monkeypatch.setenv("BASE_URL", "https://example.com/")
    assert build_session_url("abc") == "https://example.com/?s=abc"


def test_extract_short_code_from_full_url():
    from landing import _extract_short_code

    assert _extract_short_code("https://x.com/?s=abc123&edit=xyz") == "abc123"
    assert _extract_short_code("https://x.com/?s=abc123") == "abc123"
    assert _extract_short_code("abc123") == "abc123"
    assert _extract_short_code("") is None
    assert _extract_short_code("not a code with spaces") is None


def test_extract_edit_secret():
    from landing import _extract_edit_secret

    assert _extract_edit_secret("https://x.com/?s=abc&edit=xyz789") == "xyz789"
    assert _extract_edit_secret("https://x.com/?s=abc") is None
    assert _extract_edit_secret("abc") is None
