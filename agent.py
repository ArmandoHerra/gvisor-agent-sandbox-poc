#!/usr/bin/env python3
"""Claude SDK agent running inside gVisor sandbox."""

import os
import platform
import socket
import sys
import urllib.error
import urllib.request

import anthropic
import httpx2

# --- Provider / model selection ---------------------------------------------
# The agent talks to Anthropic or OpenAI. Selection order:
#   1. LLM_PROVIDER, if set to "anthropic" or "openai" (explicit switch).
#   2. Otherwise auto-detect from which API keys are present.
#   3. If both keys are set and LLM_PROVIDER is unset, Anthropic wins.
# Per-provider model defaults are overridable via ANTHROPIC_MODEL / OPENAI_MODEL.
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5.6-sol",
}

# Anthropic requires an explicit output cap; 128K is the shared ceiling of the
# current Fable/Opus/Sonnet models. OpenAI's cap varies by model and gpt-5.6-sol
# postdates this code, so the OpenAI path sends a cap only when OPENAI_MAX_TOKENS
# is set (otherwise the model's own default applies).
MAX_TOKENS = 128000


def _resolve_provider() -> str:
    """Pick the active provider from LLM_PROVIDER or the configured keys."""
    explicit = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if explicit in DEFAULT_MODELS:
        return explicit
    if explicit:
        print(
            f"ERROR: LLM_PROVIDER='{explicit}' is invalid "
            f"(expected one of: {', '.join(DEFAULT_MODELS)})"
        )
        raise SystemExit(1)

    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))
    if has_openai and not has_anthropic:
        return "openai"
    # Anthropic wins when only it is set, and is the default when both are set.
    return "anthropic"


def _resolve_model(provider: str) -> str:
    """Resolve the model for a provider, honoring its override env var."""
    override = os.environ.get(
        "OPENAI_MODEL" if provider == "openai" else "ANTHROPIC_MODEL"
    )
    return override or DEFAULT_MODELS[provider]


def _print_refusal(final) -> None:
    """Report a safety refusal with its evidence: which model actually served
    the request (response.model) and the structured reason (stop_details)."""
    parts = [f"served by {final.model}"]
    details = getattr(final, "stop_details", None)
    if details is not None:
        if getattr(details, "category", None):
            parts.append(f"category: {details.category}")
        if getattr(details, "explanation", None):
            parts.append(f"explanation: {details.explanation}")
    print(f"\n[request declined by the model's safety system — {'; '.join(parts)}]")


def _shell_enabled() -> bool:
    """True if operator shell execution (!exec) is enabled via ALLOW_SHELL."""
    return os.environ.get("ALLOW_SHELL", "").strip().lower() in ("1", "true", "yes")


def _shell_tool_enabled() -> bool:
    """True if the model-callable run_shell tool is enabled via ALLOW_SHELL_TOOL."""
    return os.environ.get("ALLOW_SHELL_TOOL", "").strip().lower() in ("1", "true", "yes")


def _check_network_isolated() -> bool:
    """Test if we can reach the public internet (not just the proxy)."""
    try:
        s = socket.create_connection(("8.8.8.8", 53), timeout=2)
        s.close()
        return False
    except (OSError, socket.timeout):
        return True


def proxy_fetch(url: str, *, proxy_url: str | None = None) -> dict:
    """
    Fetch a URL through the proxy using the X-Target-Host header.

    Parses the target URL, routes the request through the proxy,
    and returns a dict with status, headers, and body.
    """
    proxy_url = (
        proxy_url
        or os.environ.get("ANTHROPIC_PROXY_URL")
        or os.environ.get("OPENAI_PROXY_URL")
    )
    if not proxy_url:
        return {"error": "No proxy URL configured (ANTHROPIC_PROXY_URL/OPENAI_PROXY_URL not set)"}

    # Parse the target URL to extract host and path
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = parsed.hostname
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"

    if not host:
        return {"error": f"Could not parse host from URL: {url}"}

    # Route through proxy with X-Target-Host header
    proxy_request_url = f"{proxy_url.rstrip('/')}{path}"
    req = urllib.request.Request(
        proxy_request_url,
        headers={"X-Target-Host": host, "User-Agent": "gvisor-sandbox-agent/1.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
            return {
                "status": resp.status,
                "url": url,
                "content_length": len(body),
                "content_type": resp.headers.get("Content-Type", ""),
                "body_preview": body[:500].decode("utf-8", errors="replace"),
            }
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        return {"status": e.code, "url": url, "error": error_body}
    except Exception as e:
        return {"url": url, "error": str(e)}


def gather_env_info(provider: str, model: str):
    """Collect runtime environment details to send as context."""
    proxy_url = os.environ.get("ANTHROPIC_PROXY_URL") or os.environ.get("OPENAI_PROXY_URL")
    info = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "user": os.environ.get("USER", os.getuid()),
        "workdir": os.getcwd(),
        "pid": os.getpid(),
        "writable_tmp": os.access("/tmp", os.W_OK),
        "writable_root": os.access("/", os.W_OK),
        "proxied_mode": bool(proxy_url),
        "network_isolated": _check_network_isolated(),
        "shell_enabled": _shell_enabled(),
        "shell_tool_enabled": _shell_tool_enabled(),
        "provider": provider,
        "model": model,
    }
    if proxy_url:
        info["proxy_url"] = proxy_url
    return info


def create_client(provider: str | None = None):
    """
    Create the API client for the active provider.

    Anthropic: if ANTHROPIC_PROXY_URL is set, route through the host-side proxy
    (which injects the real key and forwards upstream); otherwise connect
    directly (ANTHROPIC_BASE_URL overridable).

    OpenAI: direct connection only — the bundled proxy is Anthropic-specific, so
    proxied mode combined with OpenAI is refused rather than silently misrouted.
    """
    provider = provider or _resolve_provider()

    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            print("ERROR: OPENAI_API_KEY is not set")
            raise SystemExit(1)
        openai_proxy = os.environ.get("OPENAI_PROXY_URL")
        if os.environ.get("ANTHROPIC_PROXY_URL") and not openai_proxy:
            # Network-isolated Anthropic context but OpenAI is selected without
            # its own proxy route — a direct api.openai.com call would just fail.
            print(
                "ERROR: OpenAI in proxied mode needs OPENAI_PROXY_URL "
                "(use 'make run-openai-proxied'), not ANTHROPIC_PROXY_URL."
            )
            raise SystemExit(1)
        try:
            import openai  # lazy — only needed when the OpenAI provider is active
        except ImportError:
            print("ERROR: the 'openai' package is not installed in this image/venv.")
            raise SystemExit(1)
        if openai_proxy:
            # Proxy URL is a bare base (http://host:port); the OpenAI SDK appends
            # endpoint paths under /v1, so pin the client base to <proxy>/v1.
            base_url = openai_proxy.rstrip("/") + "/v1"
        else:
            base_url = os.environ.get("OPENAI_BASE_URL") or None
        return openai.OpenAI(api_key=api_key, base_url=base_url)

    # provider == "anthropic"
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set")
        raise SystemExit(1)

    proxy_url = os.environ.get("ANTHROPIC_PROXY_URL")
    if proxy_url:
        # Proxied mode — route through host-side proxy via TCP
        return anthropic.Anthropic(api_key=api_key, base_url=proxy_url)
    # Direct mode — connect to Anthropic API directly
    return anthropic.Anthropic(
        api_key=api_key,
        base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
    )


def _stream_openai(client, model: str, system: str | None, messages: list) -> str:
    """Stream a reply from OpenAI Chat Completions; print + return the text.

    NOTE: gpt-5.6-sol and its API surface postdate this code. Chat Completions
    with `max_completion_tokens` is the assumed shape; verify against current
    OpenAI docs (Responses API, role names, parameter constraints) if a call
    fails.
    """
    oai_messages = ([{"role": "system", "content": system}] if system else []) + messages
    kwargs = {"model": model, "messages": oai_messages, "stream": True}
    cap = os.environ.get("OPENAI_MAX_TOKENS")
    if cap:
        kwargs["max_completion_tokens"] = int(cap)

    import openai  # lazy

    parts: list[str] = []
    finish_reason = None
    refusal = None
    try:
        for chunk in client.chat.completions.create(**kwargs):
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = getattr(choice, "delta", None)
            if delta is not None:
                if getattr(delta, "content", None):
                    print(delta.content, end="", flush=True)
                    parts.append(delta.content)
                if getattr(delta, "refusal", None):
                    refusal = (refusal or "") + delta.refusal
            if choice.finish_reason:
                finish_reason = choice.finish_reason
    except openai.OpenAIError as exc:
        print(f"\n\n[stream interrupted: {exc}]")

    if refusal:
        print(f"\n[the model declined this request — {refusal.strip()}]")
    elif finish_reason == "content_filter":
        print(
            f"\n[request filtered by the provider — model {model}, "
            "finish_reason: content_filter]"
        )
    return "".join(parts)


def _stream_anthropic(client, model: str, system: str | None, messages: list) -> str:
    """Stream a reply from the Anthropic Messages API; print + return the text."""
    parts: list[str] = []
    kwargs = {"model": model, "max_tokens": MAX_TOKENS, "messages": messages}
    if system:
        kwargs["system"] = system
    try:
        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
                parts.append(text)
            final = stream.get_final_message()
            if final.stop_reason == "refusal":
                _print_refusal(final)
    except (httpx2.RemoteProtocolError, httpx2.ReadError) as exc:
        print(f"\n\n[stream interrupted: {exc}]")
    return "".join(parts)


def stream_reply(client, provider: str, model: str, system: str | None, messages: list) -> str:
    """Stream a reply from the active provider, printing text as it arrives."""
    if provider == "openai":
        return _stream_openai(client, model, system, messages)
    return _stream_anthropic(client, model, system, messages)


def print_env(env_info: dict) -> None:
    """Print container environment info."""
    print("=== Container Environment ===")
    for key, value in env_info.items():
        print(f"  {key}: {value}")
    print()


def run_probe(client, provider: str, model: str, env_info: dict) -> None:
    """Run the default sandbox probe prompt."""
    prompt = f"""You are running inside a gVisor-sandboxed Docker container. Here is proof — the runtime environment data collected from inside your container:

{chr(10).join(f'- {k}: {v}' for k, v in env_info.items())}

Based on this environment data, do the following:
1. Confirm you understand you're running in a sandboxed container and mention the hostname and platform.
2. Identify which security restrictions are active (read-only root, dropped capabilities, limited PIDs, etc.).
3. Suggest one creative experiment to test the sandbox boundaries (without actually breaking anything).

Keep your response concise — under 200 words."""

    print("=== Model Response ===")
    turn_messages = [{"role": "user", "content": prompt}]
    if _shell_tool_enabled():
        _run_tool_turn(client, provider, model, None, turn_messages)
    else:
        stream_reply(client, provider, model, None, turn_messages)
    print()


def _handle_fetch_command(cmd: str) -> str | None:
    """Handle !fetch <url> commands. Returns output string or None if not a fetch command."""
    if not cmd.startswith("!fetch "):
        return None

    url = cmd[7:].strip()
    if not url:
        return "[fetch] Usage: !fetch <url>  (e.g., !fetch https://github.com)"

    # Add https:// if no scheme provided
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    result = proxy_fetch(url)

    if "error" in result:
        status = result.get("status", "N/A")
        return f"[fetch] {url}\n  Status: {status}\n  Error: {result['error']}"

    lines = [
        f"[fetch] {url}",
        f"  Status: {result['status']}",
        f"  Content-Type: {result['content_type']}",
        f"  Content-Length: {result['content_length']} bytes",
        f"  Preview: {result['body_preview'][:200]}...",
    ]
    return "\n".join(lines)


def _run_shell(command: str) -> dict:
    """Execute a shell command in the sandbox. Returns {exit,stdout,stderr} or {error}.

    Shared by the operator `!exec` command and the model-callable run_shell tool.
    Runs via the shell in /workspace with a SHELL_TIMEOUT-second cap and truncated
    output. What the command can actually do is still bounded by the container's
    gVisor hardening (dropped capabilities, read-only root, noexec tmpfs unless
    WORKSPACE_EXEC=1).
    """
    import subprocess

    try:
        timeout = float(os.environ.get("SHELL_TIMEOUT", "30"))
    except ValueError:
        timeout = 30.0

    cwd = "/workspace" if os.path.isdir("/workspace") else None
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
    except subprocess.TimeoutExpired:
        return {"error": f"Timed out after {timeout:g}s"}
    except Exception as e:  # noqa: BLE001 — surface any spawn failure to the caller
        return {"error": str(e)}

    return {
        "exit": proc.returncode,
        "stdout": (proc.stdout or "")[:4000],
        "stderr": (proc.stderr or "")[:2000],
    }


def _format_shell_result(res: dict) -> str:
    """Render a _run_shell result as text (for display and as a tool result)."""
    if "error" in res:
        return f"error: {res['error']}"
    lines = [f"exit: {res['exit']}"]
    if res.get("stdout"):
        lines.append(f"stdout:\n{res['stdout']}")
    if res.get("stderr"):
        lines.append(f"stderr:\n{res['stderr']}")
    return "\n".join(lines)


def _handle_exec_command(cmd: str) -> str | None:
    """Handle !exec <shell command> (operator-driven). None if not an !exec line.

    Gated by ALLOW_SHELL=1 — disabled by default. The model-callable equivalent
    is the run_shell tool (ALLOW_SHELL_TOOL); both share _run_shell.
    """
    if not cmd.startswith("!exec "):
        return None

    command = cmd[6:].strip()
    if not command:
        return "[exec] Usage: !exec <shell command>  (e.g., !exec ls -la /workspace)"
    if not _shell_enabled():
        return (
            "[exec] Shell execution is disabled. Re-run with ALLOW_SHELL=1 "
            "(e.g., `ALLOW_SHELL=1 make prompt`)."
        )

    body = _format_shell_result(_run_shell(command))
    return f"[exec] {command}\n" + "\n".join(f"  {ln}" for ln in body.splitlines())


# --- Model-callable shell tool (native tool-calling) ------------------------

_SHELL_TOOL_NAME = "run_shell"
_SHELL_TOOL_DESCRIPTION = (
    "Run a shell command inside the gVisor sandbox and return its exit code, "
    "stdout, and stderr. Use it to inspect the environment or test sandbox "
    "boundaries. Commands run in /workspace with a timeout; what they can do is "
    "limited by the container's hardening."
)
_SHELL_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "The shell command to run."}
    },
    "required": ["command"],
}


def _anthropic_tools() -> list:
    return [{
        "name": _SHELL_TOOL_NAME,
        "description": _SHELL_TOOL_DESCRIPTION,
        "input_schema": _SHELL_TOOL_SCHEMA,
    }]


def _openai_tools() -> list:
    return [{
        "type": "function",
        "function": {
            "name": _SHELL_TOOL_NAME,
            "description": _SHELL_TOOL_DESCRIPTION,
            "parameters": _SHELL_TOOL_SCHEMA,
        },
    }]


def _run_tool_turn(client, provider, model, system, history, max_iters=6) -> str:
    """Run one assistant turn with the model-callable run_shell tool.

    Loops model -> tool call -> tool result until the model stops calling tools
    (or max_iters). Prints assistant text and each tool call/result, and returns
    the final assistant text. Intermediate tool exchanges are kept only within
    this turn — cross-turn history stores the final text, keeping the shared
    conversation provider-neutral.
    """
    if provider == "openai":
        return _tool_turn_openai(client, model, system, history, max_iters)
    return _tool_turn_anthropic(client, model, system, history, max_iters)


def _tool_turn_anthropic(client, model, system, history, max_iters) -> str:
    tools = _anthropic_tools()
    local = list(history)
    final: list[str] = []
    for _ in range(max_iters):
        kwargs = {"model": model, "max_tokens": MAX_TOKENS, "messages": local, "tools": tools}
        if system:
            kwargs["system"] = system
        try:
            with client.messages.stream(**kwargs) as stream:
                for text in stream.text_stream:
                    print(text, end="", flush=True)
                    final.append(text)
                resp = stream.get_final_message()
        except (httpx2.RemoteProtocolError, httpx2.ReadError) as exc:
            print(f"\n\n[stream interrupted: {exc}]")
            break
        local.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            break
        tool_results = []
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == _SHELL_TOOL_NAME:
                command = (block.input or {}).get("command", "")
                print(f"\n[run_shell] {command}", flush=True)
                body = _format_shell_result(_run_shell(command))
                print(body + "\n", flush=True)
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": body}
                )
        if not tool_results:
            break
        local.append({"role": "user", "content": tool_results})
    return "".join(final)


def _tool_turn_openai(client, model, system, history, max_iters) -> str:
    import json as _json
    import openai

    tools = _openai_tools()
    local = ([{"role": "system", "content": system}] if system else []) + [dict(m) for m in history]
    kwargs = {"model": model, "tools": tools}
    cap = os.environ.get("OPENAI_MAX_TOKENS")
    if cap:
        kwargs["max_completion_tokens"] = int(cap)
    # gpt-5.x reasoning models reject function tools + reasoning in
    # /v1/chat/completions (400). reasoning_effort='none' is the documented fix
    # (the other option is the /v1/responses API). Configurable via
    # OPENAI_REASONING_EFFORT; set it to 'omit' to send no reasoning_effort at
    # all (for non-reasoning models that reject the parameter).
    effort = (os.environ.get("OPENAI_REASONING_EFFORT") or "none").strip()
    if effort.lower() != "omit":
        kwargs["reasoning_effort"] = effort
    final: list[str] = []
    for _ in range(max_iters):
        try:
            resp = client.chat.completions.create(messages=local, **kwargs)
        except openai.OpenAIError as exc:
            print(f"\n\n[error: {exc}]")
            break
        msg = resp.choices[0].message
        if msg.content:
            print(msg.content, end="", flush=True)
            final.append(msg.content)
        tool_calls = getattr(msg, "tool_calls", None)
        entry = {"role": "assistant", "content": msg.content or ""}
        if tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        local.append(entry)
        if not tool_calls:
            break
        for tc in tool_calls:
            if tc.function.name != _SHELL_TOOL_NAME:
                continue
            try:
                args = _json.loads(tc.function.arguments or "{}")
            except ValueError:
                args = {}
            command = args.get("command", "")
            print(f"\n[run_shell] {command}", flush=True)
            body = _format_shell_result(_run_shell(command))
            print(body + "\n", flush=True)
            local.append({"role": "tool", "tool_call_id": tc.id, "content": body})
    return "".join(final)


def run_interactive(client, provider: str, model: str, env_info: dict) -> None:
    """Interactive REPL — multi-turn conversation with the model inside the sandbox."""
    env_context = "\n".join(f"- {k}: {v}" for k, v in env_info.items())
    proxy_url = os.environ.get("ANTHROPIC_PROXY_URL") or os.environ.get("OPENAI_PROXY_URL")

    system_prompt = (
        "You are running inside a gVisor-sandboxed Docker container. "
        "Here is the runtime environment:\n\n"
        f"{env_context}\n\n"
        "Answer the user's questions. You are aware of your sandboxed context.\n\n"
    )

    if _shell_enabled():
        system_prompt += (
            "The operator can run shell commands inside this sandbox via the "
            "'!exec <cmd>' REPL command and will paste the output back to you. "
            "When a shell command would help (e.g. inspecting the filesystem or "
            "testing a sandbox boundary), suggest the exact '!exec ...' line for "
            "the operator to run.\n\n"
        )

    if proxy_url:
        system_prompt += (
            "IMPORTANT: This container is network-isolated. Direct HTTP requests "
            "and DNS lookups will fail. However, the user has a special REPL command "
            "'!fetch <url>' that routes HTTP GET requests through the proxy to "
            "whitelisted external hosts. Only hosts configured in the proxy's "
            "PROXY_ALLOWED_EXTERNAL_HOSTS will succeed; others return 403. "
            "When the user asks to fetch a URL or test connectivity, suggest they "
            "use '!fetch <url>' (e.g., '!fetch https://google.com'). "
            "Direct socket connections, ping, and DNS will always fail in this sandbox."
        )

    messages: list[dict] = []
    print("Interactive mode — type your prompts below. Ctrl+D or 'exit' to quit.")
    if proxy_url:
        print("  Use '!fetch <url>' to make HTTP requests through the proxy.")
    if _shell_enabled():
        print("  Use '!exec <cmd>' to run a shell command in the sandbox.")
    if _shell_tool_enabled():
        print("  The model can call a run_shell tool on its own (ALLOW_SHELL_TOOL).")
    print()

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("Bye.")
            break

        # Handle local commands (no model roundtrip needed); feed results back
        for local_output in (
            _handle_fetch_command(user_input),
            _handle_exec_command(user_input),
        ):
            if local_output is not None:
                print(f"\n{local_output}\n")
                messages.append({"role": "user", "content": user_input})
                messages.append({"role": "assistant", "content": local_output})
                break
        else:
            local_output = None
        if local_output is not None:
            continue

        messages.append({"role": "user", "content": user_input})

        print("\nmodel> ", end="", flush=True)
        if _shell_tool_enabled():
            reply = _run_tool_turn(client, provider, model, system_prompt, messages)
        else:
            reply = stream_reply(client, provider, model, system_prompt, messages)
        print("\n")

        if reply:
            messages.append({"role": "assistant", "content": reply})
        else:
            # Remove the user message if we got no response at all
            messages.pop()


def main():
    interactive = "--interactive" in sys.argv

    provider = _resolve_provider()
    model = _resolve_model(provider)

    env_info = gather_env_info(provider, model)
    print_env(env_info)

    client = create_client(provider)

    if interactive:
        run_interactive(client, provider, model, env_info)
    else:
        run_probe(client, provider, model, env_info)


if __name__ == "__main__":
    main()
