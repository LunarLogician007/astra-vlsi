#!/usr/bin/env python3
"""ASTRA flow driver: design -> synthesis -> timing report.

    astra doctor            check tools and PDKs
    astra list              show designs and past runs
    astra syn   <design>    Yosys synthesis
    astra sta   <design>    OpenSTA timing report
    astra run   <design>    synthesis + timing (add --pnr for place & route)
    astra report <design>   print the run's metrics.json

The Dr. RTL optimisation loop is built on top of those stages:

    astra localise <design> map the critical path back onto RTL lines
    astra score  <design>   Eq. 3 score of a run against a baseline run
    astra sec    <top>      sequential equivalence check between two designs
    astra skills            inspect the learned skill library
    astra opt    <design>   the closed loop: analyse -> rewrite -> evaluate

and the path-portfolio addon on top of that:

    astra scan   <design>   structural smells in the RTL, before any tool runs
    astra paths  <design>   the distinct critical-path targets worth an agent
    astra skilldoc          build the RTL timing-optimisation skill document
    astra portfolio <design>  k scoped specialists, then a merge

Outputs land in runs/<design>/<timestamp>/. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clocks as clocks_mod  # noqa: E402
import parse_sta  # noqa: E402
import toolenv    # noqa: E402

ROOT = Path(os.environ.get("ASTRA_ROOT", Path(__file__).resolve().parent.parent))
FLOW = ROOT / "flow"
DESIGNS = ROOT / "designs"
RUNS = ROOT / "runs"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _c(code: str, text: str) -> str:
    return text if not sys.stdout.isatty() else f"\033[{code}m{text}\033[0m"


def info(msg: str) -> None:
    print(_c("36", "[astra] ") + msg, flush=True)


def warn(msg: str) -> None:
    print(_c("33", "[astra] WARNING: ") + msg, flush=True)


def die(msg: str) -> None:
    print(_c("31", "[astra] ERROR: ") + msg, file=sys.stderr, flush=True)
    raise SystemExit(1)


def fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    return f"{v:.4f}" if isinstance(v, float) else str(v)


def run_tool(cmd: list[str], log_path: Path, env: dict[str, str],
             quiet: bool = False) -> tuple[int, str]:
    """Run a tool, tee its output to a log, return (exit code, text)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # ASTRA_TOOL_PREFIX, when set, rewrites this into a `docker run ...` that
    # carries the same command with container-side paths. Unset -- the normal
    # case inside the image -- cmd and env come back untouched.
    cmd, env = toolenv.wrap(cmd, env)
    info("$ " + " ".join(cmd))
    chunks: list[str] = []
    with open(log_path, "w", encoding="utf-8") as log:
        log.write("# cmd: " + " ".join(cmd) + "\n")
        for k in sorted(env):
            log.write(f"# {k}={env[k]}\n")
        log.flush()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env={**os.environ, **env}, cwd=str(ROOT))
        assert proc.stdout is not None
        for line in proc.stdout:
            chunks.append(line)
            log.write(line)
            if not quiet:
                sys.stdout.write("    " + line)
                sys.stdout.flush()
        rc = proc.wait()
        log.write(f"\n# exit code: {rc}\n")
    return rc, "".join(chunks)


def load_design(name: str) -> dict[str, Any]:
    cfg_path = DESIGNS / name / "config.json"
    if not cfg_path.exists():
        have = sorted(p.parent.name for p in DESIGNS.glob("*/config.json"))
        die(f"no design '{name}'.\n        available: {', '.join(have) or '(none)'}")
    cfg = json.loads(cfg_path.read_text())
    cfg["_dir"] = DESIGNS / name
    cfg["_name"] = name
    for key in ("top", "rtl", "sdc"):
        if key not in cfg:
            die(f"design '{name}': config.json needs a '{key}' key")
    cfg.setdefault("pdk", "nangate45")
    try:
        cs = clocks_mod.ClockSet.from_config(cfg)
    except clocks_mod.ClockError as e:
        die(f"design '{name}': {e}")
    # Both shapes are kept live: `_clocks` is the whole truth, and the
    # singular `clock` stays a one-element alias so every pre-multi-clock
    # reader keeps working unchanged.
    cfg["_clocks"] = cs
    cfg["clock"] = {"name": cs.primary.name, "period_ns": cs.primary.period_ns,
                    **({"port": cs.primary.port} if cs.primary.port else {})}
    cfg["clocks"] = [c.to_dict() for c in cs]
    return cfg


def _clock_deltas(base: Any, cand: Any) -> list[tuple[str, str, str]]:
    """Every clock that differs between two runs: (name, baseline, candidate).

    Covers appearing and disappearing clocks as well as changed periods, since
    a run that dropped a domain entirely is no more comparable than one that
    retimed it.
    """
    if base is None or cand is None:
        return []
    b = {c.name: c.period_ns for c in base}
    c = {c.name: c.period_ns for c in cand}
    out = []
    for name in sorted(set(b) | set(c)):
        was, now = b.get(name), c.get(name)
        if was is None or now is None:
            out.append((name, "absent" if was is None else f"{was:g} ns",
                        "absent" if now is None else f"{now:g} ns"))
        elif abs(was - now) > 1e-9:
            out.append((name, f"{was:g} ns", f"{now:g} ns"))
    return out


def design_clocks(cfg: dict[str, Any], period: float | None = None):
    """The design's ClockSet, with a --period override applied if given."""
    cs = cfg.get("_clocks") or clocks_mod.ClockSet.from_config(cfg)
    if period is None:
        return cs
    return cs.with_period(period)


def pdk_config(pdk: str) -> Path:
    path = FLOW / "config" / f"{pdk}.tcl"
    if not path.exists():
        have = sorted(p.stem for p in (FLOW / "config").glob("*.tcl") if p.stem != "common")
        die(f"unknown PDK '{pdk}'. available: {', '.join(have)}")
    return path


def new_run(design: str, tag: str | None) -> Path:
    rdir = RUNS / design / (datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{tag}" if tag else ""))
    rdir.mkdir(parents=True, exist_ok=True)
    link = RUNS / design / "latest"
    if link.is_symlink() or link.exists():
        try:
            link.unlink()
        except OSError:
            pass
    try:
        link.symlink_to(rdir.name)
    except OSError:
        pass
    return rdir


def find_run(design: str, run: str) -> Path:
    base = RUNS / design
    if run == "latest":
        runs = sorted(p for p in base.glob("*") if p.is_dir() and not p.is_symlink())
        if not runs:
            die(f"no runs for '{design}' yet. Try: astra run {design}")
        return runs[-1]
    path = base / run
    if not path.is_dir():
        die(f"no such run: {path}")
    return path


def read_metrics(rdir: Path) -> dict[str, Any]:
    path = rdir / "metrics.json"
    return json.loads(path.read_text()) if path.exists() else {}


def write_metrics(rdir: Path, m: dict[str, Any]) -> None:
    (rdir / "metrics.json").write_text(json.dumps(m, indent=2) + "\n")


def base_env(cfg: dict[str, Any]) -> dict[str, str]:
    return {
        "ASTRA_FLOW": str(FLOW),
        "ASTRA_PDK_CONFIG": str(pdk_config(cfg["pdk"])),
        "ASTRA_TOP": cfg["top"],
    }


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


def stage_inputs(cfg: dict[str, Any], rdir: Path, period: float) -> dict[str, Any]:
    """Copy the RTL and SDC this run uses into 00_inputs/.

    Keeps a run's numbers tied to the exact source that produced them.
    """
    idir = rdir / "00_inputs"
    idir.mkdir(parents=True, exist_ok=True)

    rtl: list[str] = []
    for rel in cfg["rtl"]:
        src = (cfg["_dir"] / rel).resolve()
        if not src.exists():
            die(f"RTL file not found: {src}")
        dst = idir / src.name
        shutil.copy2(src, dst)
        rtl.append(str(dst))

    sdc_src = (cfg["_dir"] / cfg["sdc"]).resolve()
    if not sdc_src.exists():
        die(f"SDC file not found: {sdc_src}")
    text = sdc_src.read_text()
    cs = design_clocks(cfg, period)
    templated = any(tag in text for tag in
                    ("@CLK_PERIOD@", "@ASTRA_CLOCK_DEFS@", "@CLK_PERIOD:"))
    if templated:
        stale = clocks_mod.unresolved_placeholders(
            clocks_mod.substitute(text, cs))
        if stale:
            die(f"{sdc_src.name} references undeclared clock(s): "
                f"{', '.join(stale)}\n        declared: {', '.join(cs.names())}")
        text = clocks_mod.substitute(text, cs)
    else:
        if abs(period - float(cfg["clock"]["period_ns"])) > 1e-9:
            warn(f"{sdc_src.name} has no @CLK_PERIOD@ placeholder, so --period only "
                 f"changes the synthesis delay target, not the constraint")
        if cs.is_multi:
            warn(f"{sdc_src.name} declares its clocks by hand, so the "
                 f"{len(cs)} clocks in config.json are not applied to it; add "
                 f"@ASTRA_CLOCK_DEFS@ to generate them")
    sdc = idir / sdc_src.name
    sdc.write_text(text)

    return {"rtl": rtl, "sdc": str(sdc)}


def do_syn(cfg: dict[str, Any], rdir: Path, args: argparse.Namespace,
           period: float) -> dict[str, Any]:
    odir = rdir / "01_synth"
    odir.mkdir(parents=True, exist_ok=True)
    metrics = read_metrics(rdir)
    inputs = metrics["inputs"]

    cs = design_clocks(cfg, period)
    if cs.is_multi:
        info(f"synthesis delay target: {cs.synthesis_period:g} ns "
             f"(tightest of {len(cs)} clocks); slower domains are "
             f"over-constrained -- see docs/multi-clock.md")
    env = base_env(cfg) | {
        "ASTRA_OUT_DIR": str(odir),
        "ASTRA_RTL_FILES": " ".join(inputs["rtl"]),
        "ASTRA_CLK_PERIOD": str(cs.synthesis_period),
        "ASTRA_FLATTEN": "0" if args.no_flatten else "1",
    }

    t0 = time.time()
    rc, out = run_tool(["yosys", "-c", str(FLOW / "scripts" / "synth.tcl")],
                       rdir / "logs" / "synth.log", env, args.quiet)
    stage = {"status": "ok" if rc == 0 else "failed",
             "runtime_s": round(time.time() - t0, 2),
             "netlist": str(odir / "netlist.v")}
    stage.update(synth_stats(out))
    metrics["synth"] = stage
    write_metrics(rdir, metrics)

    if rc != 0:
        die(f"yosys failed (exit {rc}) — see {rdir / 'logs' / 'synth.log'}")
    info(f"synthesis ok in {stage['runtime_s']}s: "
         f"{stage.get('cells')} cells, area {fmt(stage.get('area_um2'))} um^2")
    return stage


def synth_stats(log: str) -> dict[str, Any]:
    text = parse_sta.split_sections(log).get("synth_stat", log)
    out: dict[str, Any] = {}
    for line in text.splitlines():
        s = line.strip()
        for prefix, key, cast in (("Chip area for", "area_um2", float),
                                  ("Number of cells:", "cells", int),
                                  ("Number of wires:", "wires", int)):
            if s.startswith(prefix):
                try:
                    out[key] = cast(s.split(":")[-1].strip())
                except ValueError:
                    pass
    return out


def sta_binary() -> list[str]:
    if toolenv.dispatching():
        # The probe would run on the host, where neither binary exists.
        return ["sta", "-no_splash", "-exit"]
    if shutil.which("sta"):
        return ["sta", "-no_splash", "-exit"]
    if shutil.which("openroad"):
        warn("no standalone `sta`; using the OpenSTA engine inside openroad")
        return ["openroad", "-no_init", "-exit"]
    die("neither `sta` nor `openroad` found — are you inside the container?")
    return []


def do_sta(cfg: dict[str, Any], rdir: Path, args: argparse.Namespace) -> dict[str, Any]:
    metrics = read_metrics(rdir)
    netlist = metrics.get("synth", {}).get("netlist")
    if not netlist or not Path(netlist).exists():
        die(f"no netlist in {rdir.name}. Run: astra syn {cfg['_name']}")

    odir = rdir / "02_sta"
    odir.mkdir(parents=True, exist_ok=True)
    env = base_env(cfg) | {
        "ASTRA_NETLIST": netlist,
        "ASTRA_SDC": metrics["inputs"]["sdc"],
        "ASTRA_NPATHS": str(args.npaths),
        "ASTRA_TAG": "post_synth",
    }

    t0 = time.time()
    rc, out = run_tool(sta_binary() + [str(FLOW / "scripts" / "sta.tcl")],
                       rdir / "logs" / "sta.log", env, args.quiet)

    parsed = parse_sta.parse_log(out, "post_synth")
    sections = parsed.pop("sections", {})
    (odir / "timing.rpt").write_text(sections.get("post_synth_paths", "") + "\n")
    (odir / "check_types.rpt").write_text(sections.get("post_synth_check_types", "") + "\n")
    (odir / "timing.json").write_text(json.dumps(parsed, indent=2) + "\n")

    s = parsed["summary"]
    stage = {"status": "ok" if rc == 0 else "failed",
             "runtime_s": round(time.time() - t0, 2),
             "wns_ns": s.get("wns_ns"), "tns_ns": s.get("tns_ns"),
             "violating_endpoints": s.get("violating_endpoints"),
             "hold_wns_ns": (s.get("hold") or {}).get("wns_ns"),
             "clock_groups": parsed.get("clock_groups"),
             "report": str(odir / "timing.rpt"),
             "timing_json": str(odir / "timing.json")}
    metrics["sta"] = stage
    write_metrics(rdir, metrics)

    if rc != 0:
        die(f"STA failed (exit {rc}) — see {rdir / 'logs' / 'sta.log'}")
    print_timing("post-synthesis", stage, parsed)
    return stage


def do_pnr(cfg: dict[str, Any], rdir: Path, args: argparse.Namespace) -> dict[str, Any]:
    metrics = read_metrics(rdir)
    netlist = metrics.get("synth", {}).get("netlist")
    if not netlist or not Path(netlist).exists():
        die(f"no netlist in {rdir.name}. Run: astra syn {cfg['_name']}")
    if not shutil.which("openroad"):
        die("place & route needs OpenROAD, which is not in the lean image.\n"
            "        Build the full one:  make build DOCKERFILE=docker/Dockerfile.orfs")

    odir = rdir / "03_pnr"
    odir.mkdir(parents=True, exist_ok=True)
    env = base_env(cfg) | {
        "ASTRA_NETLIST": netlist,
        "ASTRA_SDC": metrics["inputs"]["sdc"],
        "ASTRA_OUT_DIR": str(odir),
        "ASTRA_NPATHS": str(args.npaths),
        "ASTRA_DETAILED_ROUTE": "1" if args.detailed_route else "0",
        "ASTRA_REPAIR_TIMING": "0" if args.no_repair else "1",
    }

    t0 = time.time()
    rc, out = run_tool(["openroad", "-no_init", "-exit", str(FLOW / "scripts" / "pnr.tcl")],
                       rdir / "logs" / "pnr.log", env, args.quiet)

    tag = "post_route" if args.detailed_route else "post_gr"
    parsed = parse_sta.parse_log(out, tag)
    sections = parsed.pop("sections", {})
    (odir / "timing.rpt").write_text(sections.get(f"{tag}_paths", "") + "\n")
    (odir / "timing.json").write_text(json.dumps(parsed, indent=2) + "\n")
    (odir / "area.rpt").write_text(sections.get("pnr_area", "") + "\n")

    failed = [s["name"] for s in parsed.get("steps", []) if s["status"] == "failed"]
    s = parsed["summary"]
    stage = {"status": "ok" if rc == 0 else "failed",
             "runtime_s": round(time.time() - t0, 2),
             "stage": tag, "failed_steps": failed,
             "wns_ns": s.get("wns_ns"), "tns_ns": s.get("tns_ns"),
             "def": str(odir / "final.def"), "report": str(odir / "timing.rpt")}
    metrics["pnr"] = stage
    write_metrics(rdir, metrics)

    if failed:
        warn(f"PnR steps that did not complete: {', '.join(failed)}")
    if rc != 0:
        die(f"OpenROAD failed (exit {rc}) — see {rdir / 'logs' / 'pnr.log'}")
    print_timing(tag.replace("_", "-"), stage, parsed)
    return stage


def print_timing(label: str, stage: dict[str, Any], parsed: dict[str, Any]) -> None:
    wns = stage.get("wns_ns")
    met = wns is not None and wns >= 0
    info(f"{label} timing: " + _c("32" if met else "31", "MET" if met else "VIOLATED"))
    print(f"    WNS = {fmt(wns)} ns    TNS = {fmt(stage.get('tns_ns'))} ns"
          f"    violating endpoints = {stage.get('violating_endpoints')}")
    worst = parsed.get("worst_path")
    if worst:
        print(f"    worst path: {worst.get('startpoint')} -> {worst.get('endpoint')}"
              f"  (logic depth {worst.get('logic_depth')})")
    print(f"    report: {stage.get('report')}")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_design(args.design)
    if args.pdk:
        cfg["pdk"] = args.pdk
    if args.rtl:
        cfg["rtl"] = [str(Path(p).resolve()) for p in args.rtl]
    period = args.period if args.period is not None else float(cfg["clock"]["period_ns"])

    rdir = new_run(args.design, args.tag)
    info(f"run: {rdir}")
    write_metrics(rdir, {
        "design": args.design, "run_id": rdir.name, "pdk": cfg["pdk"], "top": cfg["top"],
        **design_clocks(cfg, period).to_metrics(),
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "inputs": stage_inputs(cfg, rdir, period),
    })

    do_syn(cfg, rdir, args, period)
    if args.stage == "syn":
        return 0
    do_sta(cfg, rdir, args)
    if args.pnr:
        do_pnr(cfg, rdir, args)
    return 0


def cmd_sta(args: argparse.Namespace) -> int:
    cfg = load_design(args.design)
    do_sta(cfg, find_run(args.design, args.run), args)
    return 0


def cmd_pnr(args: argparse.Namespace) -> int:
    cfg = load_design(args.design)
    do_pnr(cfg, find_run(args.design, args.run), args)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    for d in sorted(DESIGNS.glob("*/config.json")):
        cfg = json.loads(d.read_text())
        name = d.parent.name
        try:
            cs = clocks_mod.ClockSet.from_config(cfg)
            clk = (f"period={cs.primary.period_ns:g}ns" if not cs.is_multi else
                   "clocks=" + ",".join(f"{c.name}@{c.period_ns:g}ns" for c in cs))
        except clocks_mod.ClockError as e:
            clk = f"clocks=INVALID ({e})"
        print(_c("1", f"{name}") + f"  top={cfg.get('top')}  "
              f"pdk={cfg.get('pdk', 'nangate45')}  {clk}")
        runs = sorted(p for p in (RUNS / name).glob("*") if p.is_dir() and not p.is_symlink())
        for r in runs[-args.limit:]:
            m = read_metrics(r)
            sta, syn = m.get("sta", {}), m.get("synth", {})
            print(f"    {r.name:<26} WNS={fmt(sta.get('wns_ns')):>9}  "
                  f"TNS={fmt(sta.get('tns_ns')):>10}  area={fmt(syn.get('area_um2'))}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    rdir = find_run(args.design, args.run)
    m = read_metrics(rdir)
    if not m:
        die(f"no metrics.json in {rdir}")
    if args.timing:
        rpt = Path(m.get("sta", {}).get("report", ""))
        print(rpt.read_text() if rpt.exists() else "(no timing report yet)")
    else:
        print(json.dumps(m, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    print(_c("1", "tools") + f"   ({toolenv.where()})")
    # OpenROAD is optional: the lean image does synthesis + timing only.
    # eqy is optional: sec.py falls back to a Yosys miter, which is always
    # available because Yosys is already required.
    for binary, probe, required in (("yosys", ["-V"], True),
                                    ("sta", ["-version"], True),
                                    ("openroad", ["-version"], False),
                                    ("eqy", ["--help"], False)):
        path = shutil.which(binary)
        if not path:
            if required:
                print(f"  {_c('31', 'MISSING')} {binary}")
                ok = False
            else:
                lost = {"openroad": "place & route unavailable",
                        "eqy": "SEC falls back to the Yosys miter"}
                print(f"  {_c('33', '-')}       {binary:<9} not installed "
                      f"({lost.get(binary, 'optional')})")
            continue
        ver = ""
        if probe:
            try:
                ver = subprocess.run([binary, *probe], capture_output=True, text=True,
                                     timeout=120).stdout.strip().splitlines()[0]
            except Exception:
                ver = "(version probe failed)"
        print(f"  {_c('32', 'OK')}      {binary:<9} {path}  {ver}")

    notes = Path("/etc/astra-tools.txt")
    if notes.exists():
        print(_c("1", "\nbuild notes"))
        for line in notes.read_text().splitlines():
            print(f"  {line}")

    print(_c("1", "\npdks"))
    installed: list[str] = []
    cmd = sta_binary() if (shutil.which("sta") or shutil.which("openroad")) else None
    for cfg_file in sorted((FLOW / "config").glob("*.tcl")):
        if cfg_file.stem == "common":
            continue
        print(f"  [{cfg_file.stem}]")
        if not cmd:
            print("    skipped (no STA binary)")
            ok = False
            continue
        rc, out = run_tool(cmd + [str(FLOW / "scripts" / "doctor.tcl")],
                           RUNS / "_doctor" / f"{cfg_file.stem}.log",
                           {"ASTRA_FLOW": str(FLOW), "ASTRA_PDK_CONFIG": str(cfg_file)},
                           quiet=True)
        for line in out.splitlines():
            if line.startswith("  "):
                print(f"  {line}")
        kv = parse_sta.parse_kv(out).get("pdk", {})
        if not kv.get("ok"):
            ok = False
        if kv.get("installed"):
            installed.append(cfg_file.stem)

    print(_c("1", "\ndesigns"))
    for d in sorted(DESIGNS.glob("*/config.json")):
        cfg = json.loads(d.read_text())
        missing = [r for r in cfg.get("rtl", []) if not (d.parent / r).exists()]
        if not (d.parent / cfg.get("sdc", "")).exists():
            missing.append(cfg.get("sdc"))
        print(f"  {_c('32', 'OK') if not missing else _c('31', 'MISSING ' + str(missing))}"
              f"      {d.parent.name}")
        ok = ok and not missing

    if cmd and not installed:
        print(_c("31", "\nno PDK installed — nothing can be synthesized"))
        ok = False
    print("\n" + (_c("32", "all good") if ok else _c("31", "problems above")))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Dr. RTL loop commands
#
# Each delegates to the module that owns the logic rather than re-declaring
# its options here: the sub-tools are runnable on their own, and one parser
# per concept is one place for a flag to drift out of sync.
# ---------------------------------------------------------------------------


def cmd_opt(args: argparse.Namespace) -> int:
    import drrtl
    return drrtl.Orchestrator(drrtl.build_parser().parse_args(args.rest)).run()


def cmd_sec(args: argparse.Namespace) -> int:
    import sec as sec_mod
    saved, sys.argv = sys.argv, ["astra-sec", *args.rest]
    try:
        return sec_mod.main()
    finally:
        sys.argv = saved


def cmd_skills(args: argparse.Namespace) -> int:
    import skills as skills_mod
    return skills_mod.main(["astra-skills", *args.rest])


def cmd_localise(args: argparse.Namespace) -> int:
    import rtl_map
    cfg = load_design(args.design)
    rdir = find_run(args.design, args.run)
    sdir = rtl_map._STAGE_DIRS[args.stage]
    if not (rdir / sdir / "timing.json").is_file():
        die(f"no {sdir}/timing.json in {rdir.name}. Run: astra run "
            f"{args.design}" + (" --pnr" if args.stage == "pnr" else ""))
    data = rtl_map.from_run(rdir, cfg["_dir"], top_k=args.top_k, stage=args.stage)
    print(json.dumps(data, indent=2) if args.json else rtl_map.render(data))
    return 0


def cmd_paths(args: argparse.Namespace) -> int:
    """The path-portfolio: which distinct bottlenecks are worth an agent call."""
    import pathsel
    import skills as skills_mod
    cfg = load_design(args.design)
    rdir = find_run(args.design, args.run)
    lib = None if args.no_skills else skills_mod.SkillLibrary()
    try:
        sel = pathsel.from_run(rdir, cfg["_dir"], k=args.top_k, stage=args.stage,
                               lam=args.mmr_lambda, cluster_at=args.cluster_at,
                               lib=lib, sigma=args.delay_sigma, rho=args.cone_rho)
    except (FileNotFoundError, ValueError) as e:
        die(str(e))
    if args.json:
        out = dict(sel)
        out["targets"] = [t.to_dict() for t in sel["targets"]]
        print(json.dumps(out, indent=2, default=str))
    else:
        print(pathsel.render(sel))
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Structural smells in the source, before synthesis has run."""
    import rtlscan
    cfg = load_design(args.design)
    files = [(cfg["_dir"] / r).resolve() for r in cfg["rtl"]]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        die(f"missing RTL: {', '.join(missing)}")
    report = rtlscan.scan(files)
    print(json.dumps(report, indent=2, default=str) if args.json
          else rtlscan.render(report))
    return 0


def cmd_skilldoc(args: argparse.Namespace) -> int:
    import skillgen
    return skillgen.main(["astra-skilldoc", *args.rest])


def cmd_portfolio(args: argparse.Namespace) -> int:
    import portfolio
    return portfolio.PortfolioOrchestrator(
        portfolio.build_parser().parse_args(args.rest)).run()


def cmd_score(args: argparse.Namespace) -> int:
    """Eq. 3 for one run, measured against a baseline run.

    Both runs must exist; the baseline is what PPA^norm is relative to, so
    scoring a run against itself is 0.0 by construction and scoring against a
    different clock period is meaningless.
    """
    import score as scoring

    def ppa(rdir: Path) -> dict[str, Any]:
        m = read_metrics(rdir)
        if not m:
            die(f"no metrics.json in {rdir}")
        return {"wns_ns": m.get("sta", {}).get("wns_ns"),
                "tns_ns": m.get("sta", {}).get("tns_ns"),
                "area_um2": m.get("synth", {}).get("area_um2"),
                "clock_groups": m.get("sta", {}).get("clock_groups"),
                "_period": (m.get("clock") or {}).get("period_ns"),
                "_clocks": clocks_mod.from_metrics(m),
                "_run": rdir.name}

    cand = ppa(find_run(args.design, args.run))
    base = ppa(find_run(args.design, args.baseline))

    # Compare EVERY clock, not just the primary. Checking one period lets a run
    # whose secondary domains were retimed pass as comparable, and the score
    # then silently compares two different problems -- the failure the single
    # warning below was written to prevent, reintroduced by multi-clock.
    for name, was, now in _clock_deltas(base["_clocks"], cand["_clocks"]):
        warn(f"clock {name}: {was} vs {now} — the normalised score compares "
             f"two different problems")

    weights = dict(scoring.DEFAULT_WEIGHTS)
    for k in ("alpha", "beta", "gamma"):
        v = getattr(args, k, None)
        if v is not None:
            weights[k] = v

    # Cycle-normalised where the per-group data exists, because TNS in
    # nanoseconds sums across domains of different worth. See score.py.
    cs = cand["_clocks"]
    cand_s, base_s = scoring.with_cycles(cand, cs), scoring.with_cycles(base, cs)
    if scoring.cycles_available(cand_s, base_s):
        cand, base, period = cand_s, base_s, 1.0
    else:
        period = float(cand["_period"] or 1.0)
    detail = scoring.score(cand, base, weights, period)

    if args.json:
        print(json.dumps({"candidate": cand, "baseline": base, **detail}, indent=2))
        return 0

    print(_c("1", f"Eq. 3 score: {detail['score']:+.4f}") + "   (lower is better; "
          f"baseline {base['_run']} scores 0.0000)")
    print(f"  {'metric':<10} {'baseline':>12} {'candidate':>12} {'normalised':>12} "
          f"{'weighted':>10}")
    for key, label in (("wns", "WNS (ns)"), ("tns", "TNS (ns)"),
                       ("area", "area (um2)")):
        bkey = {"wns": "wns_ns", "tns": "tns_ns", "area": "area_um2"}[key]
        print(f"  {label:<10} {fmt(base.get(bkey)):>12} {fmt(cand.get(bkey)):>12} "
              f"{detail['normalized'][key]:>+12.4f} {detail['terms'][key]:>+10.4f}")
    print(f"  {'penalty':<10} {'':>12} {'':>12} {'':>12} "
          f"{detail['terms']['penalty']:>+10.4f}"
          + ("   (area grew past the 10% threshold)"
             if detail["terms"]["penalty"] else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="astra", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-q", "--quiet", action="store_true", help="don't echo tool output")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_quiet(sp: argparse.ArgumentParser) -> None:
        # SUPPRESS so that omitting it here does not overwrite the global -q;
        # this lets `astra -q run x` and `astra run x -q` both work.
        sp.add_argument("-q", "--quiet", action="store_true",
                        default=argparse.SUPPRESS, help="don't echo tool output")

    def flow_opts(sp: argparse.ArgumentParser) -> None:
        add_quiet(sp)
        sp.add_argument("design")
        sp.add_argument("--pdk", help="override the design's PDK")
        sp.add_argument("--period", type=float, help="clock period in ns")
        sp.add_argument("--rtl", nargs="+", help="use these Verilog files instead")
        sp.add_argument("--tag", help="suffix for the run directory name")
        sp.add_argument("--no-flatten", action="store_true")
        sp.add_argument("--npaths", type=int, default=20, help="worst paths to report")
        sp.add_argument("--detailed-route", action="store_true", help="full routing (slow)")
        sp.add_argument("--no-repair", action="store_true", help="skip repair_timing in PnR")

    sp = sub.add_parser("run", help="synthesis + timing report")
    flow_opts(sp)
    sp.add_argument("--pnr", action="store_true", help="also place & route")
    sp.set_defaults(func=cmd_run, stage="sta")

    sp = sub.add_parser("syn", help="synthesis only")
    flow_opts(sp)
    sp.set_defaults(func=cmd_run, stage="syn", pnr=False)

    sp = sub.add_parser("sta", help="timing report for an existing run")
    add_quiet(sp)
    sp.add_argument("design")
    sp.add_argument("--run", default="latest")
    sp.add_argument("--npaths", type=int, default=20)
    sp.set_defaults(func=cmd_sta)

    sp = sub.add_parser("pnr", help="place & route for an existing run")
    add_quiet(sp)
    sp.add_argument("design")
    sp.add_argument("--run", default="latest")
    sp.add_argument("--npaths", type=int, default=20)
    sp.add_argument("--detailed-route", action="store_true")
    sp.add_argument("--no-repair", action="store_true")
    sp.set_defaults(func=cmd_pnr)

    sp = sub.add_parser("list", help="designs and their runs")
    sp.add_argument("--limit", type=int, default=5)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("report", help="show a run's results")
    sp.add_argument("design")
    sp.add_argument("--run", default="latest")
    sp.add_argument("--timing", action="store_true", help="print the timing report instead")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("doctor", help="check tools, PDKs and designs")
    add_quiet(sp)
    sp.set_defaults(func=cmd_doctor)

    # --- Dr. RTL loop ------------------------------------------------------
    sp = sub.add_parser("localise", aliases=["localize"],
                        help="map the critical path back onto RTL lines")
    sp.add_argument("design")
    sp.add_argument("--run", default="latest")
    sp.add_argument("--top-k", type=int, default=3)
    sp.add_argument("--stage", choices=("sta", "pnr"), default="sta",
                    help="which timing report to read")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_localise)

    sp = sub.add_parser("score", help="Eq. 3 score of a run against a baseline")
    sp.add_argument("design")
    sp.add_argument("--run", default="latest", help="the candidate run")
    sp.add_argument("--baseline", required=True, help="the run PPA^norm is relative to")
    sp.add_argument("--alpha", type=float, help="WNS weight (default 0.5)")
    sp.add_argument("--beta", type=float, help="TNS weight (default 0.35)")
    sp.add_argument("--gamma", type=float, help="area weight (default 0.15)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_score)

    sp = sub.add_parser("sec", help="sequential equivalence check (see: astra sec -h)")
    sp.add_argument("rest", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_sec)

    sp = sub.add_parser("skills", help="inspect the skill library")
    sp.add_argument("rest", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_skills)

    sp = sub.add_parser("opt", help="run the closed optimisation loop "
                                    "(see: astra opt -h)")
    sp.add_argument("rest", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_opt)

    # --- path-portfolio addon ----------------------------------------------
    sp = sub.add_parser("paths", help="distinct critical-path targets "
                                      "worth an agent call")
    sp.add_argument("design")
    sp.add_argument("--run", default="latest")
    sp.add_argument("-k", "--top-k", type=int, default=3,
                    help="maximum targets to select (a ceiling, not a quota)")
    sp.add_argument("--stage", choices=("sta", "pnr"), default="sta")
    sp.add_argument("--mmr-lambda", type=float, default=0.5,
                    help="diversity weight in the selection")
    sp.add_argument("--cluster-at", type=float, default=0.65,
                    help="similarity at which two paths are one bottleneck")
    sp.add_argument("--delay-sigma", type=float, default=0.05,
                    help="delay uncertainty as a fraction of the clock period; "
                         "0 gives the deterministic answer")
    sp.add_argument("--cone-rho", type=float, default=0.7,
                    help="share of that uncertainty common to a whole cone")
    sp.add_argument("--no-skills", action="store_true",
                    help="do not consult the skill library for tractability")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_paths)

    sp = sub.add_parser("scan", help="structural smells in the RTL, "
                                     "before any tool runs")
    sp.add_argument("design")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("skilldoc", help="build the RTL timing-optimisation "
                                         "skill document")
    sp.add_argument("rest", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_skilldoc)

    sp = sub.add_parser("portfolio", help="path-portfolio loop: k scoped "
                                          "specialists, then a merge "
                                          "(see: astra portfolio -h)")
    sp.add_argument("rest", nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_portfolio)
    return p


def main() -> int:
    args = build_parser().parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
