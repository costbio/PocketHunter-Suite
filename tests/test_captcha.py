"""Tests for the Turnstile verifier (the server-side half of B1.2).

The widget half (captcha.turnstile_widget) is a Streamlit Components
v2 wrapper — manually exercised in the live stack, not unit-tested.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestTurnstileVerifier:
    def test_disabled_always_returns_true(self):
        from captcha import TurnstileVerifier

        v = TurnstileVerifier(site_key="s", secret_key="k", enabled=False)
        assert v.verify("any-token") is True
        assert v.verify(None) is True
        assert v.verify("") is True

    def test_empty_token_rejected_when_enabled(self):
        from captcha import TurnstileVerifier

        v = TurnstileVerifier(site_key="s", secret_key="k", enabled=True)
        assert v.verify(None) is False
        assert v.verify("") is False

    def test_calls_cloudflare_siteverify_with_correct_payload(self):
        from captcha import TURNSTILE_VERIFY_URL, TurnstileVerifier

        v = TurnstileVerifier(site_key="s", secret_key="my-secret", enabled=True)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": True}
        with patch("captcha.requests.post", return_value=mock_resp) as mock_post:
            assert v.verify("user-token", remote_ip="203.0.113.5") is True
        args, kwargs = mock_post.call_args
        assert args[0] == TURNSTILE_VERIFY_URL
        assert kwargs["data"] == {
            "secret": "my-secret",
            "response": "user-token",
            "remoteip": "203.0.113.5",
        }
        assert kwargs["timeout"] == 5

    def test_success_false_in_response_rejects(self):
        from captcha import TurnstileVerifier

        v = TurnstileVerifier(site_key="s", secret_key="k", enabled=True)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": False, "error-codes": ["invalid"]}
        with patch("captcha.requests.post", return_value=mock_resp):
            assert v.verify("user-token") is False

    def test_network_error_fails_closed(self):
        from captcha import TurnstileVerifier

        v = TurnstileVerifier(site_key="s", secret_key="k", enabled=True)
        with patch("captcha.requests.post",
                   side_effect=RuntimeError("DNS failure")):
            assert v.verify("user-token") is False

    def test_remoteip_omitted_when_unknown(self):
        from captcha import TurnstileVerifier

        v = TurnstileVerifier(site_key="s", secret_key="k", enabled=True)
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": True}
        with patch("captcha.requests.post", return_value=mock_resp) as mock_post:
            v.verify("tok", remote_ip=None)
        kwargs = mock_post.call_args.kwargs
        assert "remoteip" not in kwargs["data"]


class TestBuildVerifierFromSettings:
    def test_reads_settings(self, monkeypatch):
        from config import Config

        monkeypatch.setattr(Config, "TURNSTILE_ENABLED", True)
        monkeypatch.setattr(Config, "TURNSTILE_SITE_KEY", "site-XYZ")
        monkeypatch.setattr(Config, "TURNSTILE_SECRET_KEY", "secret-ABC")
        # build_verifier_from_settings reads `settings` (the singleton),
        # not Config. Patch the singleton too.
        from settings import settings as _settings

        monkeypatch.setattr(_settings, "TURNSTILE_ENABLED", True)
        monkeypatch.setattr(_settings, "TURNSTILE_SITE_KEY", "site-XYZ")
        monkeypatch.setattr(_settings, "TURNSTILE_SECRET_KEY", "secret-ABC")

        from captcha import build_verifier_from_settings

        v = build_verifier_from_settings()
        assert v.enabled is True
        assert v.site_key == "site-XYZ"
        assert v.secret_key == "secret-ABC"
