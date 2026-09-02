"""Tests for the Anthropic API proxy."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

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
    """Create a test ProxyConfig."""
    return ProxyConfig(
        anthropic_api_key="sk-ant-test-key-1234567890",
        allowed_paths=["/v1/messages", "/v1/complete"],
        allowed_external_hosts=["github.com", "api.example.com"],
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


def _make_mock_upstream_response(status=200, headers=None, body=b'{"ok": true}'):
    """Build a mock response that works as an async context manager."""
    resp = AsyncMock()
    resp.status = status
    resp.headers = headers or {"Content-Type": "application/json"}
    resp.read = AsyncMock(return_value=body)
    return resp


def _app_with_mock_upstream(config):
    """Create an app whose upstream_session.request is a mock we can inspect."""
    app = create_app(config)
    mock_session = MagicMock()
    mock_session.close = AsyncMock()  # make teardown awaitable
    mock_response = _make_mock_upstream_response()
    # .request() returns an async context manager
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    mock_session.request = MagicMock(return_value=ctx)

    async def _replace_upstream(app_: web.Application) -> None:
        # Close the real session created by on_startup and swap in the mock
        real = app_.get("upstream_session")
        if real:
            await real.close()
        app_["upstream_session"] = mock_session

    app.on_startup.append(_replace_upstream)
    return app, mock_session, mock_response


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


class TestProxyConfig:
    def test_default_values(self):
        config = ProxyConfig()
        assert config.listen_host == "0.0.0.0"
        assert config.upstream_base_url == "https://api.anthropic.com"
        assert "/v1/messages" in config.allowed_paths
        assert config.allowed_external_hosts == []
        assert config.max_request_body_bytes == 1_048_576
        assert config.rate_limit_requests_per_minute == 60

    def test_from_env(self):
        env = {
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "PROXY_RATE_LIMIT_RPM": "30",
            "PROXY_MAX_BODY_BYTES": "512",
            "PROXY_ALLOWED_PATHS": "/v1/messages,/v1/custom",
            "PROXY_ALLOWED_EXTERNAL_HOSTS": "github.com, google.com",
        }
        with patch.dict(os.environ, env, clear=False):
            config = ProxyConfig.from_env()
        assert config.anthropic_api_key == "sk-ant-test"
        assert config.rate_limit_requests_per_minute == 30
        assert config.max_request_body_bytes == 512
        assert config.allowed_paths == ["/v1/messages", "/v1/custom"]
        assert config.allowed_external_hosts == ["github.com", "google.com"]

    def test_from_env_no_external_hosts(self):
        env = {"ANTHROPIC_API_KEY": "sk-ant-test"}
        with patch.dict(os.environ, env, clear=False):
            config = ProxyConfig.from_env()
        assert config.allowed_external_hosts == []

    def test_validate_missing_api_key(self):
        config = ProxyConfig(anthropic_api_key="")
        errors = config.validate()
        assert any("ANTHROPIC_API_KEY" in e for e in errors)

    def test_validate_valid_config(self):
        config = ProxyConfig(anthropic_api_key="sk-ant-valid")
        errors = config.validate()
        assert errors == []

    def test_validate_openai_only(self):
        """A proxy configured with only an OpenAI key is valid."""
        config = ProxyConfig(anthropic_api_key="", openai_api_key="sk-oai-valid")
        assert config.validate() == []

    def test_openai_defaults(self):
        config = ProxyConfig()
        assert config.openai_base_url == "https://api.openai.com"
        assert "/v1/chat/completions" in config.openai_allowed_paths

    def test_from_env_openai(self):
        env = {
            "OPENAI_API_KEY": "sk-oai-test",
            "PROXY_OPENAI_ALLOWED_PATHS": "/v1/chat/completions,/v1/responses",
        }
        with patch.dict(os.environ, env, clear=False):
            config = ProxyConfig.from_env()
        assert config.openai_api_key == "sk-oai-test"
        assert config.openai_allowed_paths == ["/v1/chat/completions", "/v1/responses"]


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
        app, _, _ = _app_with_mock_upstream(config)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/messages",
            data=b"x" * 100,
            headers={"Content-Type": "application/json"},
        )
        # aiohttp's client_max_size rejects before our handler — returns 400
        assert resp.status in (400, 413)

    async def test_rate_limit_returns_429(self, aiohttp_client, config):
        config.rate_limit_burst = 1
        config.rate_limit_requests_per_minute = 1
        app, _, _ = _app_with_mock_upstream(config)
        client = await aiohttp_client(app)

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

    async def test_allowed_path_forwards_to_upstream(self, aiohttp_client, config):
        app, mock_session, _ = _app_with_mock_upstream(config)
        client = await aiohttp_client(app)

        resp = await client.post(
            "/v1/messages",
            data=b'{"model":"test"}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200

        # Verify upstream was called with Anthropic URL and API key
        call_kwargs = mock_session.request.call_args
        assert "api.anthropic.com" in call_kwargs.kwargs["url"]
        assert call_kwargs.kwargs["headers"]["x-api-key"] == "sk-ant-test-key-1234567890"


# ---------------------------------------------------------------------------
# OpenAI routing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOpenAIRouting:
    def _config(self, **overrides):
        base = dict(
            anthropic_api_key="sk-ant-test-key-1234567890",
            openai_api_key="sk-oai-test-key-9876543210",
            allowed_paths=["/v1/messages"],
            rate_limit_requests_per_minute=120,
            rate_limit_burst=20,
            max_request_body_bytes=1024,
            log_format="text",
        )
        base.update(overrides)
        return ProxyConfig(**base)

    async def test_openai_path_forwards_with_bearer(self, aiohttp_client):
        app, mock_session, _ = _app_with_mock_upstream(self._config())
        client = await aiohttp_client(app)

        resp = await client.post(
            "/v1/chat/completions",
            data=b'{"model":"gpt-5.6-sol"}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200

        call = mock_session.request.call_args
        assert "api.openai.com" in call.kwargs["url"]
        assert call.kwargs["headers"]["Authorization"] == "Bearer sk-oai-test-key-9876543210"
        # The Anthropic key must not leak on an OpenAI request
        assert "x-api-key" not in {k.lower() for k in call.kwargs["headers"]}

    async def test_openai_path_without_key_returns_403(self, aiohttp_client):
        config = self._config(openai_api_key="")
        app = create_app(config)
        client = await aiohttp_client(app)
        resp = await client.post(
            "/v1/chat/completions",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403
        data = await resp.json()
        assert data["error"]["type"] == "forbidden"

    async def test_anthropic_path_unaffected_when_openai_configured(self, aiohttp_client):
        """Regression: Anthropic routing is unchanged when OpenAI is also enabled."""
        app, mock_session, _ = _app_with_mock_upstream(self._config())
        client = await aiohttp_client(app)

        resp = await client.post(
            "/v1/messages",
            data=b'{"model":"claude"}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 200

        call = mock_session.request.call_args
        assert "api.anthropic.com" in call.kwargs["url"]
        assert call.kwargs["headers"]["x-api-key"] == "sk-ant-test-key-1234567890"
        # No stray OpenAI Bearer on an Anthropic request
        assert "authorization" not in {k.lower() for k in call.kwargs["headers"]}


# ---------------------------------------------------------------------------
# External host routing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestExternalHostRouting:
    async def test_disallowed_external_host_returns_403(self, aiohttp_client, app):
        client = await aiohttp_client(app)
        resp = await client.get(
            "/some/path",
            headers={"X-Target-Host": "evil.com"},
        )
        assert resp.status == 403
        data = await resp.json()
        assert data["error"]["type"] == "forbidden"
        assert "evil.com" in data["error"]["message"]

    async def test_allowed_external_host_forwards_request(self, aiohttp_client, config):
        app, mock_session, _ = _app_with_mock_upstream(config)
        client = await aiohttp_client(app)

        resp = await client.get(
            "/api/repos",
            headers={"X-Target-Host": "github.com"},
        )
        assert resp.status == 200

        # Verify the upstream URL targets the external host
        call_kwargs = mock_session.request.call_args
        assert call_kwargs.kwargs["url"] == "https://github.com/api/repos"

        # Verify no API key was injected
        sent_headers = call_kwargs.kwargs["headers"]
        assert "x-api-key" not in sent_headers

    async def test_external_host_case_insensitive(self, aiohttp_client, config):
        app, mock_session, _ = _app_with_mock_upstream(config)
        client = await aiohttp_client(app)

        resp = await client.get(
            "/",
            headers={"X-Target-Host": "GitHub.com"},
        )
        assert resp.status == 200

    async def test_external_host_skips_path_allowlist(self, aiohttp_client, config):
        """External host requests should not be filtered by the Anthropic path allowlist."""
        app, mock_session, _ = _app_with_mock_upstream(config)
        client = await aiohttp_client(app)

        # /any/custom/path would be blocked for Anthropic, but allowed for external hosts
        resp = await client.get(
            "/any/custom/path",
            headers={"X-Target-Host": "api.example.com"},
        )
        assert resp.status == 200

    async def test_external_host_still_rate_limited(self, aiohttp_client, config):
        config.rate_limit_burst = 1
        config.rate_limit_requests_per_minute = 1
        app, _, _ = _app_with_mock_upstream(config)
        client = await aiohttp_client(app)

        # Exhaust burst
        await client.get("/", headers={"X-Target-Host": "github.com"})
        # Second request should be rate limited
        resp2 = await client.get("/", headers={"X-Target-Host": "github.com"})
        assert resp2.status == 429

    async def test_no_external_hosts_configured(self, aiohttp_client):
        """When no external hosts are configured, all X-Target-Host requests are blocked."""
        config = ProxyConfig(
            anthropic_api_key="sk-ant-test",
            allowed_external_hosts=[],
            log_format="text",
        )
        app = create_app(config)
        client = await aiohttp_client(app)
        resp = await client.get("/", headers={"X-Target-Host": "github.com"})
        assert resp.status == 403

    async def test_x_target_host_header_stripped_from_upstream(self, aiohttp_client, config):
        """The X-Target-Host header should not be forwarded to the external host."""
        app, mock_session, _ = _app_with_mock_upstream(config)
        client = await aiohttp_client(app)

        await client.get("/path", headers={"X-Target-Host": "github.com"})

        sent_headers = mock_session.request.call_args.kwargs["headers"]
        # X-Target-Host must not leak to upstream
        assert "X-Target-Host" not in sent_headers
        assert "x-target-host" not in {k.lower() for k in sent_headers}

    async def test_external_host_does_not_set_host_header(self, aiohttp_client, config):
        """Host header should NOT be manually set — aiohttp derives it from the URL."""
        app, mock_session, _ = _app_with_mock_upstream(config)
        client = await aiohttp_client(app)

        await client.get("/path", headers={"X-Target-Host": "github.com"})

        sent_headers = mock_session.request.call_args.kwargs["headers"]
        # Host is in hop_by_hop strip list; aiohttp sets it from the URL automatically
        assert "Host" not in sent_headers
        assert "host" not in {k.lower() for k in sent_headers}
