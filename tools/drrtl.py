#!/usr/bin/env python3
"""Dr. RTL orchestrator: closed-loop, tool-grounded RTL timing optimisation.

Implements the framework of Fang et al., "Dr. RTL: Autonomous Agentic RTL
Optimization through Tool-Grounded Self-Improvement" (HKUST, 2026) on the
open-source toolchain in this repo -- Yosys for synthesis, OpenSTA for timing,
and Yosys/eqy for sequential equivalence checking.

    Eq. 2   D_t -> {D_t^(i)}_{i=1..N} -> D_{t+1}

Four roles, kept strictly separated the way the paper specifies:

  Timing Analysis Agent   localises critical paths back to RTL and names root
                          causes. Proposes no fixes.       (Sec. 4.1.2)
  RTL Optimization Agent  writes N functionally-equivalent candidates in
                          parallel, guided by the skill library. (Sec. 4.1.3)
  Evaluation Agent        runs synthesis, STA and SEC. Performs no reasoning
                          and reads no reports, so no optimisation decision
                          can rest on a hallucination.     (Sec. 4.1.4)
  Skill Learning Agent    compares the group by relative advantage and
                          distils reusable pattern-strategy pairs. (Sec. 4.2)

The orchestrator owns the loop, the shared JSON state, and the hierarchical
trajectory log that skill learning reads back.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import astra          # noqa: E402  flow driver: synthesis + STA
import clocks         # noqa: E402
import llm            # noqa: E402
import protect        # noqa: E402
import rtl_map        # noqa: E402
import score as scoring  # noqa: E402
import sec as sec_mod    # noqa: E402
import skills as skills_mod  # noqa: E402

ROOT = Path(os.environ.get("ASTRA_ROOT", Path(__file__).resolve().parent.parent))
DESIGNS = ROOT / "designs"
RUNS = ROOT / "runs"


def info(msg: str) -> None:
    print(f"\033[36m[drrtl]\033[0m {msg}" if sys.stdout.isatty()
          else f"[drrtl] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[drrtl] WARNING: {msg}", flush=True)


def die(msg: str) -> None:
    print(f"[drrtl] ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    return f"{v:.4f}" if isinstance(v, float) else str(v)


# ===========================================================================
# response parsing
# ===========================================================================

_FENCE = re.compile(r"```([a-zA-Z_+-]*)\n(.*?)```", re.DOTALL)
_FILE_MARK = re.compile(r"^\s*//\s*FILE:\s*(\S+)\s*$", re.MULTILINE)


def extract_json(text: str) -> dict[str, Any] | None:
    """First JSON object in the reply, whether fenced or bare."""
    for lang, body in _FENCE.findall(text or ""):
        if lang.lower() in ("json", ""):
            try:
                obj = json.loads(body.strip())
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    # Unfenced: scan for a balanced object starting at the first '{'.
    start = (text or "").find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


def extract_verilog(text: str, expected: list[str]) -> dict[str, str]:
    """Verilog file contents from a reply, keyed by file name.

    Two accepted shapes, because insisting on one is a reliable way to throw
    away otherwise-good candidates:
      * a ``// FILE: name.v`` first line inside each fenced block
      * a single fenced block, when the design has exactly one RTL file
    """
    blocks = [(lang, body) for lang, body in _FENCE.findall(text or "")
              if lang.lower() in ("verilog", "systemverilog", "sv", "v", "")]
    out: dict[str, str] = {}
    unnamed: list[str] = []

    for _, body in blocks:
        m = _FILE_MARK.search(body)
        if m:
            name = Path(m.group(1)).name
            out[name] = _FILE_MARK.sub("", body, count=1).strip() + "\n"
        elif "module" in body:
            unnamed.append(body.strip() + "\n")

    if not out and len(unnamed) == 1 and len(expected) == 1:
        out[expected[0]] = unnamed[0]
    elif not out and unnamed and expected:
        # Match unnamed blocks to expected files by the module name they declare.
        for body in unnamed:
            mod = re.search(r"^\s*module\s+([A-Za-z_]\w*)", body, re.MULTILINE)
            if not mod:
                continue
            hit = next((e for e in expected if Path(e).stem == mod.group(1)), None)
            out[hit or (expected[0] if len(expected) == 1 else mod.group(1) + ".v")] = body
    return out


# ===========================================================================
# Evaluation Agent  (Sec. 4.1.4)
# ===========================================================================


class EvaluationAgent:
    """Runs the toolchain. Interprets nothing.

    The paper is explicit that this separation is what keeps the loop honest:
    "all optimization decisions are based solely on verified EDA feedback,
    avoiding hallucinations". So this class returns numbers and a SEC verdict,
    and never a sentence of analysis.
    """

    def __init__(self, cfg: dict[str, Any], period: float, npaths: int,
                 sec_depth: int, sec_engine: str, sec_timeout: int,
                 no_flatten: bool = False, quiet: bool = True,
                 sec_regcorr_timeout: int = sec_mod.REGCORR_TIMEOUT,
                 sec_fallback_timeout: int = sec_mod.FALLBACK_TIMEOUT) -> None:
        self.cfg = cfg
        self.period = period
        self.clocks = astra.design_clocks(cfg, period)
        self.args = SimpleNamespace(quiet=quiet, no_flatten=no_flatten,
                                    npaths=npaths)
        self.sec_depth = sec_depth
        self.sec_engine = sec_engine
        self.sec_timeout = sec_timeout
        self.sec_regcorr_timeout = sec_regcorr_timeout
        self.sec_fallback_timeout = sec_fallback_timeout

    # -- synthesis + STA ----------------------------------------------------

    def evaluate(self, rtl_files: list[Path], outdir: Path,
                 label: str) -> dict[str, Any]:
        """Synthesise and time one design. Returns metrics, never raises.

        astra's stage helpers call ``die()`` on a tool failure, which is right
        for a one-shot CLI run and wrong here: one candidate that fails to
        synthesise must not take the iteration down with it. SystemExit is
        caught and turned into a recorded failure.
        """
        outdir.mkdir(parents=True, exist_ok=True)
        idir = outdir / "00_inputs"
        idir.mkdir(parents=True, exist_ok=True)

        copied: list[str] = []
        for f in rtl_files:
            dst = idir / Path(f).name
            if Path(f).resolve() != dst.resolve():
                shutil.copy2(f, dst)
            copied.append(str(dst))

        sdc_src = (self.cfg["_dir"] / self.cfg["sdc"]).resolve()
        text = clocks.substitute(sdc_src.read_text(), self.clocks)
        sdc = idir / sdc_src.name
        sdc.write_text(text)

        astra.write_metrics(outdir, {
            "design": self.cfg["_name"], "run_id": outdir.name,
            "label": label, "pdk": self.cfg["pdk"], "top": self.cfg["top"],
            **self.clocks.to_metrics(),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "inputs": {"rtl": copied, "sdc": str(sdc)},
        })

        t0 = time.time()
        try:
            astra.do_syn(self.cfg, outdir, self.args, self.period)
        except SystemExit:
            return self._failed(outdir, "synth_failed",
                                "yosys could not synthesise the candidate",
                                time.time() - t0)
        except Exception as e:                      # noqa: BLE001
            return self._failed(outdir, "synth_failed", str(e), time.time() - t0)

        try:
            astra.do_sta(self.cfg, outdir, self.args)
        except SystemExit:
            return self._failed(outdir, "sta_failed", "OpenSTA failed",
                                time.time() - t0)
        except Exception as e:                      # noqa: BLE001
            return self._failed(outdir, "sta_failed", str(e), time.time() - t0)

        m = astra.read_metrics(outdir)
        syn, sta = m.get("synth", {}), m.get("sta", {})
        return {
            "status": "ok",
            "wns_ns": sta.get("wns_ns"),
            "tns_ns": sta.get("tns_ns"),
            "violating_endpoints": sta.get("violating_endpoints"),
            "hold_wns_ns": sta.get("hold_wns_ns"),
            "clock_groups": sta.get("clock_groups"),
            "area_um2": syn.get("area_um2"),
            "cells": syn.get("cells"),
            "runtime_s": round(time.time() - t0, 2),
            "run_dir": str(outdir),
            "timing_json": sta.get("timing_json"),
        }

    @staticmethod
    def _failed(outdir: Path, status: str, reason: str,
                elapsed: float) -> dict[str, Any]:
        return {"status": status, "reason": reason,
                "wns_ns": None, "tns_ns": None, "area_um2": None,
                "runtime_s": round(elapsed, 2), "run_dir": str(outdir)}

    # -- SEC ----------------------------------------------------------------

    def check_equivalence(self, gold: list[Path], gate: list[Path],
                          workdir: Path) -> dict[str, Any]:
        return sec_mod.check(self.cfg["top"], gold, gate, workdir,
                             depth=self.sec_depth, engine=self.sec_engine,
                             timeout=self.sec_timeout,
                             clock_set=self.clocks,
                             regcorr_timeout=self.sec_regcorr_timeout,
                             fallback_timeout=self.sec_fallback_timeout)


# ===========================================================================
# Timing Analysis Agent  (Sec. 4.1.2)
# ===========================================================================

TIMING_SYSTEM = """\
You are the Timing Analysis Agent in an RTL optimisation loop.

Your job is diagnosis, and only diagnosis. You localise post-synthesis timing
bottlenecks back to specific RTL constructs and name their root causes. You do
NOT propose fixes, rewrites, or code -- a separate agent does that, and it
works better when your output is uncontaminated by your guesses about the
remedy.

You are given a mechanical path-to-RTL mapping that was computed from Yosys
source attributes and the OpenSTA report. Treat it as evidence, not as a
conclusion: it tells you which RTL lines the path crosses, and you decide what
about those lines makes the path slow.

Reply with exactly one JSON object and nothing else:

{
  "bottlenecks": [
    {
      "rank": 1,
      "pattern": "<short reusable name for the STRUCTURAL pattern, not this
                   design's identifiers -- e.g. 'serial accumulate chain',
                   'wide comparison at end of datapath', 'high-fanout control
                   signal'>",
      "rtl_location": "<file:line or file:line-line>",
      "signals": ["<RTL signal names on the path>"],
      "root_cause": "<one or two sentences: the mechanism, in RTL terms>",
      "evidence": "<the numbers from the report that support this>",
      "share_of_path": "<rough fraction of the path delay you attribute here>"
    }
  ],
  "summary": "<two sentences on what the critical path physically is>"
}

Rank by how much delay you attribute to each, most first. Three bottlenecks at
most. Netlist instance names like _14637_ are Yosys-generated and carry no
meaning -- infer structure from the cell-type sequence and the RTL mapping."""


class TimingAnalysisAgent:
    def __init__(self, use_llm: bool, model: str, timeout: int,
                 top_k: int = 3) -> None:
        self.use_llm = use_llm
        self.model = model
        self.timeout = timeout
        self.top_k = top_k

    def analyse(self, run_dir: Path, design_dir: Path, period: Any,
                dry_run: bool = False) -> dict[str, Any]:
        """Mechanical localisation first, then optional LLM root-causing.

        The mechanical half always runs: it is what makes the agent's claims
        checkable, and it is the only part that still works with no model
        available.
        """
        structured = rtl_map.from_run(run_dir, design_dir, period, self.top_k)
        text = rtl_map.render(structured)

        out: dict[str, Any] = {
            "structured": structured,
            "text": text,
            "bottlenecks": self._heuristic_bottlenecks(structured),
            "source": "heuristic",
        }
        if not self.use_llm:
            return out

        prompt = self._prompt(structured, text, run_dir, period)
        if dry_run:
            out["prompt"] = prompt
            return out

        try:
            reply = llm.call(prompt, TIMING_SYSTEM, self.model, self.timeout)
        except llm.LLMError as e:
            warn(f"timing analysis agent failed ({e}); using heuristics only")
            return out

        parsed = extract_json(reply)
        if not parsed or not parsed.get("bottlenecks"):
            warn("timing analysis agent returned no usable JSON; "
                 "using heuristics only")
            out["raw_reply"] = reply
            return out

        out["bottlenecks"] = parsed["bottlenecks"][:3]
        out["summary"] = parsed.get("summary", "")
        out["source"] = "llm"
        out["raw_reply"] = reply
        return out

    @staticmethod
    def _heuristic_bottlenecks(structured: dict[str, Any]) -> list[dict[str, Any]]:
        """Fallback bottleneck list built purely from the parsed report.

        Same shape as the LLM's, so everything downstream -- skill matching,
        the optimiser prompt, trajectory logging -- is indifferent to which
        one produced it.
        """
        out: list[dict[str, Any]] = []
        for p in structured.get("paths", []):
            regions = p["mapping"]["regions"]
            hottest = max(regions, key=lambda r: r["delay_ns"], default=None)
            for rank, f in enumerate(p["diagnosis"]["findings"], start=1):
                if f["pattern"] == "slack gap":
                    continue
                out.append({
                    "rank": len(out) + 1,
                    "pattern": f["pattern"],
                    "rtl_location": (f"{hottest['file']}:{hottest['line']}"
                                     if hottest else ""),
                    "signals": (hottest or {}).get("signals", [])[:6],
                    "root_cause": f["evidence"],
                    "evidence": json.dumps(f["metric"]),
                    "share_of_path": "",
                })
            if len(out) >= 3:
                break
        return out[:3]

    def _prompt(self, structured: dict[str, Any], text: str,
                run_dir: Path, period: float) -> str:
        rtl = "\n\n".join(f"----- {p.name} -----\n{p.read_text()}"
                          for p in sorted((run_dir / "00_inputs").glob("*.v")))
        clock_context = clocks.render_context(
            period if hasattr(period, "is_multi") else None,
            period if isinstance(period, (int, float)) else None)
        return f"""# Timing analysis request

{clock_context}

## Mechanical path-to-RTL mapping (computed, not inferred)

{text}

## RTL

{rtl}
"""


# ===========================================================================
# RTL Optimization Agent  (Sec. 4.1.3)
# ===========================================================================

OPT_SYSTEM = """\
You are the RTL Optimization Agent in a closed-loop timing optimisation
system. You rewrite Verilog to recover setup slack.

Hard constraints, all of them checked by tools after you answer:

1. FUNCTIONAL EQUIVALENCE. A sequential equivalence check runs against the
   original design. Any change to observable behaviour is rejected outright,
   however much slack it recovered. Bit-exact outputs, cycle for cycle.
2. LATENCY AND INTERFACE ARE FIXED. Same module name, same port names, same
   port widths, same pipeline depth. You may redistribute or duplicate
   registers; you may not add or remove a pipeline stage.
   Keep existing register names; rename or add intermediate wires freely. The
   equivalence check pairs registers by name, so an edit that keeps them is
   proved in seconds, while one that moves or renames a register takes a far
   slower check that may not finish -- and an unfinished check promotes
   nothing.
3. SYNTHESISABLE VERILOG-2005 ONLY. No initial blocks, no delays, no
   testbench constructs, no SystemVerilog interfaces.
4. You must return the COMPLETE file, not a diff and not an excerpt.

What actually works, in this order: reduce the number of logic levels between
flops (balanced trees instead of serial chains); move late-arriving work off
the critical path (precompute in parallel with a long operation instead of
after it); shorten wide carry propagation; break high-fanout drivers by
replication; narrow comparisons to the bits that decide them.

What does not work and wastes an iteration: renaming signals, reordering
independent statements, adding `(* keep *)`-style attributes, restating the
same logic with different operators, or "optimisations" whose only effect is
to move the same gates around.

Reply with exactly two blocks and nothing else.

First, a JSON object describing what you did:

```json
{
  "pattern": "<the structural bottleneck you attacked, named generically so it
               transfers to other designs. AT MOST 10 WORDS -- a label, not a
               description, e.g. 'serial accumulate chain' or 'wide comparison
               at end of datapath'. The explanation goes in `rationale`.>",
  "strategy": "<the transformation principle, also generic and AT MOST 10
                WORDS, e.g. 'rebalance into an adder tree'>",
  "rationale": "<why this shortens the critical path -- the mechanism>",
  "target": "<the RTL signals or lines you changed>",
  "expected_slack_recovery_ns": <number>,
  "area_cost": "<none | small | moderate | large, and why>",
  "equivalence_argument": "<why the outputs are bit-identical, cycle for
                            cycle -- be specific about the reset and the
                            pipeline stages>"
}
```

Then the complete rewritten RTL. If the design has more than one file, emit
one fenced block per file you changed, each beginning with a
`// FILE: <name>.v` line:

```verilog
// FILE: <name>.v
module ...
endmodule
```
"""

# Each parallel candidate is given a different directive. The paper's
# exploration comes from "applying different transformations to the same
# bottleneck or by targeting different bottlenecks"; sampling one prompt N
# times gets neither reliably, so the split is made explicit.
_DIRECTIVES = [
    "Attack bottleneck #1 using the highest-confidence matching skill from the "
    "library above. If nothing in the library matches, use the most direct "
    "structural fix for it.",

    "Attack bottleneck #1, but with a DIFFERENT transformation from the one an "
    "obvious first attempt would pick. If the obvious fix is to restructure the "
    "arithmetic, try moving work off the path instead, or vice versa.",

    "Attack bottleneck #2 (or the next-ranked one if #2 is minor). Leave "
    "bottleneck #1 alone entirely, so that the group can tell the two fixes "
    "apart.",

    "Combine fixes for more than one bottleneck in a single rewrite. Higher "
    "risk of an area regression or a SEC failure -- take it anyway; the group "
    "comparison is what decides whether the risk paid off.",

    "Take the most aggressive restructuring you can still argue is equivalent. "
    "Prefer a large slack win with a moderate area cost over a safe, small one.",

    "Take the most conservative change that still recovers real slack, with as "
    "close to zero area cost as you can manage.",
]


class RtlOptimizationAgent:
    def __init__(self, model: str, timeout: int,
                 system: str | None = None,
                 directives: list[str] | None = None) -> None:
        self.model = model
        self.timeout = timeout
        # A subclass may swap the mandate without reimplementing the reply
        # handling, which is the fiddly part and the part worth sharing.
        self.system = system or OPT_SYSTEM
        self.directives = directives or _DIRECTIVES

    def propose(self, ctx: dict[str, Any], index: int,
                dry_run: bool = False) -> dict[str, Any]:
        prompt = self._prompt(ctx, index)
        cand: dict[str, Any] = {
            "id": f"cand{index}", "directive_index": index,
            "directive": self.directives[index % len(self.directives)]}
        if dry_run:
            cand["prompt"] = prompt
            return cand

        try:
            reply = llm.call(prompt, self.system, self.model, self.timeout)
        except llm.LLMError as e:
            cand["error"] = str(e)
            return cand

        cand["raw_reply"] = reply
        meta = extract_json(reply) or {}
        files = extract_verilog(reply, ctx["rtl_names"])
        if not files:
            cand["error"] = "no Verilog block in the reply"
            return cand

        cand.update({
            "pattern": meta.get("pattern") or "unlabelled transformation",
            "strategy": meta.get("strategy") or "unlabelled strategy",
            "rationale": meta.get("rationale", ""),
            "target": meta.get("target", ""),
            "expected_slack_recovery_ns": meta.get("expected_slack_recovery_ns"),
            "area_cost": meta.get("area_cost", ""),
            "equivalence_argument": meta.get("equivalence_argument", ""),
            "files": files,
        })
        return cand

    def _prompt(self, ctx: dict[str, Any], index: int) -> str:
        return f"""# Design: {ctx['design']}   (candidate {index + 1} of {ctx['n']})

{ctx['clock_context']}
current WNS   {fmt(ctx['metrics'].get('wns_ns'))} ns
current TNS   {fmt(ctx['metrics'].get('tns_ns'))} ns
current area  {fmt(ctx['metrics'].get('area_um2'))} um^2
iteration     {ctx['iteration']} of {ctx['max_iters']}

## Invariants this design declares (from config.json)

{ctx['invariants']}
## Off limits -- do not edit (checked mechanically, before synthesis)

{ctx['protected']}

## Timing analysis

{ctx['analysis_summary']}

### Bottlenecks, ranked

{ctx['bottleneck_text']}

### Mechanical path-to-RTL mapping

{ctx['mapping_text']}

## Skill library -- transformations learned from previous runs

{ctx['skills_text']}

## Already tried on this design in earlier iterations

{ctx['history_text']}

## Current RTL

{ctx['rtl_text']}

## Your directive for this candidate

{self.directives[index % len(self.directives)]}
"""


# ===========================================================================
# Skill Learning Agent  (Sec. 4.2)
# ===========================================================================

SKILL_SYSTEM = """\
You are the Skill Learning Agent. You read one iteration's group of parallel
RTL rewrites -- all generated from the same parent design under the same
timing feedback -- together with each one's measured PPA outcome, equivalence
result, and group-relative advantage A_i.

A_i is a z-score of the candidate's Eq. 3 score within the group. Lower score
is better, so a NEGATIVE A_i means the candidate beat its peers. That relative
signal is the point: absolute PPA numbers are noisy and design-specific, but
"this transformation beat its siblings under identical conditions" transfers.

Extract reusable pattern-strategy pairs. A pattern is a recurring STRUCTURAL
bottleneck (deep FSM decode, high-fanout control, wide comparison, mux-heavy
selection, serial accumulate chain). A strategy is the transformation
principle that addressed it (condition pre-computation, signal replication,
selective register insertion, tree rebalancing). Both must be phrased so they
apply to a design nobody has seen yet -- no identifiers from this design, and
each at most ten words. They are library keys: entries that say the same thing
in different words must collide so their statistics accumulate on one entry
instead of fragmenting across near-duplicates. Put the reasoning in
`rationale`, which has no length limit.

Also report what did NOT work. A strategy that broke equivalence, or that
consistently scored worse than its siblings, is worth recording precisely so
it is not retried.

Reply with exactly one JSON object and nothing else:

{
  "skills": [
    {
      "pattern": "<at most 10 words -- a reusable label, not a paragraph>",
      "strategy": "<at most 10 words>",
      "rationale": "<the mechanism, one or two sentences>",
      "example": "<a few lines of illustrative Verilog, generic>",
      "verdict": "effective" | "ineffective" | "breaks-equivalence",
      "evidence": "<which candidate, what A_i, what it did to WNS and area>"
    }
  ],
  "observations": "<what the group comparison showed that a single candidate
                    would not have>"
}

At most four skills. Only include one if the group's evidence actually
supports it -- a group where every candidate scored the same has taught you
nothing, and saying so is the correct answer."""


class SkillLearningAgent:
    def __init__(self, lib: skills_mod.SkillLibrary, use_llm: bool,
                 model: str, timeout: int) -> None:
        self.lib = lib
        self.use_llm = use_llm
        self.model = model
        self.timeout = timeout

    def learn(self, group: list[dict[str, Any]], design: str, iteration: int,
              dry_run: bool = False) -> dict[str, Any]:
        """Record the group, then optionally abstract across it.

        The mechanical record always happens: whatever each candidate declared
        it was doing, plus its measured advantage, is ground truth about what
        was tried and how it fared. The LLM pass is layered on top to
        generalise -- it never replaces the statistics.
        """
        recorded: list[str] = []
        for c in group:
            pattern = c.get("pattern")
            strategy = c.get("strategy")
            if not pattern or not strategy:
                continue
            entry = self.lib.record(
                pattern, strategy,
                sec_pass=scoring.sec_passed(c),
                conclusive=scoring.sec_decided(c),
                advantage=c.get("advantage"),
                design=design, iteration=iteration,
                rationale=c.get("rationale", ""),
                score_delta=c.get("score"),
            )
            if entry.get("id"):
                recorded.append(entry["id"])
            else:
                warn(f"{c['id']}: {entry.get('skipped')} "
                     f"({strategy[:60]!r}) -- not added to the library")

        out: dict[str, Any] = {"recorded": recorded, "abstracted": [],
                               "observations": ""}
        if not self.use_llm or len(group) < 2:
            self.lib.save()
            return out

        prompt = self._prompt(group, design, iteration)
        if dry_run:
            out["prompt"] = prompt
            return out

        try:
            reply = llm.call(prompt, SKILL_SYSTEM, self.model, self.timeout)
        except llm.LLMError as e:
            warn(f"skill learning agent failed ({e}); kept the mechanical record")
            self.lib.save()
            return out

        parsed = extract_json(reply) or {}
        # What the group actually established. An abstraction may only claim
        # a verdict the checks reached: measured on vending_machine, two
        # correct rewrites whose SEC *crashed* were summarised as
        # "breaks-equivalence" and recorded as refutations, which marked a
        # sound transformation "SEC pass 0/1" for every later design.
        refuted = any(scoring.sec_decided(c) and not scoring.sec_passed(c)
                      for c in group)
        proved = any(scoring.sec_passed(c) for c in group)
        for s in (parsed.get("skills") or [])[:4]:
            if not s.get("pattern") or not s.get("strategy"):
                continue
            verdict = (s.get("verdict") or "").lower()
            breaks = verdict == "breaks-equivalence"
            # The abstraction is credited with the group's own outcome for
            # that verdict, not invented statistics: "effective" earns one
            # SEC-passing observation at the group's best advantage,
            # "breaks-equivalence" earns a SEC failure -- and either counts as
            # undecided when no check in the group reached that verdict.
            best = min((c.get("advantage") for c in group
                        if c.get("advantage") is not None), default=None)
            entry = self.lib.record(
                s["pattern"], s["strategy"],
                sec_pass=not breaks,
                conclusive=refuted if breaks else proved,
                advantage=best if verdict == "effective" else None,
                design=design, iteration=iteration,
                rationale=s.get("rationale", ""),
                example=s.get("example", ""),
            )
            if entry.get("id"):
                out["abstracted"].append({"id": entry["id"], "verdict": verdict,
                                          "evidence": s.get("evidence", "")})
        out["observations"] = parsed.get("observations", "")
        out["raw_reply"] = reply
        self.lib.save()
        return out

    @staticmethod
    def _prompt(group: list[dict[str, Any]], design: str, iteration: int) -> str:
        stats = scoring.group_stats(group)
        rows = []
        for c in group:
            m = c.get("metrics") or {}
            sec = c.get("sec") or {}
            rows.append(f"""### {c['id']}   {c.get('pattern', '?')} -> {c.get('strategy', '?')}
  directive        {c.get('directive', '')}
  rationale        {c.get('rationale', '')}
  SEC              {'PASS' if scoring.sec_passed(c) else 'FAIL'} """
                        f"""({sec.get('engine', '?')}/{sec.get('method', '?')}: {sec.get('reason', '')})
  WNS              {fmt(m.get('wns_ns'))} ns
  TNS              {fmt(m.get('tns_ns'))} ns
  area             {fmt(m.get('area_um2'))} um^2
  Score (Eq. 3)    {fmt(c.get('score'))}
  A_i (Eq. 5)      {fmt(c.get('advantage'))}""")
        return f"""# Group comparison -- design {design}, iteration {iteration}

All candidates below were generated from the same parent RTL, under the same
timing feedback, and evaluated with the same tool settings.

group size {stats['n']}, mean score {fmt(stats['mean'])}, sigma {fmt(stats['std'])}

{chr(10).join(rows)}
"""


# ===========================================================================
# Orchestrator  (Sec. 4.1.1)
# ===========================================================================


class Orchestrator:
    # Subclasses running a different loop write to their own run prefix, so a
    # portfolio run and a base run never collide in runs/<design>/.
    RUN_PREFIX = "opt-"

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.cfg = astra.load_design(args.design)
        if args.pdk:
            self.cfg["pdk"] = args.pdk
        self.period = float(args.period if args.period is not None
                            else self.cfg["clock"]["period_ns"])
        self.clocks = astra.design_clocks(self.cfg, args.period)
        # Identifier globs the optimiser may not touch -- CDC and clock
        # generation, which the equivalence proof cuts and therefore cannot
        # check. See tools/protect.py.
        self.protected = list(self.cfg.get("protected") or [])
        # A bounded check is worth exactly what its depth can see, and the
        # depth a difference needs to reach an output is a property of the
        # DESIGN, not a global default -- on dual_clock a genuine bug is
        # invisible at depth 4 and refuted at 6. So a design may record the
        # depth its author actually verified, and --sec-depth overrides it.
        self.sec_depth = int(args.sec_depth if args.sec_depth is not None
                             else self.cfg.get("sec_depth", 20))
        self.design = args.design

        self.weights = dict(scoring.DEFAULT_WEIGHTS)
        for k in ("alpha", "beta", "gamma"):
            v = getattr(args, k, None)
            if v is not None:
                self.weights[k] = float(v)

        self.outdir = RUNS / self.design / (
            self.RUN_PREFIX + datetime.now().strftime("%Y%m%d-%H%M%S")
            + (f"-{args.tag}" if args.tag else ""))
        self.outdir.mkdir(parents=True, exist_ok=True)

        self.lib = skills_mod.SkillLibrary(args.skills)
        self.evaluator = EvaluationAgent(
            self.cfg, self.period, args.npaths, self.sec_depth,
            args.sec_engine, args.sec_timeout, args.no_flatten, quiet=True,
            sec_regcorr_timeout=args.sec_regcorr_timeout,
            sec_fallback_timeout=args.sec_fallback_timeout)
        self.analyst = TimingAnalysisAgent(
            not args.no_llm, args.model, args.timeout, args.top_k)
        self.optimizer = RtlOptimizationAgent(args.model, args.timeout)
        self.learner = SkillLearningAgent(
            self.lib, not args.no_llm and not args.no_skill_agent,
            args.model, args.timeout)

        # The shared JSON state of Sec. 4.1.1, and the three-layer trajectory
        # of Sec. 4.2 -- iteration / candidate / critical path.
        self.state: dict[str, Any] = {
            "design": self.design,
            "top": self.cfg["top"],
            "pdk": self.cfg["pdk"],
            **self.clocks.to_metrics(),
            "weights": self.weights,
            "config": {
                "candidates_per_iteration": args.n,
                "max_iterations": args.iters,
                "top_k_paths": args.top_k,
                "sec": {"engine": args.sec_engine, "depth": self.sec_depth},
                "model": args.model, "llm": not args.no_llm,
            },
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "baseline": None,
            "iterations": [],
            "best": None,
            "convergence_steps": None,
        }

    # -- helpers ------------------------------------------------------------

    def _save(self) -> None:
        (self.outdir / "trajectory.json").write_text(
            json.dumps(self.state, indent=2, default=str) + "\n")

    def _score_period(self, base_metrics: dict[str, Any] | None = None) -> float:
        """The period Eq. 3's normalisation is scaled by.

        One number is needed here because WNS and TNS are design-wide scalars.
        On a multi-clock design it is the period of the domain that owns the
        worst slack -- the domain the reported WNS came from.
        """
        groups = (base_metrics or {}).get("clock_groups")
        return scoring.critical_period(groups, self.clocks, self.period)

    def _score(self, group: list[dict[str, Any]],
               base: dict[str, Any]) -> dict[str, Any]:
        """Eq. 3 over a candidate group, in whichever unit is well defined.

        Cycle-normalised WNS/TNS where the per-group data exists, because the
        nanosecond scalars sum across domains of different worth; nanoseconds
        against the critical group's period otherwise. Both sides are converted
        together or neither is -- comparing a candidate in cycles against a
        baseline in nanoseconds would be worse than not converting at all.

        Returns the baseline actually used, since the caller needs the same one
        for any per-candidate scoring it does afterwards.
        """
        base_c = scoring.with_cycles(base, self.clocks)
        for c in group:
            if c.get("metrics"):
                c["metrics"] = scoring.with_cycles(c["metrics"], self.clocks)
        sides = [c["metrics"] for c in group if c.get("metrics")] + [base_c]
        period = 1.0 if scoring.cycles_available(*sides) \
            else self._score_period(base)
        scoring.score_group(group, base_c, self.weights, period)
        return base_c

    def _rtl_paths(self, d: Path) -> list[Path]:
        return sorted(p for p in d.glob("*.v")) + sorted(p for p in d.glob("*.sv"))

    # -- iteration 0: the baseline -----------------------------------------

    def baseline(self) -> dict[str, Any]:
        info("evaluating the baseline design D_0")
        rtl = [(self.cfg["_dir"] / r).resolve() for r in self.cfg["rtl"]]
        m = self.evaluator.evaluate(rtl, self.outdir / "baseline", "D_0")
        if m["status"] != "ok":
            die(f"the baseline design does not build: {m.get('reason')}")
        self.state["baseline"] = {
            "metrics": m,
            "rtl": [str(p) for p in rtl],
            "score": 0.0,   # Eq. 3 is measured relative to D_0, so D_0 scores 0
        }
        info(f"D_0: WNS {fmt(m['wns_ns'])} ns  TNS {fmt(m['tns_ns'])} ns  "
             f"area {fmt(m['area_um2'])} um^2")
        return m

    # -- one iteration ------------------------------------------------------

    def iterate(self, t: int, parent_dir: Path, parent_rtl: list[Path],
                parent_metrics: dict[str, Any],
                ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        idir = self.outdir / f"iter_{t:02d}"
        idir.mkdir(parents=True, exist_ok=True)
        info(f"--- iteration {t} " + "-" * 50)

        # 1. Timing Analysis Agent
        # The ClockSet, not the scalar period: rtl_map resolves each path's
        # own clock from it, and the prompt renders every domain.
        analysis = self.analyst.analyse(parent_dir, self.cfg["_dir"],
                                        self.clocks, self.args.dry_run)
        (idir / "analysis.txt").write_text(analysis["text"] + "\n")
        (idir / "analysis.json").write_text(
            json.dumps({k: v for k, v in analysis.items() if k != "prompt"},
                       indent=2, default=str) + "\n")
        patterns = [b.get("pattern", "") for b in analysis.get("bottlenecks", [])]
        info(f"bottlenecks ({analysis['source']}): "
             + ("; ".join(p for p in patterns if p) or "none identified"))

        # 2. RTL Optimization Agent -- N candidates in parallel
        matched = self.lib.match(patterns)
        ctx = self._context(t, parent_rtl, parent_metrics, analysis, matched)
        info(f"generating {self.args.n} candidates "
             f"({len(matched)} matching skill(s) in the library)")

        if self.args.dry_run:
            for i in range(self.args.n):
                cand = self.optimizer.propose(ctx, i, dry_run=True)
                (idir / f"prompt_cand{i}.txt").write_text(cand["prompt"])
            info(f"dry run: wrote {self.args.n} prompt(s) to {idir}")
            return {"iteration": t, "dry_run": True}, None

        with ThreadPoolExecutor(max_workers=min(self.args.n, 8)) as pool:
            group = list(pool.map(lambda i: self.optimizer.propose(ctx, i),
                                  range(self.args.n)))

        # 3. Evaluation Agent -- synthesis, STA and SEC for each candidate
        with ThreadPoolExecutor(max_workers=min(self.args.jobs, self.args.n)) as pool:
            list(pool.map(lambda c: self._evaluate_candidate(c, idir, parent_rtl),
                          group))

        # 4. Eq. 3 / Eq. 5 -- score the group and compute relative advantage
        baseline_metrics = self.state["baseline"]["metrics"]
        self._score(group, baseline_metrics)
        stats = scoring.group_stats(group)

        for c in group:
            m = c.get("metrics") or {}
            info(f"  {c['id']}: SEC {'pass' if scoring.sec_passed(c) else 'FAIL'}  "
                 f"WNS {fmt(m.get('wns_ns'))}  area {fmt(m.get('area_um2'))}  "
                 f"score {fmt(c.get('score'))}  A_i {fmt(c.get('advantage'))}"
                 + (f"  [{c['pattern']}]" if c.get("pattern") else "")
                 + (f"  ({c['error']})" if c.get("error") else ""))

        # 5. Eq. 4 -- promote the best SEC-passing candidate
        best = scoring.select_best(group)

        # 6. Skill Learning Agent
        learned = self.learner.learn(group, self.design, t)

        record = {
            "iteration": t,
            "parent": str(parent_dir),
            "analysis": {
                "source": analysis["source"],
                "summary": analysis.get("summary", ""),
                "bottlenecks": analysis.get("bottlenecks", []),
                "paths": [
                    {"rank": p["rank"], "slack_ns": p["slack_ns"],
                     "startpoint": p["startpoint"], "endpoint": p["endpoint"],
                     "diagnosis": p["diagnosis"],
                     "coverage": p["mapping"]["coverage"],
                     "regions": [{k: v for k, v in r.items() if k != "source"}
                                 for r in p["mapping"]["regions"]]}
                    for p in analysis["structured"].get("paths", [])
                ],
            },
            "skills_offered": [{"id": s["id"], "pattern": s["pattern"],
                                "strategy": s["strategy"],
                                "confidence": s.get("confidence"),
                                "status": s.get("status"),
                                "match_score": s.get("match_score")}
                               for s in matched],
            "group_stats": stats,
            "candidates": [self._trim(c) for c in group],
            "selected": best["id"] if best else None,
            "skill_learning": {k: v for k, v in learned.items() if k != "prompt"},
        }
        self.state["iterations"].append(record)
        self._save()

        if best:
            info(f"promoted {best['id']} (score {fmt(best['score'])}, "
                 f"A_i {fmt(best['advantage'])})")
        else:
            reasons = {c["id"]: (c.get("error")
                                 or (c.get("sec") or {}).get("reason")
                                 or (c.get("metrics") or {}).get("reason")
                                 or "no score")
                       for c in group}
            warn("no candidate passed SEC with a score; keeping the parent design")
            for cid, why in reasons.items():
                warn(f"  {cid}: {str(why)[:160]}")
        return record, best

    # -- per-candidate evaluation -------------------------------------------

    def _evaluate_candidate(self, cand: dict[str, Any], idir: Path,
                            parent_rtl: list[Path],
                            gold: list[Path] | None = None) -> None:
        cdir = idir / cand["id"]
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "proposal.json").write_text(json.dumps(
            {k: v for k, v in cand.items()
             if k not in ("raw_reply", "files", "prompt")}, indent=2) + "\n")
        if cand.get("raw_reply"):
            (cdir / "reply.md").write_text(cand["raw_reply"])

        if cand.get("error"):
            cand["metrics"] = {"status": "not_generated", "reason": cand["error"]}
            cand["sec"] = {"equivalent": False, "method": "skipped",
                           "reason": cand["error"], "engine": "none"}
            return

        # Write the candidate design: the files it rewrote, plus every file it
        # left alone, so the candidate is a complete design and not a patch.
        rtl_dir = cdir / "rtl"
        rtl_dir.mkdir(parents=True, exist_ok=True)
        files: list[Path] = []
        for src in parent_rtl:
            dst = rtl_dir / src.name
            body = cand["files"].get(src.name)
            dst.write_text(body if body is not None else src.read_text())
            files.append(dst)
        for name, body in cand["files"].items():
            if not (rtl_dir / name).exists():
                (rtl_dir / name).write_text(body)
                files.append(rtl_dir / name)
        cand["rtl"] = [str(p) for p in files]
        cand["changed_files"] = sorted(cand["files"].keys())

        # Protected regions before anything else. CDC and clock-generation
        # logic sits on the far side of the cut the equivalence proof makes,
        # so SEC cannot refute a change to it -- it would pass, and be wrong
        # silicon. This is the only gate that can catch it, so it runs before
        # the gate that cannot.
        guard = protect.check_files(
            {p.name: p.read_text() for p in parent_rtl},
            {p.name: p.read_text() for p in files},
            self.protected)
        cand["protected"] = guard
        if not guard["ok"]:
            (cdir / "protected.json").write_text(
                json.dumps(guard, indent=2) + "\n")
            reason = protect.render(guard)
            cand["metrics"] = {"status": "protected_edit", "reason": reason}
            cand["sec"] = {"equivalent": False, "method": "skipped",
                           "reason": reason, "engine": "none"}
            return

        # SEC first: a broken rewrite's timing numbers are not worth the
        # synthesis time, and reporting them at all invites reading them.
        # Gold is D_0, not the parent -- equivalence has to hold against the
        # original design, or it drifts one accepted rewrite at a time.
        # Gold defaults to D_0. A caller passes it explicitly when the working
        # parent is no longer the original design -- a pre-synthesis cleanup
        # replaces the parent, and equivalence must still hold against what the
        # user actually wrote, not against an already-rewritten intermediate.
        gold = [Path(p) for p in (gold or self.state["baseline"]["rtl"])]
        cand["sec"] = self.evaluator.check_equivalence(gold, files, cdir / "sec")

        if not cand["sec"]["equivalent"]:
            cand["metrics"] = {"status": "sec_failed",
                               "reason": cand["sec"].get("reason", "")}
            return

        cand["metrics"] = self.evaluator.evaluate(files, cdir / "eval", cand["id"])

    # -- prompt context -----------------------------------------------------

    def _context(self, t: int, rtl: list[Path], metrics: dict[str, Any],
                 analysis: dict[str, Any],
                 matched: list[dict[str, Any]]) -> dict[str, Any]:
        notes = self.cfg.get("notes") or {}
        bl = []
        for b in analysis.get("bottlenecks", []):
            bl.append(f"{b.get('rank', '?')}. [{b.get('pattern', '?')}] "
                      f"at {b.get('rtl_location', '?')}\n"
                      f"   signals: {', '.join(b.get('signals') or []) or '-'}\n"
                      f"   root cause: {b.get('root_cause', '')}\n"
                      f"   evidence: {b.get('evidence', '')}")

        history = []
        for it in self.state["iterations"]:
            for c in it["candidates"]:
                m = c.get("metrics") or {}
                history.append(
                    f"  iter {it['iteration']} {c['id']}: "
                    f"{c.get('pattern', '?')} -> {c.get('strategy', '?')}  "
                    f"[SEC {'pass' if (c.get('sec') or {}).get('equivalent') else 'FAIL'}, "
                    f"WNS {fmt(m.get('wns_ns'))}, area {fmt(m.get('area_um2'))}, "
                    f"score {fmt(c.get('score'))}]")

        return {
            "design": self.design,
            "n": self.args.n,
            "iteration": t,
            "max_iters": self.args.iters,
            "period": self.period,
            "clock_context": clocks.render_context(self.clocks),
            "metrics": metrics,
            "invariants": json.dumps(notes, indent=2) if notes else "<none declared>",
            "protected": protect.describe(self.protected),
            "analysis_summary": analysis.get("summary")
                                or "(no narrative summary; see the mapping below)",
            "bottleneck_text": "\n".join(bl) or "(none identified)",
            "mapping_text": analysis["text"],
            "skills_text": skills_mod.render(matched),
            "history_text": "\n".join(history) or "  (nothing yet)",
            "rtl_text": "\n\n".join(f"----- {p.name} -----\n{p.read_text()}"
                                    for p in rtl),
            "rtl_names": [p.name for p in rtl],
        }

    @staticmethod
    def _trim(c: dict[str, Any]) -> dict[str, Any]:
        """Candidate record for the trajectory: everything except the bulk."""
        return {k: v for k, v in c.items()
                if k not in ("raw_reply", "files", "prompt")}

    # -- the loop -----------------------------------------------------------

    def run(self) -> int:
        if self.args.no_llm and not self.args.dry_run:
            die("the optimisation loop needs a model to write the candidates.\n"
                "        Use --dry-run to inspect the prompts, or drop --no-llm.")
        if not self.args.dry_run and not llm.available():
            die("`claude` not found on PATH -- install Claude Code, "
                "or use --dry-run")

        info(f"run: {self.outdir}")
        info(f"skill library: {self.lib.path} ({len(self.lib)} entries)")
        self.baseline()
        self._save()

        parent_dir = self.outdir / "baseline"
        parent_rtl = self._rtl_paths(parent_dir / "00_inputs")
        parent_metrics = self.state["baseline"]["metrics"]

        best_overall = {"metrics": parent_metrics, "score": 0.0,
                        "id": "D_0", "iteration": 0,
                        "rtl": self.state["baseline"]["rtl"]}
        stalled = 0

        for t in range(1, self.args.iters + 1):
            rec, best = self.iterate(t, parent_dir, parent_rtl, parent_metrics)
            if rec.get("dry_run"):
                return 0

            if best is None:
                stalled += 1
                if stalled >= self.args.patience:
                    info(f"no promotable candidate for {stalled} iteration(s); "
                         f"stopping")
                    break
                continue

            # Eq. 2: the promoted candidate becomes the next parent.
            parent_rtl = [Path(p) for p in best["rtl"]]
            parent_dir = Path(best["metrics"]["run_dir"])
            parent_metrics = best["metrics"]

            if best["score"] < best_overall["score"] - 1e-9:
                best_overall = {**best, "iteration": t}
                stalled = 0
                self.state["convergence_steps"] = t
            else:
                stalled += 1
                info(f"iteration {t} did not beat the best score so far "
                     f"({fmt(best['score'])} vs {fmt(best_overall['score'])})")
                if stalled >= self.args.patience:
                    info(f"no improvement for {stalled} iteration(s); stopping")
                    break

        self.state["best"] = {
            "id": best_overall.get("id"),
            "iteration": best_overall.get("iteration"),
            "score": best_overall.get("score"),
            "metrics": best_overall.get("metrics"),
            "pattern": best_overall.get("pattern"),
            "strategy": best_overall.get("strategy"),
            "rtl": best_overall.get("rtl"),
            "sec": best_overall.get("sec"),
        }
        self.state["usage"] = llm.usage()
        self._save()
        self._publish_best(best_overall)
        self._summarise()
        return 0

    def _publish_best(self, best: dict[str, Any]) -> None:
        bdir = self.outdir / "best"
        bdir.mkdir(parents=True, exist_ok=True)
        for p in best.get("rtl") or []:
            src = Path(p)
            if src.is_file():
                shutil.copy2(src, bdir / src.name)
        (bdir / "README.md").write_text(
            f"# Best design from {self.outdir.name}\n\n"
            f"candidate  {best.get('id')}\n"
            f"iteration  {best.get('iteration')}\n"
            f"Eq.3 score {fmt(best.get('score'))} (D_0 = 0.0)\n"
            f"pattern    {best.get('pattern', '-')}\n"
            f"strategy   {best.get('strategy', '-')}\n\n"
            "Verified functionally equivalent to the original RTL by the "
            "SEC step recorded in trajectory.json.\n")

    def _summarise(self) -> None:
        base = self.state["baseline"]["metrics"]
        best = self.state["best"] or {}
        bm = best.get("metrics") or {}

        def delta(key: str) -> str:
            a, c = base.get(key), bm.get(key)
            if a is None or c is None or abs(a) < 1e-12:
                return "n/a"
            return f"{(c - a) / abs(a) * 100:+.1f}%"

        lines = [
            f"# Dr. RTL optimisation -- {self.design}",
            "",
            f"run           {self.outdir.name}",
            f"{clocks.render_context(self.clocks)}",
            f"iterations    {len(self.state['iterations'])} "
            f"(converged at {self.state.get('convergence_steps') or 'n/a'})",
            f"candidates    {self.args.n} per iteration",
            f"weights       alpha={self.weights['alpha']} beta={self.weights['beta']} "
            f"gamma={self.weights['gamma']}",
            "",
            "| metric | D_0 | best | change |",
            "|---|---|---|---|",
            f"| WNS (ns) | {fmt(base.get('wns_ns'))} | {fmt(bm.get('wns_ns'))} | {delta('wns_ns')} |",
            f"| TNS (ns) | {fmt(base.get('tns_ns'))} | {fmt(bm.get('tns_ns'))} | {delta('tns_ns')} |",
            f"| area (um^2) | {fmt(base.get('area_um2'))} | {fmt(bm.get('area_um2'))} | {delta('area_um2')} |",
            f"| cells | {fmt(base.get('cells'))} | {fmt(bm.get('cells'))} | {delta('cells')} |",
            f"| Eq. 3 score | 0.0000 | {fmt(best.get('score'))} | |",
            "",
        ]

        # Counted over candidates that actually reached a verdict. A reply the
        # model never produced says nothing about whether the transformation
        # was sound, and folding it in reports a flaky harness as a bad method.
        cands = [c for it in self.state["iterations"]
                 for c in it.get("candidates", [])]
        lines += [scoring.render_sec_tally(scoring.sec_tally(cands)), ""]

        lines.append("## Iterations")
        lines.append("")
        for it in self.state["iterations"]:
            lines.append(f"### Iteration {it['iteration']}")
            pats = "; ".join(x.get("pattern", "")
                             for x in it["analysis"]["bottlenecks"])
            lines.append(f"bottlenecks: {pats or '-'}")
            lines.append("")
            lines.append("| candidate | pattern -> strategy | SEC | WNS | area | score | A_i |")
            lines.append("|---|---|---|---|---|---|---|")
            for c in it["candidates"]:
                m = c.get("metrics") or {}
                sel = " **<-**" if c["id"] == it.get("selected") else ""
                lines.append(
                    f"| {c['id']}{sel} | {c.get('pattern', '-')} -> "
                    f"{c.get('strategy', '-')} | "
                    f"{'pass' if (c.get('sec') or {}).get('equivalent') else 'FAIL'} | "
                    f"{fmt(m.get('wns_ns'))} | {fmt(m.get('area_um2'))} | "
                    f"{fmt(c.get('score'))} | {fmt(c.get('advantage'))} |")
            lines.append("")

        lines.append("## Skill library after this run")
        lines.append("")
        lines.append("```")
        lines.append(skills_mod.render(self.lib.all(True), with_examples=False))
        lines.append("```")

        u = self.state.get("usage") or {}
        if u.get("calls"):
            lines += ["", f"Model calls: {u['calls']}, "
                      f"in {u['input_tokens']} + cache {u['cache_read_input_tokens']}, "
                      f"out {u['output_tokens']}, notional ${u['cost_usd']:.4f}."]

        out = self.outdir / "summary.md"
        out.write_text("\n".join(lines) + "\n")
        info(f"summary: {out}")
        print()
        print("\n".join(lines[:22]))


# ===========================================================================
# cli
# ===========================================================================


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="astra-opt", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("design")
    p.add_argument("-n", type=int, default=4,
                   help="parallel candidates per iteration (paper's N, default 4)")
    p.add_argument("--iters", type=int, default=3,
                   help="maximum iterations (paper's K, default 3)")
    p.add_argument("--patience", type=int, default=2,
                   help="stop after this many iterations with no improvement")
    p.add_argument("--top-k", type=int, default=3,
                   help="critical paths the timing agent analyses")
    p.add_argument("--jobs", type=int, default=4,
                   help="concurrent candidate evaluations")

    p.add_argument("--period", type=float, help="clock period in ns")
    p.add_argument("--pdk", help="override the design's PDK")
    p.add_argument("--npaths", type=int, default=20, help="paths STA reports")
    p.add_argument("--no-flatten", action="store_true")
    p.add_argument("--tag", help="suffix for the run directory")

    p.add_argument("--alpha", type=float, help="Eq. 3 WNS weight (default 0.5)")
    p.add_argument("--beta", type=float, help="Eq. 3 TNS weight (default 0.35)")
    p.add_argument("--gamma", type=float, help="Eq. 3 area weight (default 0.15)")

    p.add_argument("--sec-engine", choices=("auto", "eqy", "yosys"), default="auto")
    # Default None, not 20, so a design's own verified sec_depth can be told
    # apart from the fallback -- see Orchestrator.__init__.
    p.add_argument("--sec-depth", type=int, default=None,
                   help="cycles for the bounded equivalence check")
    p.add_argument("--sec-timeout", type=int, default=1800,
                   help="single-clock equivalence check")
    # Multi-clock designs are asked two questions, cheap one first -- see
    # sec.check. The fallback is the whole-design bounded check; 0 skips it,
    # which leaves anything register correspondence cannot prove undecided.
    p.add_argument("--sec-regcorr-timeout", type=int, default=sec_mod.REGCORR_TIMEOUT,
                   help="multi-clock register-correspondence stage")
    p.add_argument("--sec-fallback-timeout", type=int, default=sec_mod.FALLBACK_TIMEOUT,
                   help="multi-clock bounded fallback; 0 skips it")

    p.add_argument("--model", default="opus")
    p.add_argument("--timeout", type=int, default=900, help="per model call")
    p.add_argument("--skills", type=Path, default=None,
                   help="skill library path (default: skills/library.json)")
    p.add_argument("--no-skill-agent", action="store_true",
                   help="record statistics but skip the LLM abstraction pass")
    p.add_argument("--no-llm", action="store_true",
                   help="heuristics only; valid with --dry-run")
    p.add_argument("--dry-run", action="store_true",
                   help="write the prompts and exit without calling a model")
    return p


def main() -> int:
    args = build_parser().parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    return Orchestrator(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
