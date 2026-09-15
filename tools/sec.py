#!/usr/bin/env python3
"""Sequential equivalence checking -- the SEC_i constraint of Eq. 4.

The paper uses commercial SEC. Here there are three open-source routes:

    regcorr  register correspondence (tools/regcorr.py + flow/scripts/
             sec_regcorr.tcl). Multi-clock designs try this first. Registers
             are paired by name and cut open, which turns the sequential
             question into a combinational one; everything textually
             unchanged is settled by exact structural hashing, and only the
             edited cone reaches a solver. An unbounded proof that holds for
             every clock interleaving -- see docs/equivalence-contract.md.
             It can prove, never refute: anything left unproven falls
             through to the bounded check below.
    eqy      the SymbiYosys equivalence front end. Preferred for single-clock
             designs when installed: it partitions the design and discharges
             each partition separately.
    yosys    a miter discharged by Yosys's own SAT engine, via
             flow/scripts/sec.tcl. Always available, since Yosys is already a
             hard dependency of the flow.

All report the same shape. The one field worth reading carefully is
``method``: ``regcorr``, ``induction`` and eqy's ``pass`` are unbounded proofs,
``identical`` means the texts match, whereas ``bounded`` only says no
counterexample exists within N cycles. The orchestrator records the difference
rather than flattening it, because promoting a design on a bounded check is a
weaker claim than the paper makes.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse_sta  # noqa: E402
import regcorr    # noqa: E402
import toolenv    # noqa: E402

ROOT = Path(os.environ.get("ASTRA_ROOT", Path(__file__).resolve().parent.parent))
FLOW = ROOT / "flow"

DEFAULT_DEPTH = 20

# Register correspondence normally decides in seconds; this only bounds a
# pathological cone. The bounded fallback is the expensive part, and 0 skips it
# on designs where it is known not to finish.
REGCORR_TIMEOUT = 300
FALLBACK_TIMEOUT = 600

# Methods that did not reach a verdict. Mirrors score.sec_decided, and is what
# the verdict cache refuses to remember.
UNDECIDED = ("error", "skipped", "unsupported", "regcorr-unproven", None)

# A note on depth, because the obvious optimisation here is a trap.
#
# Multi-clock checks that fall through to clk2fflogic pay two solver steps per
# edge and get no induction, so the whole budget goes into a bounded unrolling
# that copies the design once per step. On anything with unpipelined
# multipliers that gets expensive fast, and the tempting fix is to cap the
# depth so the check finishes.
#
# Do not. It was measured on dual_clock (four 12x12 multipliers):
#
#   depth 8  refutes a broken candidate in 9 s; does not prove a good one in 900 s
#   depth 3  decides neither within 200 s
#   depth 2  proves a good candidate in 2 s -- AND "proves" the broken one too
#
# Four steps cannot reach the difference, so the shallow check reports
# equivalent for a design that is not. A bounded pass is only worth what its
# depth can see, and a cap tuned to make proofs finish is tuned to make them
# vacuous. Timing out is undecided and promotes nothing, which is the correct
# failure; passing wrongly promotes a broken candidate, which is not.
#
# So the depth stays where the caller put it. The way out of the asymmetry is
# not a shallower bounded check but a different question: register
# correspondence, tried first, whose cost follows the size of the edit and
# which needs no depth at all. The bounded check is what remains for the
# candidates it cannot decide -- retimed ones -- and for refutation.

_FALLBACK_LOCK = threading.Lock()   # whole-design clk2fflogic: one at a time
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()


def _fail(reason: str, engine: str, **extra: Any) -> dict[str, Any]:
    return {"equivalent": False, "method": "error", "reason": reason,
            "engine": engine, **extra}


def _run(cmd: list[str], log: Path, env: dict[str, str] | None = None,
         cwd: Path | None = None, timeout: int = 1800) -> tuple[int, str]:
    log.parent.mkdir(parents=True, exist_ok=True)
    # Enforce the limit where the tool runs, not only here. Killing a
    # `docker run` client on timeout leaves its container grinding: measured,
    # a regcorr solver kept a core busy for 16 minutes after its 300 s budget.
    # coreutils `timeout` is in the image; the grace period lets it fire first.
    killer = toolenv.dispatching() or bool(shutil.which("timeout"))
    if killer:
        cmd = ["timeout", "-s", "KILL", str(max(1, int(timeout))), *cmd]
    cmd, env = toolenv.wrap(cmd, env)
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout + 15,
                              env={**os.environ, **(env or {})},
                              cwd=str(cwd or ROOT))
    except subprocess.TimeoutExpired:
        log.write_text(f"# cmd: {' '.join(cmd)}\n# TIMEOUT after {timeout}s\n")
        return 124, ""
    out = (proc.stdout or "") + (proc.stderr or "")
    if killer and proc.returncode == 137 and time.time() - t0 >= timeout - 1:
        # SIGKILL at the budget is our own `timeout`, not the OOM killer --
        # report it as the timeout it is. An earlier 137 stays a 137.
        log.write_text(f"# cmd: {' '.join(cmd)}\n{out}\n# TIMEOUT after {timeout}s\n")
        return 124, out
    log.write_text(f"# cmd: {' '.join(cmd)}\n{out}\n# exit code: {proc.returncode}\n")
    return proc.returncode, out


# ---------------------------------------------------------------------------
# free verdicts: identical text, and verdicts already reached
# ---------------------------------------------------------------------------


def normalise_verilog(text: str) -> str:
    """Verilog with comments removed and whitespace collapsed."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return " ".join(text.split())


def identical(gold: list[Path], gate: list[Path]) -> bool:
    """Do the two source lists say the same thing, file by file?

    On dual_clock every specialist returned its parent unchanged; each of those
    used to cost a full bounded proof to establish what a string comparison
    does.
    """
    if len(gold) != len(gate):
        return False
    return all(normalise_verilog(Path(a).read_text(errors="replace"))
               == normalise_verilog(Path(b).read_text(errors="replace"))
               for a, b in zip(gold, gate))


def _digest(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(hashlib.sha256(Path(p).read_bytes()).digest())
    return h.hexdigest()


def _cache_key(top: str, gold: list[Path], gate: list[Path], **how: Any) -> str:
    return json.dumps({"top": top, "gold": _digest(gold), "gate": _digest(gate),
                       **{k: str(v) for k, v in how.items()}}, sort_keys=True)


def _remember(key: str, verdict: dict[str, Any]) -> dict[str, Any]:
    """Cache decided verdicts only. A timeout says nothing about the pair."""
    if verdict.get("equivalent") is True or verdict.get("method") not in UNDECIDED:
        with _CACHE_LOCK:
            _CACHE[key] = copy.deepcopy(verdict)
    return verdict


def clear_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------------------
# yosys miter engine
# ---------------------------------------------------------------------------


def check_yosys(top: str, gold: list[Path], gate: list[Path], workdir: Path,
                depth: int = DEFAULT_DEPTH, liberty: Path | None = None,
                timeout: int = 1800, multiclock: bool = False) -> dict[str, Any]:
    """The Yosys miter. ``multiclock`` turns every clock into a free input via
    ``clk2fflogic``, so a verdict holds for any interleaving of the domains --
    see flow/scripts/sec.tcl and docs/equivalence-contract.md."""
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
        "ASTRA_SEC_MULTICLOCK": "1" if multiclock else "0",
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
    if not kv and rc == 137:
        # SIGKILL, which under a `docker run --memory` cap means the kernel's
        # OOM killer. Measured: the whole-design clk2fflogic miter on netproc
        # dies this way at 6 GB. Undecided, and worth saying plainly.
        return _fail("yosys SEC was killed (exit 137): out of memory under the "
                     "container cap -- raise DOCKER_MEM, or skip the bounded "
                     "fallback with --sec-fallback-timeout 0", "yosys",
                     runtime_s=runtime, log=str(log))
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
        # Not "steps": that key already carries the per-stage step log below.
        "sat_steps": kv.get("steps"),
        "multiclock": bool(kv.get("multiclock")),
        "engine": "yosys",
        "runtime_s": runtime,
        "steps": parse_sta.parse_steps(out),
        "log": str(log),
    }


# ---------------------------------------------------------------------------
# register correspondence
# ---------------------------------------------------------------------------


def check_regcorr(top: str, gold: list[Path], gate: list[Path], workdir: Path,
                  timeout: int = REGCORR_TIMEOUT) -> dict[str, Any]:
    """Register correspondence: elaborate (Yosys), pair and cut (Python),
    prove the differing cones (Yosys). Never refutes.

    Returns method ``regcorr`` on a proof, ``regcorr-unproven`` when it could
    not decide (undecided, not a refutation), or ``error`` when a side would
    not elaborate or the tool failed; ``elaboration_failed`` marks the case
    where a weaker check would fail identically and is not worth running.
    """
    if not toolenv.have("yosys"):
        return _fail("yosys not on PATH", "yosys", stage="regcorr")

    rdir = workdir / "regcorr"
    rdir.mkdir(parents=True, exist_ok=True)
    for stale in ("gold_cone.json", "gate_cone.json", "regcorr_report.json"):
        (rdir / stale).unlink(missing_ok=True)
    t0 = time.time()
    deadline = t0 + timeout
    script = str(FLOW / "scripts" / "sec_regcorr.tcl")
    env = {
        "ASTRA_FLOW": str(FLOW),
        "ASTRA_TOP": top,
        "ASTRA_GOLD_FILES": " ".join(str(p) for p in gold),
        "ASTRA_GATE_FILES": " ".join(str(p) for p in gate),
        "ASTRA_SEC_WORKDIR": str(rdir),
    }
    base: dict[str, Any] = {"engine": "yosys", "stage": "regcorr", "log": str(rdir)}

    def undecided(reason: str, **extra: Any) -> dict[str, Any]:
        return {**base, "equivalent": False, "method": "regcorr-unproven",
                "reason": reason, "runtime_s": round(time.time() - t0, 2), **extra}

    rc, out = _run(["yosys", "-c", script], rdir / "elaborate.log",
                   env={**env, "ASTRA_REGCORR_PHASE": "elaborate"}, timeout=timeout)
    if rc == 124:
        return _fail(f"regcorr elaboration timed out after {timeout}s", "yosys",
                     stage="regcorr", runtime_s=round(time.time() - t0, 2),
                     log=str(rdir))
    kv = parse_sta.parse_kv(out).get("sec", {})
    if kv.get("method") == "error":
        return {**base, "equivalent": False, "method": "error",
                "reason": kv.get("reason", "elaboration failed"),
                "elaboration_failed": True,
                "runtime_s": round(time.time() - t0, 2)}
    if not kv.get("elaborated"):
        tail = "\n".join(out.strip().splitlines()[-8:])
        return _fail(f"regcorr elaboration produced no verdict (exit {rc}): {tail}",
                     "yosys", stage="regcorr", log=str(rdir),
                     runtime_s=round(time.time() - t0, 2))

    try:
        rep = regcorr.prepare(rdir / "regcorr_gold.json", rdir / "regcorr_gate.json",
                              top, rdir)
    except (regcorr.RegcorrError, OSError, ValueError, KeyError) as e:
        return undecided(f"register correspondence could not prepare: {e}")

    base.update({k: rep[k] for k in ("paired", "unpaired_gold", "unpaired_gate",
                                     "ports_total", "ports_structural")
                 if k in rep})
    base["sat_ports"] = list(rep.get("sat_ports", []))[:20]

    if rep["status"] == "unsupported":
        return undecided("register correspondence does not apply: " + rep["reason"])
    if rep["status"] == "proven":
        return {**base, "equivalent": True, "method": "regcorr",
                "unproven": 0, "equiv_cells": 0,
                "runtime_s": round(time.time() - t0, 2),
                "reason": ("register correspondence: " + rep["reason"]
                           + "; unbounded, holds for every clock interleaving")}

    remaining = int(deadline - time.time())
    if remaining <= 0:
        return undecided(f"regcorr ran out of its {timeout}s budget before the solver")
    rc, out = _run(["yosys", "-c", script], rdir / "prove.log",
                   env={**env, "ASTRA_REGCORR_PHASE": "prove"}, timeout=remaining)
    if rc == 124:
        return undecided(f"regcorr solver timed out after {timeout}s on "
                         f"{len(rep['sat_ports'])} differing port(s)")
    kv = parse_sta.parse_kv(out).get("sec", {})
    passed = str(kv.get("equivalent")) in ("1", "True", "true")
    unproven = kv.get("unproven")
    # Belt and braces: a pass needs a readable, nonzero equiv count with none
    # left unproven, whatever the method line says.
    if passed and not (isinstance(unproven, int) and unproven == 0
                       and isinstance(kv.get("equiv_cells"), int)
                       and kv["equiv_cells"] > 0):
        passed = False
    verdict = {**base, "equivalent": passed,
               "method": "regcorr" if passed else "regcorr-unproven",
               "reason": kv.get("reason") or
               "\n".join(out.strip().splitlines()[-8:]),
               "unproven": unproven, "equiv_cells": kv.get("equiv_cells"),
               "attempt": kv.get("attempt"),
               "runtime_s": round(time.time() - t0, 2),
               "steps": parse_sta.parse_steps(out)}
    if passed:
        verdict["reason"] = (f"{rep['ports_structural']} of {rep['ports_total']} "
                             f"outputs matched structurally; " + verdict["reason"])
    return verdict


def _summary(v: dict[str, Any]) -> dict[str, Any]:
    keep = ("method", "reason", "runtime_s", "paired", "unpaired_gold",
            "unpaired_gate", "ports_total", "ports_structural", "sat_ports",
            "unproven", "equiv_cells", "log")
    return {k: v[k] for k in keep if k in v}


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
          clock_set: Any = None, regcorr_timeout: int = REGCORR_TIMEOUT,
          fallback_timeout: int = FALLBACK_TIMEOUT) -> dict[str, Any]:
    """Run SEC and return a verdict dict. Never raises on an unequal design.

    ``engine="auto"`` prefers eqy on single-clock designs and falls back to
    the Yosys miter -- but only when eqy is *absent*, not when it ran and said
    no. An eqy verdict of "not equivalent" is a verdict, and retrying it on a
    weaker engine until one of them agrees would defeat the point of the
    constraint.

    ``clock_set`` decides how the question is asked. `sat` models a $dff as
    "Q at t+1 is D at t" and ignores the clock, which assumes every flop ticks
    together -- true of one clock, false of five. So a multi-clock design is
    asked two questions, cheap one first:

      1. register correspondence (``regcorr``): pair registers by name, cut
         them open, prove the edited cone. Unbounded, holds for every clock
         interleaving, costs what the edit costs. It can only prove.
      2. only if that leaves something unproven: the whole-design
         ``clk2fflogic`` miter, every clock a free input. Bounded, since
         induction does not converge with free clocks; this is what refutes.
         It runs one at a time (``_FALLBACK_LOCK``) because several at once is
         what OOM-killed the VM, under ``fallback_timeout``; 0 skips it and
         leaves the verdict undecided.

    ``timeout`` bounds the single-clock check, as before.

    Before any of that: identical text is equivalent without a tool, and a
    verdict already reached for the same pair of texts is reused.
    """
    missing = [str(p) for p in (*gold, *gate) if not Path(p).is_file()]
    if missing:
        return _fail(f"missing source file(s): {', '.join(missing)}", engine)

    multiclock = bool(clock_set is not None
                      and getattr(clock_set, "is_multi", False))
    if multiclock and engine == "eqy":
        # eqy partitions against a common clock; it has no multiclock mode, so
        # asking it here would answer the wrong question confidently.
        return unsupported(
            "eqy cannot check a multi-clock design; register correspondence "
            "and the Yosys miter's clk2fflogic path can -- use engine=auto",
            "eqy", clocks=len(clock_set))

    if liberty is None and identical(gold, gate):
        return {"equivalent": True, "method": "identical", "engine": "none",
                "runtime_s": 0.0, "multiclock": multiclock,
                "reason": "the candidate's text matches the reference once "
                          "comments and whitespace are ignored"}

    key = _cache_key(top, gold, gate, depth=depth, liberty=liberty,
                     engine=engine, multiclock=multiclock)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return {**copy.deepcopy(hit), "cached": True}

    if multiclock and liberty is None and engine in ("auto", "yosys"):
        rc = check_regcorr(top, gold, gate, workdir, timeout=regcorr_timeout)
        if rc.get("equivalent") is True:
            return _remember(key, rc)
        if rc.get("elaboration_failed"):
            # The bounded miter would fail to elaborate the same text.
            return rc
        if fallback_timeout <= 0:
            rc["reason"] = (rc.get("reason", "")
                            + "; the bounded fallback is disabled (timeout 0)")
            return rc
        with _FALLBACK_LOCK:
            v = check_yosys(top, gold, gate, workdir, depth, liberty,
                            fallback_timeout, multiclock=True)
        v["regcorr"] = _summary(rc)
        return _remember(key, v)

    # When tools run elsewhere the host cannot see which engines exist, so
    # "auto" resolves to the one that is always present. Ask for eqy by name
    # to use it in that setup.
    if engine == "eqy":
        if not toolenv.have("eqy"):
            return _fail("eqy requested but not installed", "eqy")
        return _remember(key, check_eqy(top, gold, gate, workdir, depth, timeout))
    if engine == "auto" and not multiclock \
            and not toolenv.dispatching() and shutil.which("eqy"):
        return _remember(key, check_eqy(top, gold, gate, workdir, depth, timeout))

    return _remember(key, check_yosys(top, gold, gate, workdir, depth, liberty,
                                      timeout, multiclock=multiclock))


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
    ap.add_argument("--clocks-from", type=Path, default=None, metavar="CONFIG",
                    help="a design config.json; its clocks decide whether the "
                         "multi-clock path (regcorr, then bounded) is taken")
    ap.add_argument("--regcorr-timeout", type=int, default=REGCORR_TIMEOUT)
    ap.add_argument("--fallback-timeout", type=int, default=FALLBACK_TIMEOUT,
                    help="bounded multi-clock fallback; 0 skips it")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    clock_set = None
    if args.clocks_from:
        import clocks
        clock_set = clocks.ClockSet.from_config(json.loads(args.clocks_from.read_text()))

    res = check(args.top, args.gold, args.gate, args.workdir,
                args.depth, args.liberty, args.engine, args.timeout,
                clock_set=clock_set, regcorr_timeout=args.regcorr_timeout,
                fallback_timeout=args.fallback_timeout)
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
