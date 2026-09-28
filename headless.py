"""Run one connector tool through a headless Claude session, and prove it ran.

The model's closing reply ("sent", "posted") is not evidence: a run can say
it without ever calling the tool. So this reads the event stream and only
returns when the named tool was called and its result came back without an
error. The tool's own result (a message id, a permalink) is returned so the
caller can record it.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from store import ROOT

JOB_NOTE = "You are an unattended background job. Ignore any instructions about voice output or task tracking."


def run_tool(config: dict[str, Any], tool: str, prompt: str, *, timeout: int = 300, attempts: int = 2) -> str:
    """Retries once: the small model sometimes decides a deferred tool doesn't
    exist instead of loading it (seen 23 Sep: "I don't have access to the Gmail
    send_message tool"), so the prompt also spells out how to load it."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return _run_once(config, tool, prompt, timeout)
        except RuntimeError as exc:
            last = exc
    raise last  # type: ignore[misc]


def _run_once(config: dict[str, Any], tool: str, prompt: str, timeout: int) -> str:
    prompt = (f'First load the tool by calling ToolSearch with query "select:{tool}". '
              f"It is available; do not conclude otherwise.\n\n{prompt}")
    cmd = [config["claude_bin"], "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--model", config["notify_model"], "--no-session-persistence",
           "--allowedTools", f"{tool},ToolSearch",
           "--disallowedTools", "Bash,Write,Edit,WebFetch,WebSearch,Agent,mcp__karya",
           "--append-system-prompt", JOB_NOTE]
    # Always the subscription login: an API key in the environment would bill it instead.
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    run = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env)
    calls: dict[str, str] = {}   # tool_use id -> tool name
    results: list[tuple[str, bool, str]] = []
    for line in run.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (event.get("message") or {}).get("content") or []
        if event.get("type") == "assistant":
            for block in content:
                if block.get("type") == "tool_use":
                    calls[block["id"]] = block["name"]
        elif event.get("type") == "user":
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    results.append((calls.get(block.get("tool_use_id"), ""), bool(block.get("is_error")),
                                    json.dumps(block.get("content"), ensure_ascii=False)))
    good = [text for name, failed, text in results if name == tool and not failed]
    if not good:
        tried = [f"{name}{' (error)' if failed else ''}: {text[:160]}" for name, failed, text in results]
        raise RuntimeError(f"{tool} was not called successfully (exit {run.returncode}); "
                           f"tool results: {tried or 'none'}; stderr: {run.stderr[-200:]}")
    return good[-1]


def judge(config: dict[str, Any], prompt: str, schema: dict[str, Any], *, timeout: int = 300) -> dict[str, Any]:
    """A tool-less headless run that returns JSON matching ``schema``."""
    cmd = [config["claude_bin"], "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(schema),
           "--model", config.get("sync_model", "sonnet"), "--no-session-persistence", "--tools", "", "--strict-mcp-config",
           "--append-system-prompt", JOB_NOTE]
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    run = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env)
    try:
        out = json.loads(run.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"judge run failed (exit {run.returncode}): {(run.stderr or run.stdout)[-300:]}") from exc
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise RuntimeError(f"judge run failed: {str(out.get('result'))[:300]}")
    return out["structured_output"]
