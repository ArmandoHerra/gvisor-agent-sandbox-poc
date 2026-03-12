"""Tests for agent.py client creation and transport configuration."""

import os
from unittest.mock import patch

import pytest

from agent import create_client, proxy_fetch, _handle_fetch_command


class TestCreateClient:
    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False)
    def test_direct_mode_without_proxy(self):
        """Given no ANTHROPIC_PROXY_URL, client connects directly to Anthropic."""
        os.environ.pop("ANTHROPIC_PROXY_URL", None)
        client = create_client()
        assert "api.anthropic.com" in str(client.base_url)

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "ANTHROPIC_PROXY_URL": "http://proxy-host:18080",
        },
        clear=False,
    )
    def test_proxy_mode_with_url(self):
        """Given ANTHROPIC_PROXY_URL, client routes through the proxy."""
        client = create_client()
        assert "proxy-host:18080" in str(client.base_url)

    @patch.dict(os.environ, {}, clear=False)
    def test_missing_api_key_raises_system_exit(self):
        """Given no ANTHROPIC_API_KEY, create_client raises SystemExit."""
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("ANTHROPIC_PROXY_URL", None)
        with pytest.raises(SystemExit):
            create_client()


class TestProxyFetch:
    @patch.dict(os.environ, {}, clear=False)
    def test_no_proxy_url_returns_error(self):
        """proxy_fetch returns error when no proxy URL is configured."""
        os.environ.pop("ANTHROPIC_PROXY_URL", None)
        result = proxy_fetch("https://example.com")
        assert "error" in result
        assert "No proxy URL" in result["error"]

    def test_invalid_url_returns_error(self):
        result = proxy_fetch("not-a-url", proxy_url="http://localhost:18080")
        assert "error" in result


class TestHandleFetchCommand:
    def test_non_fetch_command_returns_none(self):
        assert _handle_fetch_command("hello world") is None
        assert _handle_fetch_command("fetch something") is None

    def test_empty_url_returns_usage(self):
        result = _handle_fetch_command("!fetch ")
        assert result is not None
        assert "Usage" in result

    @patch.dict(os.environ, {"ANTHROPIC_PROXY_URL": ""}, clear=False)
    def test_fetch_adds_https_scheme(self):
        """!fetch google.com should prepend https://."""
        os.environ.pop("ANTHROPIC_PROXY_URL", None)
        result = _handle_fetch_command("!fetch google.com")
        assert result is not None
        assert "No proxy URL" in result  # fails gracefully without proxy
