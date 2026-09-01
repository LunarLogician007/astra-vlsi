#!/usr/bin/env python3
"""Where the EDA binaries actually run.

The optimisation loop needs two things that do not live in the same place:
the EDA tools (in the container) and the `claude` CLI with the user's
credentials (on the host). Rather than putting a model client inside the image
or credentials into a build, the orchestrator runs on the host and dispatches
each tool invocation into the container.

Set ``ASTRA_TOOL_PREFIX`` to the command that wraps a tool invocation, e.g.

    ASTRA_TOOL_PREFIX='docker run --rm -v /path/to/astra:/work -w /work astra:latest'

The repo is bind-mounted, so both sides see the same files -- but at different
absolute paths. Every host path in the argv and in the tool environment is
rewritten from ``ASTRA_ROOT`` to ``ASTRA_CONTAINER_ROOT`` (default /work) on
the way in. Unset the prefix and everything runs directly, which is what
happens when the flow is already inside the container.
"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("ASTRA_ROOT", Path(__file__).resolve().parent.parent))
CONTAINER_ROOT = os.environ.get("ASTRA_CONTAINER_ROOT", "/work")


def prefix() -> list[str]:
    return shlex.split(os.environ.get("ASTRA_TOOL_PREFIX", "").strip())


def dispatching() -> bool:
    return bool(prefix())


def translate(text: str) -> str:
    """Host path -> container path, when dispatching."""
    if not dispatching():
        return text
    return text.replace(str(ROOT), CONTAINER_ROOT)


def wrap(cmd: list[str], env: dict[str, str] | None = None
         ) -> tuple[list[str], dict[str, str]]:
    """Rewrite one tool invocation for wherever it is going to run.

    Environment variables are passed through with ``-e NAME=value`` rather
    than inherited, because the wrapper command starts a fresh container.
    """
    pre = prefix()
    env = dict(env or {})
    if not pre:
        return cmd, env

    env = {k: translate(v) for k, v in env.items()}
    env["ASTRA_ROOT"] = CONTAINER_ROOT
    passthrough: list[str] = []
    for k, v in env.items():
        passthrough += ["-e", f"{k}={v}"]

    # `-e` belongs to `docker run`, so it has to sit before the image name.
    # The image is the last token of the prefix by construction.
    head, image = pre[:-1], pre[-1:]
    return head + passthrough + image + [translate(c) for c in cmd], env


def have(binary: str) -> bool:
    """Is this tool reachable? Assumed present when dispatching elsewhere."""
    return True if dispatching() else bool(shutil.which(binary))


def where() -> str:
    return ("dispatching to: " + " ".join(prefix())) if dispatching() else "local"
