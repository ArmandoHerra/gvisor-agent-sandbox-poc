"""Tests for agent.py client creation and transport configuration."""

import os
from unittest.mock import patch

import pytest

from types import SimpleNamespace

from agent import (
    create_client,
    proxy_fetch,
    _handle_fetch_command,
    _handle_exec_command,
    _run_shell,
    _shell_tool_enabled,
    _anthropic_tools,
    _openai_tools,
    _tool_turn_anthropic,
    _tool_turn_openai,
)


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
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("LLM_PROVIDER", None)
        with pytest.raises(SystemExit):
            create_client()

    @patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "sk-oai-test",
            "OPENAI_PROXY_URL": "http://proxy-host:18080",
            "LLM_PROVIDER": "openai",
        },
        clear=False,
    )
    def test_openai_proxied_base_url(self):
        """OpenAI proxied mode pins the client base_url to <proxy>/v1."""
        os.environ.pop("ANTHROPIC_PROXY_URL", None)
        client = create_client()
        base = str(client.base_url)
        assert "proxy-host:18080" in base
        assert base.rstrip("/").endswith("/v1")

    @patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "sk-oai-test",
            "ANTHROPIC_PROXY_URL": "http://proxy-host:18080",
            "LLM_PROVIDER": "openai",
        },
        clear=False,
    )
    def test_openai_with_anthropic_proxy_only_raises(self):
        """OpenAI selected inside an Anthropic-proxied context (no OPENAI_PROXY_URL) is refused."""
        os.environ.pop("OPENAI_PROXY_URL", None)
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


class TestHandleExecCommand:
    def test_non_exec_command_returns_none(self):
        assert _handle_exec_command("hello world") is None
        assert _handle_exec_command("exec ls") is None

    def test_empty_command_returns_usage(self):
        result = _handle_exec_command("!exec ")
        assert result is not None
        assert "Usage" in result

    @patch.dict(os.environ, {}, clear=False)
    def test_disabled_by_default(self):
        os.environ.pop("ALLOW_SHELL", None)
        result = _handle_exec_command("!exec echo hi")
        assert result is not None
        assert "disabled" in result.lower()

    @patch.dict(os.environ, {"ALLOW_SHELL": "1"}, clear=False)
    def test_enabled_runs_command(self):
        result = _handle_exec_command("!exec echo sandbox-exec-ok")
        assert result is not None
        assert "sandbox-exec-ok" in result
        assert "exit: 0" in result

    @patch.dict(os.environ, {"ALLOW_SHELL": "1", "SHELL_TIMEOUT": "1"}, clear=False)
    def test_timeout_is_reported(self):
        result = _handle_exec_command("!exec sleep 5")
        assert result is not None
        assert "Timed out" in result


class TestShellTool:
    def test_run_shell_success(self):
        res = _run_shell("echo run-shell-ok")
        assert res["exit"] == 0
        assert "run-shell-ok" in res["stdout"]

    def test_run_shell_nonzero_exit(self):
        res = _run_shell("exit 3")
        assert res.get("exit") == 3

    @patch.dict(os.environ, {}, clear=False)
    def test_shell_tool_disabled_by_default(self):
        os.environ.pop("ALLOW_SHELL_TOOL", None)
        assert _shell_tool_enabled() is False

    @patch.dict(os.environ, {"ALLOW_SHELL_TOOL": "1"}, clear=False)
    def test_shell_tool_enabled(self):
        assert _shell_tool_enabled() is True

    def test_tool_schemas(self):
        a = _anthropic_tools()[0]
        assert a["name"] == "run_shell"
        assert "command" in a["input_schema"]["properties"]
        o = _openai_tools()[0]
        assert o["type"] == "function"
        assert o["function"]["name"] == "run_shell"

    def test_anthropic_tool_loop_executes_and_finishes(self, capsys):
        """Model calls run_shell once, then returns a final answer — mocked, no API."""
        tool_use = SimpleNamespace(
            type="tool_use", name="run_shell",
            input={"command": "echo tool-loop-ok"}, id="t1",
        )
        turn1 = SimpleNamespace(content=[tool_use], stop_reason="tool_use")
        turn2 = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="done")], stop_reason="end_turn",
        )

        class _FakeStream:
            def __init__(self, texts, final):
                self._texts, self._final = texts, final
            def __enter__(self): return self
            def __exit__(self, *a): return False
            @property
            def text_stream(self): return iter(self._texts)
            def get_final_message(self): return self._final

        class _FakeMessages:
            def __init__(self, scripted): self._s, self.calls = scripted, 0
            def stream(self, **kw):
                texts, final = self._s[self.calls]; self.calls += 1
                return _FakeStream(texts, final)

        client = SimpleNamespace(messages=_FakeMessages([([], turn1), (["done"], turn2)]))
        result = _tool_turn_anthropic(client, "m", None, [{"role": "user", "content": "go"}], 6)

        assert result == "done"
        assert client.messages.calls == 2  # looped through the tool call
        out = capsys.readouterr().out
        assert "[run_shell] echo tool-loop-ok" in out
        assert "tool-loop-ok" in out  # the command actually executed

    def test_openai_tool_loop_sends_reasoning_effort(self, capsys):
        """Regression: gpt-5.x reject function tools + reasoning in chat/completions,
        so the OpenAI tool loop must send reasoning_effort='none' — mocked, no API."""
        os.environ.pop("OPENAI_REASONING_EFFORT", None)
        tc = SimpleNamespace(
            id="c1",
            function=SimpleNamespace(
                name="run_shell", arguments='{"command": "echo oai-tool-ok"}'
            ),
        )
        msg1 = SimpleNamespace(content=None, tool_calls=[tc])
        msg2 = SimpleNamespace(content="all done", tool_calls=None)

        class _Comp:
            def __init__(self, scripted):
                self._s, self.calls, self.last_kwargs = scripted, 0, None
            def create(self, **kwargs):
                self.last_kwargs = kwargs
                r = SimpleNamespace(choices=[SimpleNamespace(message=self._s[self.calls])])
                self.calls += 1
                return r

        comp = _Comp([msg1, msg2])
        client = SimpleNamespace(chat=SimpleNamespace(completions=comp))

        result = _tool_turn_openai(client, "gpt-5.6-sol", None, [{"role": "user", "content": "go"}], 6)

        assert result == "all done"
        assert comp.calls == 2
        assert comp.last_kwargs.get("reasoning_effort") == "none"
        assert "oai-tool-ok" in capsys.readouterr().out
