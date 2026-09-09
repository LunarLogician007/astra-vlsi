#!/usr/bin/env python3
"""Sequential equivalence checking -- the SEC_i constraint of Eq. 4.

The paper uses commercial SEC. Here there are two open-source routes, tried in
whichever order the `engine` argument asks for:

    eqy    the SymbiYosys equivalence front end. Preferred when installed:
           it partitions the design and discharges each partition separately,
           so it scales past what a single monolithic miter can prove.
    yosys  a miter discharged by Yosys's own SAT engine, via
           flow/scripts/sec.tcl. Always available, since Yosys is already a
           hard dependency of the flow.

Both report the same shape. The one field worth reading carefully is
``method``: ``induction`` (or eqy's ``pass``) is an unbounded proof, whereas
``bounded`` only says no counterexample exists within N cycles. The
orchestrator records the difference rather than flattening it, because
promoting a design on a bounded check is a weaker claim than the paper makes.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_sta  # noqa: E402
import toolenv    # noqa: E402

ROOT = Path(os.environ.get("ASTRA_ROOT", Path(__file__).resolve().parent.parent))
FLOW = ROOT / "flow"

DEFAULT_DEPTH = 20


def _fail(reason: str, engine: str, **extra: Any) -> dict[str, Any]:
    return {"equivalent": False, "method": "error", "reason": reason,
            "engine": engine, **extra}


def _run(cmd: list[str], log: Path, env: dict[str, str] | None = None,
         cwd: Path | None = None, timeout: int = 1800) -> tuple[int, str]:
    log.parent.mkdir(parents=True, exist_ok=True)
    cmd, env = toolenv.wrap(cmd, env)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, **(env or {})},
                              cwd=str(cwd or ROOT))
    except subprocess.TimeoutExpired:
        log.write_text(f"# cmd: {' '.join(cmd)}\n# TIMEOUT after {timeout}s\n")
        return 124, ""
    out = (proc.stdout or "") + (proc.stderr or "")
    log.write_text(f"# cmd: {' '.join(cmd)}\n{out}\n# exit code: {proc.returncode}\n")
    return proc.returncode, out


# ---------------------------------------------------------------------------
# yosys miter engine
# ---------------------------------------------------------------------------


def check_yosys(top: str, gold: list[Path], gate: list[Path], workdir: Path,
                depth: int = DEFAULT_DEPTH, liberty: Path | None = None,
                timeout: int = 1800) -> dict[str, Any]:
    if not toolenv.have("yosys"):
        return _fail("yosys not on PATH", "yosys")

    workdir.mkdir(parents=True, exist_ok=True)
    env = {
        "ASTRA_FLOW": str(FLOW),
        "ASTRA_TOP": top,
        "ASTRA_GOLD_FILES": " ".join(str(p) for p in gold),
        "ASTRA_GATE_FILES": " ".join(str(p) for p in gate),
        "ASTRA_SEC_DEPTH": str(depth),
        "ASTRA_SEC_LIBERTY": str(liberty) if liberty else "",
        "ASTRA_SEC_WORKDIR": str(workdir),
    }
    log = workdir / "sec.log"
    t0 = time.time()
    rc, out = _run(["yosys", "-c", str(FLOW / "scripts" / "sec.tcl")], log,
                   env=env, timeout=timeout)
    runtime = round(time.time() - t0, 2)

    if rc == 124:
        return _fail(f"yosys SEC timed out after {timeout}s", "yosys",
                     runtime_s=runtime, log=str(log))

    kv = parse_sta.parse_kv(out).get("sec", {})
    if not kv:
        # The script exits 0 on every decided outcome, so an empty KV block
        # means it died before deciding anything.
        tail = "\n".join(out.strip().splitlines()[-8:])
        return _fail(f"yosys SEC produced no verdict (exit {rc}): {tail}", "yosys",
                     runtime_s=runtime, log=str(log))

    return {
        "equivalent": str(kv.get("equivalent")) in ("1", "True", "true"),
        "method": kv.get("method", "unknown"),
        "reason": kv.get("reason", ""),
        "depth": kv.get("depth", depth),
        "engine": "yosys",
        "runtime_s": runtime,
        "steps": parse_sta.parse_steps(out),
        "log": str(log),
    }


# ---------------------------------------------------------------------------
# eqy engine
# ---------------------------------------------------------------------------


_EQY_TEMPLATE = """\
[options]
depth {depth}

[gold]
{gold_read}
prep -top {top}

[gate]
{gate_read}
prep -top {top}

[strategy sat]
use sat
depth {depth}
"""


def check_eqy(top: str, gold: list[Path], gate: list[Path], workdir: Path,
              depth: int = DEFAULT_DEPTH, timeout: int = 1800) -> dict[str, Any]:
    if not toolenv.have("eqy"):
        return _fail("eqy not on PATH", "eqy")

    workdir.mkdir(parents=True, exist_ok=True)
    cfg = workdir / "sec.eqy"
    # The file names go inside the config, where toolenv.wrap cannot see them,
    # so they are translated here -- eqy reads this file wherever it runs.
    cfg.write_text(_EQY_TEMPLATE.format(
        depth=depth, top=top,
        gold_read="\n".join(f"read -sv {toolenv.translate(str(p))}" for p in gold),
        gate_read="\n".join(f"read -sv {toolenv.translate(str(p))}" for p in gate),
    ))

    # eqy refuses to start when its work directory already exists; -f clears it.
    outdir = workdir / "eqy"
    log = workdir / "sec_eqy.log"
    t0 = time.time()
    rc, out = _run(["eqy", "-f", "-d", str(outdir), str(cfg)], log, timeout=timeout)
    runtime = round(time.time() - t0, 2)

    if rc == 124:
        return _fail(f"eqy timed out after {timeout}s", "eqy",
                     runtime_s=runtime, log=str(log))

    # eqy writes its verdict to <outdir>/status and mirrors it in the exit
    # code. The file is the authority when it exists; the exit code is the
    # fallback, and both have to agree for a pass.
    status_file = outdir / "status"
    status = status_file.read_text().strip().upper() if status_file.is_file() else ""
    passed = rc == 0 and (status.startswith("PASS") if status else True)

    return {
        "equivalent": bool(passed),
        "method": "eqy" if passed else "eqy-fail",
        "reason": (f"eqy reported {status.lower() or ('pass' if passed else 'fail')} "
                   f"(exit {rc})"),
        "depth": depth,
        "engine": "eqy",
        "runtime_s": runtime,
        "log": str(log),
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def unsupported(reason: str, engine: str, **extra: Any) -> dict[str, Any]:
    """A check that was not run because it would not have meant anything.

    Distinct from ``_fail``: ``method="unsupported"`` is undecided, not a
    refutation, so it neither promotes a candidate nor teaches the skill
    library that a sound transformation breaks equivalence.
    """
    return {"equivalent": False, "method": "unsupported", "reason": reason,
            "engine": engine, **extra}


def check(top: str, gold: list[Path], gate: list[Path], workdir: Path,
          depth: int = DEFAULT_DEPTH, liberty: Path | None = None,
          engine: str = "auto", timeout: int = 1800,
          clock_set: Any = None) -> dict[str, Any]:
    """Run SEC and return a verdict dict. Never raises on an unequal design.

    ``engine="auto"`` prefers eqy and falls back to the Yosys miter -- but
    only when eqy is *absent*, not when it ran and said no. An eqy verdict of
    "not equivalent" is a verdict, and retrying it on a weaker engine until
    one of them agrees would defeat the point of the constraint.

    ``clock_set`` enforces the equivalence contract. Both engines build a
    miter over a common clock, so on a genuinely multi-clock design the
    question they answer is not the question that was asked -- see
    docs/equivalence-contract.md. Rather than return a confident answer to the
    wrong question, the check declines and says so.
    """
    missing = [str(p) for p in (*gold, *gate) if not Path(p).is_file()]
    if missing:
        return _fail(f"missing source file(s): {', '.join(missing)}", engine)

    if clock_set is not None and getattr(clock_set, "is_multi", False):
        names = ", ".join(getattr(clock_set, "names", lambda: [])())
        return unsupported(
            f"the design has {len(clock_set)} clocks ({names}); both engines "
            f"build a miter over one common clock, so a verdict here would "
            f"not be a statement about the design. Cut the CDC boundaries and "
            f"check each domain separately -- see docs/equivalence-contract.md",
            engine, clocks=len(clock_set))

    # When tools run elsewhere the host cannot see which engines exist, so
    # "auto" resolves to the one that is always present. Ask for eqy by name
    # to use it in that setup.
    if engine == "eqy":
        if not toolenv.have("eqy"):
            return _fail("eqy requested but not installed", "eqy")
        return check_eqy(top, gold, gate, workdir, depth, timeout)
    if engine == "auto" and not toolenv.dispatching() and shutil.which("eqy"):
        return check_eqy(top, gold, gate, workdir, depth, timeout)
    return check_yosys(top, gold, gate, workdir, depth, liberty, timeout)


def available() -> dict[str, bool]:
    return {"eqy": toolenv.have("eqy"), "yosys": toolenv.have("yosys")}


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="astra-sec",
        description="Sequential equivalence check between two RTL designs.")
    ap.add_argument("top", help="top module name (identical on both sides)")
    ap.add_argument("--gold", nargs="+", required=True, type=Path)
    ap.add_argument("--gate", nargs="+", required=True, type=Path)
    ap.add_argument("--workdir", type=Path, default=ROOT / "runs" / "_sec")
    ap.add_argument("--depth", type=int, default=DEFAULT_DEPTH)
    ap.add_argument("--liberty", type=Path, default=None)
    ap.add_argument("--engine", choices=("auto", "eqy", "yosys"), default="auto")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = check(args.top, args.gold, args.gate, args.workdir,
                args.depth, args.liberty, args.engine, args.timeout)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        verdict = "EQUIVALENT" if res["equivalent"] else "NOT EQUIVALENT"
        print(f"[sec] {verdict}  ({res['engine']}/{res['method']})")
        print(f"      {res.get('reason', '')}")
        if res.get("log"):
            print(f"      log: {res['log']}")
    return 0 if res["equivalent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
