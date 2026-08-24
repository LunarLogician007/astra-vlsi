#!/usr/bin/env python3
"""ASTRA timing advisor: one Claude call per completed run.

    astra-advise <design>            advise on the latest run
    astra-advise <design> --run <id> advise on a specific run
    astra-advise <design> --dry-run  print the prompt, call nothing

Reads the artifacts `astra run` already produced -- metrics.json, the parsed
timing.json, the design's RTL and SDC -- and asks Claude what to change to
close timing. Output lands in the run directory as advice.md.

This runs on the HOST, not inside the container: the image has no `claude`
binary and no credentials. The run directory is bind-mounted at /work, so the
host sees the same files under runs/.

Billing: shells out to `claude -p`, which authenticates with your Claude
subscription rather than an API key. That means no per-token bill, but the
usage draws from the same plan limits as your interactive sessions -- so this
is deliberately one call per finished run, never one per synthesis attempt.
Each invocation also carries ~10k tokens of Claude Code harness overhead on
top of the prompt below, which is why the timing digest is condensed rather
than dumped whole.

Stdlib only, like the rest of tools/.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("ASTRA_ROOT", Path(__file__).resolve().parent.parent))
DESIGNS = ROOT / "designs"
RUNS = ROOT / "runs"

# The advisor is handed every fact it needs in the prompt, so it has no reason
# to touch the filesystem or the network. Denying the tools keeps it to a
# single round trip -- an agent that starts grepping the repo turns one call
# into many, which is exactly the usage pattern this tool exists to avoid.
NO_TOOLS = "Bash Edit Write Read Glob Grep WebFetch WebSearch Task NotebookEdit"

SYSTEM = """You are a senior physical-design engineer reviewing a failed \
static-timing run from an ASIC synthesis flow (Yosys + OpenSTA, Nangate45).

You are given the full RTL, the SDC constraints, and a digest of the OpenSTA
critical path. Netlist instance names (_14637_, _12490_) are Yosys-generated
and carry no meaning -- infer the structure from the cell-type sequence and
map it back to the RTL yourself.

Answer with these sections and nothing else:

## Diagnosis
What the critical path physically is, in RTL terms. Name the specific
expression or signal. Two or three sentences.

## Recommendations
Ranked, most slack recovered first. For each: the change, the RTL or SDC edit
that implements it (show real code, not prose), the slack you expect it to
recover and why, and the cost in area or latency.

## Invariants
State explicitly whether each recommendation preserves the invariants given in
the design config. Flag any that do not.

Be concrete and quantitative. Do not restate the numbers back at me -- I can
read the report. No preamble, no summary at the end."""


def die(msg: str) -> None:
    print(f"[advise] ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def info(msg: str) -> None:
    print(f"[advise] {msg}", flush=True)


def find_run(design: str, run: str | None) -> Path:
    base = RUNS / design
    if not base.is_dir():
        die(f"no runs for '{design}' -- run `make run DESIGN={design}` first")
    if run and run != "latest":
        rdir = base / run
        if not rdir.is_dir():
            die(f"no such run: {rdir}")
        return rdir
    latest = base / "latest"
    if latest.exists():
        return latest.resolve()
    stamped = sorted(p for p in base.iterdir() if p.is_dir())
    if not stamped:
        die(f"no runs for '{design}'")
    return stamped[-1]


def load_json(path: Path, what: str) -> dict[str, Any]:
    if not path.is_file():
        die(f"missing {what}: {path}")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        die(f"{what} is not valid JSON ({path}): {e}")
    return {}


def read_text(path: Path) -> str:
    try:
        return path.read_text()
    except OSError as e:
        return f"<unreadable: {e}>"


def path_digest(path: dict[str, Any], top_n: int = 12) -> str:
    """Condense a 90-stage OpenSTA path into something worth paying for.

    The full stage list is mostly zero-delay pin arrivals. What actually
    characterises the path is the cell-type mix (a wall of AOI/OAI/XOR is a
    ripple carry; a wall of full adders is a multiplier array) plus the
    handful of stages that own the delay.
    """
    stages = path.get("stages") or []
    cells = [s["cell"] for s in stages if s.get("cell")]
    hist = Counter(cells)

    lines = [
        f"startpoint      {path.get('startpoint')} ({path.get('startpoint_info')})",
        f"endpoint        {path.get('endpoint')} ({path.get('endpoint_info')})",
        f"slack           {path.get('slack_ns')} ns ({path.get('status')})",
        f"arrival         {path.get('arrival_ns')} ns",
        f"required        {path.get('required_ns')} ns",
        f"logic depth     {path.get('logic_depth')}",
        "",
        f"cell mix ({len(cells)} cell pins over {len(stages)} stages):",
    ]
    for cell, n in hist.most_common():
        lines.append(f"    {n:4d}x  {cell}")

    ranked = sorted(
        (s for s in stages if s.get("delay")),
        key=lambda s: s["delay"],
        reverse=True,
    )[:top_n]
    lines += ["", f"top {len(ranked)} stages by delay:"]
    for s in ranked:
        lines.append(
            f"    {s['delay']:.4f} ns  at t={s.get('time'):.4f}  "
            f"{s.get('cell')}  {s.get('pin')}"
        )

    # The ordered cell walk is what reveals the topology (serial vs tree).
    walk = [s["cell"] for s in stages if s.get("cell")]
    lines += ["", "cell sequence along the path:", "    " + " -> ".join(walk)]
    return "\n".join(lines)


def endpoint_summary(paths: list[dict[str, Any]], limit: int = 10) -> str:
    viol = [p for p in paths if p.get("status") == "VIOLATED"]
    if not viol:
        return "none"
    lines = []
    for p in viol[:limit]:
        lines.append(
            f"    {p.get('slack_ns'):>8} ns  depth {p.get('logic_depth'):>3}  "
            f"{p.get('startpoint')} -> {p.get('endpoint')}"
        )
    if len(viol) > limit:
        lines.append(f"    ... and {len(viol) - limit} more")
    return "\n".join(lines)


def build_prompt(design: str, rdir: Path) -> str:
    cfg = load_json(DESIGNS / design / "config.json", "design config")
    metrics = load_json(rdir / "metrics.json", "metrics.json")
    timing = load_json(rdir / "02_sta" / "timing.json", "timing.json")

    ddir = DESIGNS / design
    rtl = "\n\n".join(
        f"----- {r} -----\n{read_text(ddir / r)}" for r in cfg.get("rtl", [])
    )
    sdc = read_text(ddir / cfg["sdc"]) if cfg.get("sdc") else "<none>"

    summary = timing.get("summary", {})
    synth = metrics.get("synth", {})
    clock = cfg.get("clock", {})
    worst = timing.get("worst_path") or {}

    notes = cfg.get("notes") or {}
    notes_block = json.dumps(notes, indent=2) if notes else "<none given>"

    return f"""# Design: {design}

{cfg.get('description', '')}

target clock    {clock.get('name')} @ {clock.get('period_ns')} ns
pdk             {cfg.get('pdk')}

## Design notes from config.json

{notes_block}

## Synthesis result

cells           {synth.get('cells')}
area            {synth.get('area_um2')} um^2

## Setup timing (post-synthesis)

WNS             {summary.get('wns_ns')} ns
TNS             {summary.get('tns_ns')} ns
violating eps   {summary.get('violating_endpoints')}
hold WNS        {(summary.get('hold') or {}).get('wns_ns')} ns

violating endpoints (worst first):
{endpoint_summary(timing.get('critical_paths') or [])}

## Critical path

{path_digest(worst)}

## RTL

{rtl}

## SDC

{sdc}
"""


def call_claude(prompt: str, model: str, timeout: int) -> dict[str, Any]:
    exe = shutil.which("claude")
    if not exe:
        die("`claude` not found on PATH -- install Claude Code, or use --dry-run")

    cmd = [
        exe, "-p",
        "--output-format", "json",
        "--model", model,
        "--append-system-prompt", SYSTEM,
        "--disallowedTools", NO_TOOLS,
        # Someone else's MCP servers would be loaded into this call and billed
        # to it. This prompt needs none.
        "--strict-mcp-config",
    ]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        die(f"claude timed out after {timeout}s")

    if proc.returncode != 0:
        die(f"claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        die(f"unexpected output from claude: {proc.stdout[:500]}")
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="astra-advise",
        description="Ask Claude what to change to close timing on a finished run.",
    )
    ap.add_argument("design")
    ap.add_argument("--run", default="latest", help="run id (default: latest)")
    ap.add_argument("--model", default="opus", help="claude model alias (default: opus)")
    ap.add_argument("--timeout", type=int, default=600, help="seconds (default: 600)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompt and exit without calling Claude")
    ap.add_argument("--force", action="store_true",
                    help="re-advise even if advice.md already exists")
    args = ap.parse_args()

    rdir = find_run(args.design, args.run)
    prompt = build_prompt(args.design, rdir)

    if args.dry_run:
        print(prompt)
        approx = len(prompt) // 4
        info(f"prompt is {len(prompt)} chars (~{approx} tokens); nothing was called")
        return 0

    out = rdir / "advice.md"
    if out.exists() and not args.force:
        info(f"{out} already exists -- re-run with --force to spend another call")
        return 0

    info(f"run {rdir.name}: asking {args.model} ({len(prompt) // 4} tokens of context)")
    res = call_claude(prompt, args.model, args.timeout)

    if res.get("is_error"):
        die(f"claude reported an error: {res.get('result', '')[:500]}")

    text = res.get("result", "")
    out.write_text(text + "\n")

    cost = res.get("total_cost_usd")
    secs = (res.get("duration_ms") or 0) / 1000
    usage = res.get("usage") or {}
    info(
        f"wrote {out} in {secs:.1f}s "
        f"(in {usage.get('input_tokens', 0)} + cache {usage.get('cache_read_input_tokens', 0)}, "
        f"out {usage.get('output_tokens', 0)})"
    )
    if cost is not None:
        # On a subscription this is notional -- it is what the call would have
        # cost on the API, and is the honest measure of what it drew from the
        # plan's limits.
        info(f"notional cost ${cost:.4f} (drawn from your plan, not billed separately)")
    print()
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
