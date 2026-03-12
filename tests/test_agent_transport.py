"""Tests for agent.py Unix socket transport configuration."""

import os
from unittest.mock import patch

import pytest

from agent import create_client


class TestCreateClient:
    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}, clear=False)
    def test_direct_mode_without_proxy_socket(self):
        """Given no ANTHROPIC_PROXY_SOCKET, client uses standard TCP transport."""
        os.environ.pop("ANTHROPIC_PROXY_SOCKET", None)
        client = create_client()
        assert "api.anthropic.com" in str(client.base_url)

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "ANTHROPIC_PROXY_SOCKET": "/var/run/proxy.sock",
        },
        clear=False,
    )
    def test_proxy_mode_with_socket(self):
        """Given ANTHROPIC_PROXY_SOCKET, client uses UDS transport with localhost base_url."""
        client = create_client()
        assert "localhost" in str(client.base_url)

    @patch.dict(os.environ, {}, clear=False)
    def test_missing_api_key_raises_system_exit(self):
        """Given no ANTHROPIC_API_KEY, create_client raises SystemExit."""
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("ANTHROPIC_PROXY_SOCKET", None)
        with pytest.raises(SystemExit):
            create_client()
