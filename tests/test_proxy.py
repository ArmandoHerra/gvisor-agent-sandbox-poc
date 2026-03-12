"""Tests for the Anthropic API proxy."""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from proxy import (
    TokenBucketRateLimiter,
    create_app,
)
from proxy_config import ProxyConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config():
    """Create a test ProxyConfig with a temp socket path."""
    return ProxyConfig(
        socket_path="/tmp/test-proxy.sock",
        anthropic_api_key="sk-ant-test-key-1234567890",
        allowed_paths=["/v1/messages", "/v1/complete"],
        rate_limit_requests_per_minute=120,
        rate_limit_burst=20,
        max_request_body_bytes=1024,
        log_level="DEBUG",
        log_format="text",
    )


@pytest.fixture
def app(config):
    """Create a test aiohttp application."""
    return create_app(config)


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestProxyConfig:
    def test_default_values(self):
        config = ProxyConfig()
        assert config.socket_path == "/tmp/anthropic-proxy.sock"
        assert config.upstream_base_url == "https://api.anthropic.com"
        assert "/v1/messages" in config.allowed_paths
        assert config.max_request_body_bytes == 1_048_576
        assert config.rate_limit_requests_per_minute == 60

    def test_from_env(self):
        env = {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "PROXY_SOCKET_PATH": "/tmp/custom.sock",
            "PROXY_RATE_LIMIT_RPM": "30",
            "PROXY_MAX_BODY_BYTES": "512",
            "PROXY_ALLOWED_PATHS": "/v1/messages,/v1/custom",
        }
        with patch.dict(os.environ, env, clear=False):
            config = ProxyConfig.from_env()
        assert config.anthropic_api_key == "sk-ant-test"
        assert config.socket_path == "/tmp/custom.sock"
        assert config.rate_limit_requests_per_minute == 30
        assert config.max_request_body_bytes == 512
        assert config.allowed_paths == ["/v1/messages", "/v1/custom"]

    def test_validate_missing_api_key(self):
        config = ProxyConfig(anthropic_api_key="")
        errors = config.validate()
        assert any("ANTHROPIC_API_KEY" in e for e in errors)

    def test_validate_valid_config(self):
        config = ProxyConfig(anthropic_api_key="sk-ant-valid")
        errors = config.validate()
        assert errors == []


# ---------------------------------------------------------------------------
# Rate limiter tests
# ---------------------------------------------------------------------------


class TestTokenBucketRateLimiter:
    @pytest.mark.asyncio
    async def test_allows_burst(self):
        limiter = TokenBucketRateLimiter(rate=1.0, burst=3)
        assert await limiter.acquire() is True
        assert await limiter.acquire() is True
        assert await limiter.acquire() is True

    @pytest.mark.asyncio
    async def test_blocks_after_burst_exhausted(self):
        limiter = TokenBucketRateLimiter(rate=1.0, burst=2)
        await limiter.acquire()
        await limiter.acquire()
        assert await limiter.acquire() is False

    @pytest.mark.asyncio
    async def test_refills_over_time(self):
        limiter = TokenBucketRateLimiter(rate=10.0, burst=1)
        await limiter.acquire()
        assert await limiter.acquire() is False
        await asyncio.sleep(0.15)  # Should refill ~1.5 tokens at rate=10/s
        assert await limiter.acquire() is True


# ---------------------------------------------------------------------------
# Handler tests (using aiohttp test client)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHandlers:
    async def test_health_endpoint(self, aiohttp_client, app):
        client = await aiohttp_client(app)
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"

    async def test_disallowed_path_returns_403(self, aiohttp_client, app):
        client = await aiohttp_client(app)
        resp = await client.get("/v1/not-allowed")
        assert resp.status == 403
        data = await resp.json()
        assert data["error"]["type"] == "forbidden"

    async def test_request_too_large_returns_413(self, aiohttp_client, config):
        config.max_request_body_bytes = 10  # Very small limit
        app = create_app(config)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/messages",
            data=b"x" * 100,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 413
        data = await resp.json()
        assert data["error"]["type"] == "request_too_large"

    async def test_rate_limit_returns_429(self, aiohttp_client, config):
        config.rate_limit_burst = 1
        config.rate_limit_requests_per_minute = 1
        app = create_app(config)
        client = await aiohttp_client(app)

        with patch("aiohttp.ClientSession.request") as mock_req:
            mock_response = AsyncMock()
            mock_response.status = 200
            mock_response.headers = {"Content-Type": "application/json"}
            mock_response.read = AsyncMock(return_value=b'{"ok": true}')
            mock_response.__aenter__ = AsyncMock(return_value=mock_response)
            mock_response.__aexit__ = AsyncMock(return_value=False)
            mock_req.return_value = mock_response

            # Exhaust burst
            await client.post(
                "/v1/messages",
                data=b'{"model":"test"}',
                headers={"Content-Type": "application/json"},
            )
            # Second request should be rate limited
            resp2 = await client.post(
                "/v1/messages",
                data=b'{"model":"test"}',
                headers={"Content-Type": "application/json"},
            )
            assert resp2.status == 429
            data = await resp2.json()
            assert data["error"]["type"] == "rate_limit_error"
