"""The panel's browser boundary.

Binding to 127.0.0.1 keeps the panel off the network but not away from the
operator's own browser: any page open in that browser can issue requests to it.
These tests pin the three checks that close that gap, because each one is easy to
regress silently — nothing about the panel LOOKS broken when they are gone.
"""

from __future__ import annotations

import sys
import pathlib

import pytest


from deployctl.webui.panel import security  # noqa: E402


class TestLoopbackDetection:
    @pytest.mark.parametrize("value", [
        "127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:8765",
        "http://127.0.0.1:8765", "http://localhost:8765", "[::1]:8765",
    ])
    def test_accepts_loopback(self, value):
        assert security.is_loopback(value)

    @pytest.mark.parametrize("value", [
        "evil.example.com",
        "evil.example.com:8765",
        "https://evil.example.com",
        # The shapes a rebinding or confusion attack actually takes.
        "127.0.0.1.evil.example.com",
        "localhost.evil.example.com",
        "notlocalhost",
        "10.0.0.5:8765",
    ])
    def test_rejects_everything_else(self, value):
        assert not security.is_loopback(value)


class TestTokenScope:
    def test_page_and_assets_need_no_token(self):
        """The operator types the URL; a top-level navigation carries nothing."""
        assert not security.requires_token("/")
        assert not security.requires_token("/static/panel.js")
        assert not security.requires_token("/static/panel.css")

    @pytest.mark.parametrize("path", [
        "/run/update", "/run/stop", "/config", "/logs/10.0.0.1",
        "/live/bar", "/live/checklist", "/image-tags", "/config/preview", "/config/problems",
        "/open-terminal/local", "/open-terminal/ssh/10.0.0.1",
        "/jobs", "/jobs/20260923-101500-abc123/stream", "/jobs/20260923-101500-abc123/cancel",
    ])
    def test_every_acting_route_needs_a_token(self, path):
        assert security.requires_token(path)

    def test_unknown_future_route_defaults_to_protected(self):
        """Fail closed: a route added later is covered until it opts out."""
        assert security.requires_token("/some/route/added/next/year")


class TestToken:
    def test_token_is_long_and_random(self):
        assert len(security.TOKEN) >= 32

    def test_comparison_rejects_wrong_and_empty(self):
        class FakeRequest:
            def __init__(self, token):
                self.query_params = {"t": token} if token is not None else {}
                self.headers = {}

        assert security.has_valid_token(FakeRequest(security.TOKEN))
        assert not security.has_valid_token(FakeRequest("wrong"))
        assert not security.has_valid_token(FakeRequest(""))
        assert not security.has_valid_token(FakeRequest(None))
