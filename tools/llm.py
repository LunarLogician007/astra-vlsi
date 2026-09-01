#!/usr/bin/env python3
"""Thin wrapper around the `claude` CLI, shared by the agent implementations.

Every agent here is a single non-interactive round trip: the whole prompt is
assembled from files the flow already produced, so the model has no reason to
touch the filesystem or the network. Tools are denied for that reason -- an
agent that starts grepping the repo turns one call into many, and the
orchestrator makes N of these per iteration.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from typing import Any

NO_TOOLS = "Bash Edit Write Read Glob Grep WebFetch WebSearch Task NotebookEdit"

# The CLI is invoked once per candidate, in parallel. Nothing here is shared
# mutable state except the usage tally, which is.
_usage_lock = threading.Lock()
_usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
          "cache_read_input_tokens": 0, "cost_usd": 0.0, "duration_s": 0.0}


class LLMError(RuntimeError):
    pass


def available() -> bool:
    return bool(shutil.which("claude"))


def usage() -> dict[str, Any]:
    with _usage_lock:
        return dict(_usage)


def reset_usage() -> None:
    with _usage_lock:
        for k in _usage:
            _usage[k] = 0 if isinstance(_usage[k], int) else 0.0


def call(prompt: str, system: str, model: str = "opus", timeout: int = 900,
         max_retries: int = 1) -> str:
    """Run one prompt and return the model's text.

    Retries only on transport-level failures (a non-zero exit or unparsable
    stdout). A model answer that is merely unusable is the caller's problem to
    detect, because only the caller knows what shape it asked for.
    """
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("`claude` not found on PATH -- install Claude Code, "
                       "or run with --dry-run")

    cmd = [exe, "-p", "--output-format", "json", "--model", model,
           "--append-system-prompt", system,
           "--disallowedTools", NO_TOOLS,
           # Someone else's MCP servers would be loaded into this call and
           # billed to it. These prompts need none.
           "--strict-mcp-config"]

    last = ""
    for attempt in range(max_retries + 1):
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = f"timed out after {timeout}s"
            continue
        if proc.returncode != 0:
            last = f"exit {proc.returncode}: {(proc.stderr or '').strip()[:300]}"
            continue
        try:
            res = json.loads(proc.stdout)
        except json.JSONDecodeError:
            last = f"unparsable output: {proc.stdout[:300]}"
            continue

        if res.get("is_error"):
            raise LLMError(f"claude reported an error: "
                           f"{str(res.get('result', ''))[:300]}")

        u = res.get("usage") or {}
        with _usage_lock:
            _usage["calls"] += 1
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens"):
                _usage[k] += int(u.get(k) or 0)
            _usage["cost_usd"] += float(res.get("total_cost_usd") or 0.0)
            _usage["duration_s"] += float(res.get("duration_ms") or 0) / 1000.0

        return str(res.get("result", ""))

    raise LLMError(f"claude call failed after {max_retries + 1} attempt(s): {last}")
