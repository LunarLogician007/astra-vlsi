#!/usr/bin/env python3
"""Path-portfolio optimisation: an addon to the Dr. RTL loop.

The base loop in ``drrtl.py`` sends N candidates at one shared analysis and
takes the best. That works, and its ceiling is visible in its own artifacts:
every candidate attacks the same bottleneck, each rewrites the whole file to
fix one thing, and two good partial fixes in different candidates can never be
combined.

This loop changes three things and reuses everything else:

  1.  ``pathsel`` picks k *distinct* targets -- separate logic cones where a
      design has them, disjoint segments of one cone where it does not.
  2.  Each target gets its own specialist, scoped to that target and told to
      leave the others alone. Smaller edits are likelier to survive the
      equivalence check, and they are what makes step 3 possible at all.
  3.  Their fixes are recombined. Every subset that composes by text splicing
      is assembled and evaluated for free; a merge agent additionally
      reconciles the parts that genuinely overlap.

The winner is whichever of {specialists, mechanical unions, agent union,
parent} measures best under Eq. 3. A union does not win for being a union --
combined fixes can trip the area penalty, or expose a fourth path neither agent
saw, and only measurement catches that.

The mechanical unions are also the control on the merge agent. If the model's
union never beats the one ``difflib`` assembled for nothing, the merge call is
not paying for itself, and ``summary.md`` says so in the one number that
matters: score(agent union) - score(best mechanical union).

Roles stay separated exactly as the base loop keeps them: the agents that
decide never run a tool, and the evaluator that runs the tools never reads a
report.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import drrtl                 # noqa: E402
import llm                   # noqa: E402
import merge as merge_mod    # noqa: E402
import pathsel               # noqa: E402
import rtlscan               # noqa: E402
import score as scoring      # noqa: E402
import skillgen              # noqa: E402
import skills as skills_mod  # noqa: E402

info, warn, die, fmt = drrtl.info, drrtl.warn, drrtl.die, drrtl.fmt
RUNS = drrtl.RUNS


# ===========================================================================
# Stage 1 -- pre-synthesis cleanup
# ===========================================================================

CLEANUP_SYSTEM = """\
You are the Pre-Synthesis RTL Agent. You clean up Verilog *before* it has been
synthesised or timed.

You are working with the weakest evidence in this pipeline, and you should act
like it. There is no timing report yet -- only a mechanical scan of the source
that flags constructs known to synthesise into deep logic. A scan finding is a
hypothesis, not a measurement, and a rewrite you cannot justify structurally is
worse than no rewrite, because it spends an equivalence proof and a synthesis
run to learn nothing.

The same hard constraints apply as to any rewrite here, and all are checked by
tools after you answer:

1. FUNCTIONAL EQUIVALENCE against the original. Bit-exact, cycle for cycle.
2. LATENCY AND INTERFACE FIXED. Same module name, ports, widths, pipeline
   depth. You may redistribute registers; you may not add or remove a stage.
3. SYNTHESISABLE VERILOG-2005 ONLY.
4. Return the COMPLETE file.

What is worth doing at this stage, before any path is known:

- Rebalance a reduction written as a serial chain into a tree. This is
  structurally better regardless of which path turns out to be critical.
- Remove logic that provably cannot affect the output -- a comparison against a
  bound the operand cannot reach, a branch that cannot be taken.
- Compute a late-arriving condition from earlier values where it is
  algebraically the same.

What is not worth doing at this stage:

- Guessing which path will be critical. You do not know yet. A later stage
  will, and it will do a better job with that information than you can here.
- Reformatting, renaming, or reordering. Return the file with its existing
  layout intact apart from what you actually changed -- a reflowed file cannot
  be combined with anything.

If the scan shows nothing worth acting on, say so and return the file
unchanged. That is a correct answer and it costs the pipeline nothing.

Reply with exactly two blocks and nothing else: a JSON object, then the
complete file in a fenced verilog block.

```json
{
  "pattern": "<the structural issue you fixed, at most 10 words, generic>",
  "strategy": "<the transformation, at most 10 words>",
  "rationale": "<why this removes logic levels>",
  "target": "<the signals or lines you changed>",
  "changed": true,
  "expected_slack_recovery_ns": <number, or null if you cannot say>,
  "area_cost": "<none | small | moderate | large, and why>",
  "equivalence_argument": "<why the outputs are bit-identical, cycle for
                            cycle -- be specific about reset and about
                            intermediate widths>"
}
```
"""


class RtlCleanupAgent(drrtl.RtlOptimizationAgent):
    """Stage 1. Same reply contract as the optimiser, a different mandate."""

    def __init__(self, model: str, timeout: int, skill: str = "") -> None:
        super().__init__(model, timeout,
                         system=skillgen.inject(CLEANUP_SYSTEM, skill),
                         directives=[""])

    def _prompt(self, ctx: dict[str, Any], index: int) -> str:
        return f"""# Pre-synthesis cleanup -- {ctx['design']}

target clock {ctx['period']:g} ns  ({ctx['pdk']})

## Invariants this design declares (from config.json)

{ctx['invariants']}

## Mechanical scan of the source

{ctx['scan_text']}

## Current RTL

{ctx['rtl_text']}
"""


# ===========================================================================
# Stage 3 -- one specialist per target
# ===========================================================================

SPECIALIST_SYSTEM = """\
You are an RTL Optimization Agent in a closed-loop timing optimisation system.
You rewrite Verilog to recover setup slack.

You have been assigned ONE target out of several, and the scope rule below is
as binding as the equivalence rule.

Hard constraints, all checked by tools after you answer:

1. FUNCTIONAL EQUIVALENCE. A sequential equivalence check runs against the
   original design. Any change to observable behaviour is rejected outright,
   however much slack it recovered. Bit-exact outputs, cycle for cycle.
2. LATENCY AND INTERFACE ARE FIXED. Same module name, same port names, same
   port widths, same pipeline depth. You may redistribute or duplicate
   registers; you may not add or remove a pipeline stage.
3. SCOPE. Change only the logic your target names. Other agents are working on
   the other targets in parallel, right now, and their edits will be combined
   with yours by splicing text. An edit outside your target collides with a
   sibling's work and gets both dropped from the combination. Leave every other
   line byte-identical, including its indentation and its comments -- a
   reformatted file cannot be combined with anything.
4. SYNTHESISABLE VERILOG-2005 ONLY. No initial blocks, no delays, no testbench
   constructs, no SystemVerilog interfaces.
5. Return the COMPLETE file, not a diff and not an excerpt.

Your target is described by where its delay actually sits: a span of the
critical path, the cell types along it in order, the fanout peaks, and the RTL
the span resolves to. Read the cell walk -- it is the topology. A run of
AND/OR feeding a wide reduction is partial-product or comparison logic; a run
of XOR with AOI/OAI between is carry propagation; repeated MUX cells in series
are a select cascade.

If your target has no fix you can argue is equivalent, say so and return the
file unchanged. An honest no-op costs one synthesis run. A speculative rewrite
costs that plus an equivalence proof, and it pollutes the group comparison the
skill library learns from.

Reply with exactly two blocks and nothing else.

```json
{
  "pattern": "<the structural bottleneck you attacked, named generically so it
               transfers to other designs. AT MOST 10 WORDS -- a label, not a
               description. The explanation goes in `rationale`.>",
  "strategy": "<the transformation principle, also generic, AT MOST 10 WORDS>",
  "rationale": "<why this shortens the path -- the mechanism>",
  "target": "<the RTL signals or lines you changed>",
  "changed": true,
  "expected_slack_recovery_ns": <number>,
  "area_cost": "<none | small | moderate | large, and why>",
  "equivalence_argument": "<why the outputs are bit-identical, cycle for
                            cycle -- be specific about the reset and about
                            intermediate widths>"
}
```

Then the complete rewritten RTL. If the design has more than one file, emit one
fenced block per file you changed, each beginning with a `// FILE: <name>.v`
line:

```verilog
// FILE: <name>.v
module ...
endmodule
```
"""


class PathSpecialistAgent(drrtl.RtlOptimizationAgent):
    """One target, one candidate. Reuses the base agent's reply handling."""

    def __init__(self, model: str, timeout: int, skill: str = "") -> None:
        super().__init__(model, timeout,
                         system=skillgen.inject(SPECIALIST_SYSTEM, skill),
                         directives=[""])

    def propose_for(self, target: pathsel.PathTarget, ctx: dict[str, Any],
                    dry_run: bool = False) -> dict[str, Any]:
        prompt = self._prompt_for(target, ctx)
        cand: dict[str, Any] = {"id": target.id.lower(), "target_id": target.id,
                                "target_kind": target.kind,
                                "target_value": target.value}
        if dry_run:
            cand["prompt"] = prompt
            return cand
        try:
            reply = llm.call(prompt, self.system, self.model, self.timeout)
        except llm.LLMError as e:
            cand["error"] = str(e)
            return cand

        cand["raw_reply"] = reply
        meta = drrtl.extract_json(reply) or {}
        files = drrtl.extract_verilog(reply, ctx["rtl_names"])
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
            "declared_changed": meta.get("changed"),
            "files": files,
        })
        return cand

    def _prompt_for(self, target: pathsel.PathTarget, ctx: dict[str, Any]) -> str:
        others = "\n".join(
            f"  {o.id}  {o.kind}, {o.delay_share:.0%} of the path"
            + (f", stages {o.stage_span[0]}-{o.stage_span[1]}" if o.stage_span else "")
            + f"  [{', '.join(o.signals[:4]) or 'unresolved'}]"
            for o in ctx["targets"] if o.id != target.id)
        return f"""# Design: {ctx['design']}

target clock  {ctx['period']:g} ns   ({ctx['pdk']})
current WNS   {fmt(ctx['metrics'].get('wns_ns'))} ns
current TNS   {fmt(ctx['metrics'].get('tns_ns'))} ns
current area  {fmt(ctx['metrics'].get('area_um2'))} um^2
iteration     {ctx['iteration']} of {ctx['max_iters']}
stage         {ctx['stage_label']}

## Invariants this design declares (from config.json)

{ctx['invariants']}

## YOUR TARGET

{target.brief}

## The other targets in this portfolio -- do not touch their logic

{others or "  (none -- you are the only agent on this design this iteration)"}

## Already tried on this design in earlier iterations

{ctx['history_text']}

## Current RTL

{ctx['rtl_text']}
"""


# ===========================================================================
# Stage 4 -- the merge agent
# ===========================================================================

MERGE_SYSTEM = """\
You are the Merge Agent. Several RTL Optimization Agents each rewrote one part
of the same design, in parallel, and every one of their rewrites has now been
synthesised, timed and equivalence-checked. You produce the best single design
that combines what worked.

You are not being asked to accept all of them. Combining fixes is not additive:
two changes that each recovered slack on the same path may recover almost
nothing together, and each one costs area, which is scored. Your job is to
decide which combination is actually best and to write it.

What you are given, and what each part is for:

  * A unified diff per candidate against the parent. This is the change --
    read it rather than trying to reconstruct it from the full files.
  * A mechanical conflict report. Regions no two candidates touched can be
    combined by splicing text, and that has already been done for free. Regions
    two candidates both edited cannot, and those are the ones that need you.
  * Each candidate's measured WNS, TNS, area and Eq. 3 score, and the same for
    the mechanical union.

Where you add value, in order:

1. Reconciling genuinely overlapping edits, which no patch tool can do.
2. Dropping a fix that measured worse than the parent. A candidate that passed
   the equivalence check but regressed the score should not be carried along
   just because it is available.
3. Fusing two transformations into something neither candidate wrote, where
   they compose better than either alone.

If the mechanical union is already the best available design, say so and return
it unchanged. That is a real answer, and it is more useful than a rearrangement
that has to be re-measured to discover it changed nothing.

Same hard constraints as every other rewrite: bit-exact equivalence to the
ORIGINAL design, identical latency and interface, synthesisable Verilog-2005,
complete files.

Reply with exactly two blocks and nothing else.

```json
{
  "included": ["<candidate ids whose changes you kept>"],
  "excluded": ["<candidate ids you dropped, and this is expected>"],
  "reasoning": "<why this combination -- especially why you dropped what you
                 dropped, and how you resolved any conflict>",
  "conflicts_resolved": "<how you handled each contested region, or 'none'>",
  "expected_vs_mechanical": "<what this does that the mechanical union does
                              not, or 'nothing -- the mechanical union is
                              already best'>",
  "equivalence_argument": "<why the combined design is bit-identical to the
                            original, cycle for cycle>"
}
```

Then the complete merged RTL, one fenced block per file, each beginning with a
`// FILE: <name>.v` line.
"""


class MergeAgent:
    def __init__(self, model: str, timeout: int, skill: str = "") -> None:
        self.model = model
        self.timeout = timeout
        self.system = skillgen.inject(MERGE_SYSTEM, skill)

    def merge(self, ctx: dict[str, Any], group: list[dict[str, Any]],
              diffs: str, conflicts: str, mech: dict[str, Any] | None,
              dry_run: bool = False) -> dict[str, Any]:
        prompt = self._prompt(ctx, group, diffs, conflicts, mech)
        cand: dict[str, Any] = {"id": "umerge", "kind": "union",
                                "union_source": "agent"}
        if dry_run:
            cand["prompt"] = prompt
            return cand
        try:
            reply = llm.call(prompt, self.system, self.model, self.timeout)
        except llm.LLMError as e:
            cand["error"] = str(e)
            return cand

        cand["raw_reply"] = reply
        meta = drrtl.extract_json(reply) or {}
        files = drrtl.extract_verilog(reply, ctx["rtl_names"])
        if not files:
            cand["error"] = "no Verilog block in the reply"
            return cand
        cand.update({
            "pattern": "combined fixes from several targets",
            "strategy": "union of independently verified rewrites",
            "included": meta.get("included") or [],
            "excluded": meta.get("excluded") or [],
            "rationale": meta.get("reasoning", ""),
            "conflicts_resolved": meta.get("conflicts_resolved", ""),
            "expected_vs_mechanical": meta.get("expected_vs_mechanical", ""),
            "equivalence_argument": meta.get("equivalence_argument", ""),
            "files": files,
        })
        return cand

    @staticmethod
    def _row(c: dict[str, Any]) -> str:
        m = c.get("metrics") or {}
        return (f"  {c['id']:<8} {c.get('pattern', '?')[:38]:<40} "
                f"WNS {fmt(m.get('wns_ns')):>9}  TNS {fmt(m.get('tns_ns')):>9}  "
                f"area {fmt(m.get('area_um2')):>10}  "
                f"score {fmt(c.get('score')):>9}  "
                f"SEC {'pass' if scoring.sec_passed(c) else 'FAIL'}")

    def _prompt(self, ctx: dict[str, Any], group: list[dict[str, Any]],
                diffs: str, conflicts: str,
                mech: dict[str, Any] | None) -> str:
        rows = "\n".join(self._row(c) for c in group)
        mech_block = "  (no mechanical union was possible)"
        if mech:
            mech_block = self._row(mech) + (
                f"\n  built from: {', '.join(mech.get('union_members') or [])}")
        return f"""# Merge -- {ctx['design']}, iteration {ctx['iteration']}

target clock {ctx['period']:g} ns.  Eq. 3 score is relative to the ORIGINAL
design, which scores 0.0, and LOWER IS BETTER. The parent you are merging onto
scores {fmt(ctx.get('parent_score'))}.

## What each candidate measured

{rows}

## The mechanical union, assembled by splicing non-conflicting text

{mech_block}

## Conflict analysis

{conflicts}

## What each candidate changed

{diffs}

## The parent RTL you are merging onto

{ctx['rtl_text']}
"""


# ===========================================================================
# Orchestrator
# ===========================================================================


class PortfolioOrchestrator(drrtl.Orchestrator):
    RUN_PREFIX = "pf-"

    def __init__(self, args: argparse.Namespace) -> None:
        super().__init__(args)
        # Each agent gets only the sections it can act on. The merge agent in
        # particular reconciles diffs between rewrites that already exist, so
        # the transformation catalogue and the path-reading material are cost
        # with no possible use -- they push the diffs and the parent RTL
        # further down a prompt the model has to hold all of.
        self.skill = skillgen.load_doc()
        self.cleaner = RtlCleanupAgent(args.model, args.timeout,
                                       skillgen.load_doc(role="cleanup"))
        self.specialist = PathSpecialistAgent(args.model, args.timeout,
                                              skillgen.load_doc(role="specialist"))
        self.merger = MergeAgent(args.model, args.timeout,
                                 skillgen.load_doc(role="merge"))
        self.calls_planned = call_budget(args)

        self.state["mode"] = "portfolio"
        self.state["config"].update({
            "targets_per_iteration": args.top_k,
            "mmr_lambda": args.mmr_lambda,
            "cluster_at": args.cluster_at,
            "delay_sigma": args.delay_sigma,
            "cone_rho": args.cone_rho,
            "cleanup_candidates": args.clean,
            "merge_agent": not args.no_merge_agent,
            "stage": args.stage,
            "skill_document": str(skillgen.SKILL_PATH) if self.skill else None,
        })
        self.state["budget"] = {"planned_calls": self.calls_planned}
        self.state["cleanup"] = None

    # -- helpers ------------------------------------------------------------

    def _files_of(self, rtl: list[Path]) -> dict[str, str]:
        return {p.name: p.read_text() for p in rtl}

    def _rtl_text(self, rtl: list[Path]) -> str:
        return "\n\n".join(f"----- {p.name} -----\n{p.read_text()}" for p in rtl)

    def _history(self) -> str:
        out = []
        for it in self.state["iterations"]:
            for c in it.get("candidates", []):
                m = c.get("metrics") or {}
                out.append(
                    f"  iter {it['iteration']} {c['id']}: "
                    f"{c.get('pattern', '?')} -> {c.get('strategy', '?')}  "
                    f"[SEC {'pass' if (c.get('sec') or {}).get('equivalent') else 'FAIL'}, "
                    f"WNS {fmt(m.get('wns_ns'))}, score {fmt(c.get('score'))}]")
        return "\n".join(out) or "  (nothing yet)"

    def _ctx(self, t: int, rtl: list[Path], metrics: dict[str, Any],
             targets: list[pathsel.PathTarget]) -> dict[str, Any]:
        notes = self.cfg.get("notes") or {}
        return {
            "design": self.design,
            "pdk": self.cfg["pdk"],
            "period": self.period,
            "iteration": t,
            "max_iters": self.args.iters,
            "stage_label": ("post-synthesis" if self.args.stage == "sta"
                            else "post-place-and-route"),
            "metrics": metrics,
            "targets": targets,
            "invariants": json.dumps(notes, indent=2) if notes else "<none declared>",
            "history_text": self._history(),
            "rtl_text": self._rtl_text(rtl),
            "rtl_names": [p.name for p in rtl],
            "parent_score": metrics.get("_score"),
        }

    # -- stage 1 ------------------------------------------------------------

    def cleanup(self, rtl: list[Path]) -> tuple[list[Path], dict[str, Any]]:
        """Pre-synthesis pass. Adopted only if it measurably beats D_0.

        The blindest step in the pipeline -- there is no timing report yet --
        so the gate is not "did it pass the equivalence check" but "did it
        actually score better". The advisor in this repo has one recorded case
        of confidently attributing 0.18 ns to a construct worth 0.001 ns, and a
        pre-synthesis pass has strictly less to go on than the advisor did.
        """
        cdir = self.outdir / "cleanup"
        cdir.mkdir(parents=True, exist_ok=True)
        report = rtlscan.scan(rtl)
        (cdir / "findings.json").write_text(json.dumps(report, indent=2) + "\n")
        (cdir / "findings.txt").write_text(rtlscan.render(report) + "\n")
        info(f"pre-synthesis scan: {len(report['findings'])} finding(s)")

        if not report["findings"]:
            info("nothing to clean up before synthesis; keeping D_0")
            return rtl, {"adopted": False, "reason": "no scan findings"}

        ctx = {"design": self.design, "period": self.period,
               "pdk": self.cfg["pdk"],
               "invariants": json.dumps(self.cfg.get("notes") or {}, indent=2)
                            or "<none declared>",
               "scan_text": rtlscan.render(report),
               "rtl_text": self._rtl_text(rtl),
               "rtl_names": [p.name for p in rtl]}

        if self.args.dry_run:
            for i in range(self.args.clean):
                cand = self.cleaner.propose(ctx, i, dry_run=True)
                (cdir / f"prompt_clean{i}.txt").write_text(cand["prompt"])
            info(f"dry run: wrote {self.args.clean} cleanup prompt(s)")
            return rtl, {"adopted": False, "reason": "dry run"}

        with ThreadPoolExecutor(max_workers=min(self.args.clean, 4)) as pool:
            group = list(pool.map(lambda i: self.cleaner.propose(ctx, i),
                                  range(self.args.clean)))
        for i, c in enumerate(group):
            c["id"] = f"clean{i}"
        with ThreadPoolExecutor(max_workers=min(self.args.jobs, len(group))) as pool:
            list(pool.map(lambda c: self._evaluate_candidate(c, cdir, rtl, gold=rtl),
                          group))

        base = self.state["baseline"]["metrics"]
        scoring.score_group(group, base, self.weights, self._score_period(base))
        for c in group:
            m = c.get("metrics") or {}
            info(f"  {c['id']}: SEC {'pass' if scoring.sec_passed(c) else 'FAIL'}  "
                 f"WNS {fmt(m.get('wns_ns'))}  score {fmt(c.get('score'))}"
                 + (f"  ({c['error']})" if c.get("error") else ""))

        best = scoring.select_best(group)
        record = {"candidates": [self._trim(c) for c in group],
                  "adopted": False, "gate": self.args.clean_gate}
        if best is None or best["score"] > -abs(self.args.clean_gate):
            info("no cleanup candidate cleared the gate; keeping D_0 as the parent")
            record["reason"] = ("nothing beat D_0 by the required margin"
                                if best else "no candidate passed SEC")
            return rtl, record

        info(f"adopted {best['id']} as the pre-synthesis parent "
             f"(score {fmt(best['score'])})")
        record.update({"adopted": True, "id": best["id"],
                       "score": best["score"], "rtl": best["rtl"]})
        return [Path(p) for p in best["rtl"]], record

    # -- one iteration ------------------------------------------------------

    def iterate(self, t: int, parent_dir: Path, parent_rtl: list[Path],
                parent_metrics: dict[str, Any],
                ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        idir = self.outdir / f"iter_{t:02d}"
        idir.mkdir(parents=True, exist_ok=True)
        info(f"--- iteration {t} " + "-" * 50)

        # 1. Portfolio selection -- mechanical, no model call.
        try:
            sel = pathsel.from_run(parent_dir, self.cfg["_dir"], self.clocks,
                                   self.args.top_k, self.args.stage,
                                   self.args.mmr_lambda, self.args.cluster_at,
                                   self.lib, self.args.delay_sigma,
                                   self.args.cone_rho)
        except (FileNotFoundError, ValueError) as e:
            die(str(e))
        targets: list[pathsel.PathTarget] = sel["targets"]
        (idir / "pathsel.txt").write_text(pathsel.render(sel) + "\n")
        (idir / "pathsel.json").write_text(json.dumps(
            {**sel, "targets": [t_.to_dict() for t_ in targets]},
            indent=2, default=str) + "\n")
        bdir = idir / "briefs"
        bdir.mkdir(exist_ok=True)
        for tgt in targets:
            (bdir / f"{tgt.id}.md").write_text(tgt.brief + "\n")

        if not targets:
            warn("no target cleared the mass floor; nothing to optimise")
            return {"iteration": t, "targets": [], "candidates": []}, None

        info(f"portfolio: {sel['k_effective']}/{sel['k_requested']} target(s), "
             f"{sel['cluster_count']} cone(s)"
             + ("  [one cone, cut into segments]" if sel["collapsed"] else ""))
        for tgt in targets:
            info(f"  {tgt.id} [{tgt.kind}] value {tgt.value:.3f}  "
                 + (f"P(limits clock) {tgt.p_critical:.0%}  "
                    if tgt.p_critical is not None else "")
                 + f"{tgt.delay_share:.0%} of path delay  "
                 + f"{', '.join(p for p in list(tgt.cell_families)[:3])}")

        ctx = self._ctx(t, parent_rtl, {**parent_metrics,
                                        "_score": parent_metrics.get("_score", 0.0)},
                        targets)

        # 2. One specialist per target, in parallel.
        if self.args.dry_run:
            for tgt in targets:
                cand = self.specialist.propose_for(tgt, ctx, dry_run=True)
                (idir / f"prompt_{tgt.id}.txt").write_text(cand["prompt"])
            info(f"dry run: wrote {len(targets)} specialist prompt(s) to {idir}")
            # No merge prompt: it is built from the candidates' measured
            # diffs, and in a dry run there are none. Saying so beats
            # writing a prompt with placeholder content in it.
            info("dry run: no merge prompt -- it is assembled from measured "
                 "candidates, which a dry run does not produce")
            return {"iteration": t, "dry_run": True}, None

        with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as pool:
            group = list(pool.map(
                lambda tg: self.specialist.propose_for(tg, ctx), targets))

        # 3. Evaluate: SEC against D_0, then synthesis and STA.
        gold = [Path(p) for p in self.state["baseline"]["rtl"]]
        with ThreadPoolExecutor(max_workers=min(self.args.jobs, len(group))) as pool:
            list(pool.map(lambda c: self._evaluate_candidate(c, idir, parent_rtl,
                                                             gold=gold), group))

        base = self.state["baseline"]["metrics"]
        scoring.score_group(group, base, self.weights, self._score_period(base))
        for c, tgt in zip(group, targets):
            self._scope_check(c, tgt, parent_rtl)
            m = c.get("metrics") or {}
            info(f"  {c['id']}: SEC {'pass' if scoring.sec_passed(c) else 'FAIL'}  "
                 f"WNS {fmt(m.get('wns_ns'))}  area {fmt(m.get('area_um2'))}  "
                 f"score {fmt(c.get('score'))}"
                 + (f"  [{c['pattern']}]" if c.get("pattern") else "")
                 + (f"  ({c['error']})" if c.get("error") else ""))

        # 4. Recombination.
        unions, merge_rec = self.merge_stage(ctx, group, parent_rtl, idir, gold)

        # 5. Eq. 3 / Eq. 5 over the specialists; unions scored individually.
        pool_all = group + unions
        stats = scoring.group_stats(group)

        # 6. Eq. 4 over everything, including the option of keeping the parent.
        best = scoring.select_best(pool_all)
        learned = self.learner.learn(group, self.design, t)

        record = {
            "iteration": t,
            "parent": str(parent_dir),
            "stage": self.args.stage,
            "portfolio": {
                "k_requested": sel["k_requested"],
                "k_effective": sel["k_effective"],
                "collapsed": sel["collapsed"],
                "cluster_count": sel["cluster_count"],
                "pool": sel["pool"],
                "targets": [t_.to_dict() for t_ in targets],
            },
            "group_stats": stats,
            "candidates": [self._trim(c) for c in group],
            "unions": [self._trim(c) for c in unions],
            "merge": merge_rec,
            "selected": best["id"] if best else None,
            "skill_learning": {k: v for k, v in learned.items() if k != "prompt"},
        }
        self.state["iterations"].append(record)
        self._save()

        if best:
            info(f"promoted {best['id']} (score {fmt(best['score'])})")
        else:
            warn("no candidate passed SEC with a score; keeping the parent design")
            for c in pool_all:
                why = (c.get("error") or (c.get("sec") or {}).get("reason")
                       or (c.get("metrics") or {}).get("reason") or "no score")
                warn(f"  {c['id']}: {str(why)[:160]}")
        return record, best

    # -- scope ---------------------------------------------------------------

    def _scope_check(self, cand: dict[str, Any], target: pathsel.PathTarget,
                     parent_rtl: list[Path]) -> None:
        """Did the specialist stay inside its target? Recorded, not enforced.

        Rejecting a candidate for adding a `wire` outside its span would throw
        away good work over a declaration. And nothing downstream depends on
        this being right: the merge stage detects real collisions from the real
        diffs. This is here so a high violation rate is *visible*, because that
        would mean the scoping premise of the merge stage is unsound and should
        be reported rather than assumed.
        """
        if not cand.get("files"):
            return
        parent_files = self._files_of(parent_rtl)
        outside: list[str] = []
        changed_lines = 0
        for fname, body in cand["files"].items():
            if fname not in parent_files:
                continue
            p = merge_mod.normalise(parent_files[fname])
            c = merge_mod.normalise(body)
            spans = target.allowed_lines.get(fname) or []
            for h in merge_mod.hunks(p, c, cand["id"], fname):
                changed_lines += max(1, h.p1 - h.p0)
                lo, hi = h.p0 + 1, max(h.p1, h.p0 + 1)
                if not any(a <= hi and lo <= b for a, b in spans):
                    outside.append(f"{fname}:{lo}-{hi}")
        cand["scope"] = {
            "enforceable": target.scope_enforceable,
            "allowed": {f: [list(s) for s in sp]
                        for f, sp in target.allowed_lines.items()},
            "outside": outside[:12],
            "violation": bool(outside) and target.scope_enforceable,
            "changed_lines": changed_lines,
        }
        if cand["scope"]["violation"]:
            warn(f"  {cand['id']} edited outside its target: "
                 f"{', '.join(outside[:4])}")

    # -- stage 4 -------------------------------------------------------------

    def merge_stage(self, ctx: dict[str, Any], group: list[dict[str, Any]],
                    parent_rtl: list[Path], idir: Path, gold: list[Path],
                    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Mechanical subset unions first, then the merge agent."""
        mdir = idir / "merge"
        mdir.mkdir(parents=True, exist_ok=True)

        ok = [c for c in group
              if scoring.sec_passed(c) and c.get("files")
              and (c.get("metrics") or {}).get("status") == "ok"]
        parent_files = self._files_of(parent_rtl)
        rec: dict[str, Any] = {"eligible": [c["id"] for c in ok],
                               "mechanical": [], "agent": None}

        if len(ok) < 2:
            rec["reason"] = (f"only {len(ok)} candidate(s) survived evaluation; "
                             f"there is nothing to combine")
            (mdir / "conflicts.txt").write_text(rec["reason"] + "\n")
            info(f"merge: {rec['reason']}")
            return [], rec

        cand_files = {c["id"]: {**parent_files, **c["files"]} for c in ok}

        # Diffs and the conflict report, for the record and for the prompt.
        diff_dir = mdir / "diffs"
        diff_dir.mkdir(exist_ok=True)
        diff_blocks: list[str] = []
        for cid, files in cand_files.items():
            for fname, body in files.items():
                d = merge_mod.unified(parent_files[fname], body, fname, cid)
                if d.strip():
                    (diff_dir / f"{cid}.patch").write_text(d + "\n")
                    diff_blocks.append(f"### {cid} -- {fname}\n\n```diff\n{d}\n```")
        diffs = "\n\n".join(diff_blocks) or "(no candidate changed anything)"

        # Every subset that composes. Deduplicated by result, because a subset
        # whose extra member contributed nothing is the same design as the
        # smaller one and evaluating it twice buys nothing.
        seen: dict[str, str] = {}
        unions: list[dict[str, Any]] = []
        full_report: dict[str, Any] = {}
        for members in merge_mod.subsets([c["id"] for c in ok]):
            merged, report = merge_mod.mechanical_union(
                parent_files, {m: cand_files[m] for m in members})
            if len(members) == len(ok):
                full_report = report
            if not report["changed"] or not report["applied_candidates"]:
                continue
            key = json.dumps(merged, sort_keys=True)
            uid = merge_mod.union_id(members)
            if key in seen:
                rec["mechanical"].append({"id": uid, "members": list(members),
                                          "duplicate_of": seen[key]})
                continue
            seen[key] = uid
            unions.append({
                "id": uid, "kind": "union", "union_source": "mechanical",
                "union_members": list(members),
                "applied": report["applied_candidates"],
                "dropped_hunks": report["dropped_hunks"],
                "pattern": "combined fixes from several targets",
                "strategy": f"mechanical union of {', '.join(members)}",
                "rationale": ("assembled by splicing non-conflicting text; "
                              "no model was involved"),
                "files": merged,
            })

        conflicts = merge_mod.render_conflicts(
            full_report or {"conflicts": [], "clean": []},
            (full_report or {}).get("excluded") or {})
        (mdir / "conflicts.txt").write_text(conflicts + "\n")
        (mdir / "conflicts.json").write_text(
            json.dumps(full_report, indent=2, default=str) + "\n")
        info(f"merge: {len(unions)} mechanical union(s) from {len(ok)} candidate(s), "
             f"{len(full_report.get('conflicts') or [])} contested region(s)")

        if unions:
            with ThreadPoolExecutor(max_workers=min(self.args.jobs, len(unions))) as pool:
                list(pool.map(lambda c: self._evaluate_candidate(c, mdir, parent_rtl,
                                                                 gold=gold), unions))
            base = self.state["baseline"]["metrics"]
            for c in unions:
                self._score_one(c, base)
                m = c.get("metrics") or {}
                info(f"  {c['id']} ({'+'.join(c['union_members'])}): "
                     f"SEC {'pass' if scoring.sec_passed(c) else 'FAIL'}  "
                     f"WNS {fmt(m.get('wns_ns'))}  score {fmt(c.get('score'))}")
        rec["mechanical"] += [self._trim(c) for c in unions]

        if self.args.no_merge_agent:
            rec["agent"] = {"skipped": "--no-merge-agent"}
            return unions, rec

        best_mech = scoring.select_best(unions)
        agent = self.merger.merge(ctx, group, diffs, conflicts, best_mech,
                                  dry_run=self.args.dry_run)
        if self.args.dry_run:
            (mdir / "prompt.txt").write_text(agent["prompt"])
            return unions, rec

        if agent.get("error"):
            warn(f"merge agent failed ({agent['error']}); "
                 f"keeping the mechanical unions")
            rec["agent"] = {"error": agent["error"]}
            return unions, rec

        self._evaluate_candidate(agent, mdir, parent_rtl, gold=gold)
        self._score_one(agent, self.state["baseline"]["metrics"])
        m = agent.get("metrics") or {}
        info(f"  {agent['id']} (agent): "
             f"SEC {'pass' if scoring.sec_passed(agent) else 'FAIL'}  "
             f"WNS {fmt(m.get('wns_ns'))}  score {fmt(agent.get('score'))}")

        # The number the whole merge stage exists to produce: what the model's
        # union bought over the one that cost nothing. Negative is the model
        # winning, because lower scores are better.
        delta = None
        if agent.get("score") is not None and best_mech \
                and best_mech.get("score") is not None:
            delta = agent["score"] - best_mech["score"]
            info(f"  merge agent vs best mechanical union: {delta:+.4f} "
                 f"({'agent wins' if delta < 0 else 'mechanical wins'})")
        rec["agent"] = {**self._trim(agent),
                        "vs_mechanical": delta,
                        "best_mechanical": best_mech["id"] if best_mech else None}
        return unions + [agent], rec

    def _score_one(self, cand: dict[str, Any], base: dict[str, Any]) -> None:
        """Score a union on its own.

        Eq. 5 does not apply to a union: the advantage is a z-score within a
        group of siblings generated from one parent under identical feedback,
        and a union is not a sibling of the candidates it was assembled from.
        Giving it one would put a derived design into the statistics the skill
        library learns from, and credit a transformation twice.
        """
        m = cand.get("metrics") or {}
        if m.get("wns_ns") is None and m.get("area_um2") is None:
            cand["score"] = cand["score_detail"] = None
        else:
            detail = scoring.score(m, base, self.weights, self._score_period(base))
            cand["score"] = detail["score"]
            cand["score_detail"] = detail
        cand["advantage"] = None

    # -- the loop ------------------------------------------------------------

    def run(self) -> int:
        if self.args.no_llm and not self.args.dry_run:
            die("the portfolio loop needs a model to write the candidates.\n"
                "        Use --dry-run to inspect the prompts, or drop --no-llm.")
        if not self.args.dry_run and not llm.available():
            die("`claude` not found on PATH -- install Claude Code, "
                "or use --dry-run")

        info(f"run: {self.outdir}")
        info(f"skill library: {self.lib.path} ({len(self.lib)} entries)")
        if self.skill:
            per_role = ", ".join(
                f"{r} {len(skillgen.load_doc(role=r))}"
                for r in skillgen.ALL_ROLES)
            info(f"skill document: {len(self.skill)} chars "
                 f"({skillgen.SKILL_PATH.name}); injected per role: {per_role}")
        else:
            info("skill document: none (run: astra skilldoc build)")
        info(f"planned model calls: {self.calls_planned}"
             + (f", capped at {self.args.max_calls}" if self.args.max_calls else ""))
        if self.args.max_calls and self.calls_planned > self.args.max_calls:
            die(f"this run plans {self.calls_planned} model calls, over the "
                f"--max-calls limit of {self.args.max_calls}. Lower --iters, "
                f"--top-k or --clean, or raise the limit.")
        if self.skill:
            shutil.copy2(skillgen.SKILL_PATH, self.outdir / "skill_doc.md")

        self.baseline()
        self._save()

        parent_dir = self.outdir / "baseline"
        parent_rtl = self._rtl_paths(parent_dir / "00_inputs")
        parent_metrics = dict(self.state["baseline"]["metrics"])
        parent_metrics["_score"] = 0.0

        # Stage 1, before anything is timed.
        if self.args.clean > 0:
            cleaned, rec = self.cleanup(parent_rtl)
            self.state["cleanup"] = rec
            if rec.get("adopted"):
                adopted = next(c for c in rec["candidates"]
                               if c["id"] == rec["id"])
                parent_rtl = cleaned
                parent_dir = Path(adopted["metrics"]["run_dir"])
                parent_metrics = dict(adopted["metrics"])
                parent_metrics["_score"] = rec["score"]
            self._save()

        best_overall = {"metrics": parent_metrics,
                        "score": parent_metrics.get("_score", 0.0),
                        "id": "D_0", "iteration": 0,
                        "rtl": [str(p) for p in parent_rtl]}
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

            parent_rtl = [Path(p) for p in best["rtl"]]
            parent_dir = Path(best["metrics"]["run_dir"])
            parent_metrics = dict(best["metrics"])
            parent_metrics["_score"] = best["score"]

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
        self.state["budget"]["actual_calls"] = llm.usage().get("calls")
        self._save()
        (self.outdir / "budget.json").write_text(
            json.dumps(self.state["budget"], indent=2) + "\n")
        self._publish_best(best_overall)
        self._summarise()
        return 0

    # -- reporting -----------------------------------------------------------

    def _summarise(self) -> None:
        base = self.state["baseline"]["metrics"]
        best = self.state["best"] or {}
        bm = best.get("metrics") or {}

        def delta(key: str) -> str:
            a, c = base.get(key), bm.get(key)
            if a is None or c is None or abs(a) < 1e-12:
                return "n/a"
            return f"{(c - a) / abs(a) * 100:+.1f}%"

        L = [
            f"# Path-portfolio optimisation -- {self.design}",
            "",
            f"run           {self.outdir.name}",
            f"target clock  {self.period:g} ns",
            f"stage         {self.args.stage}",
            f"iterations    {len(self.state['iterations'])} "
            f"(converged at {self.state.get('convergence_steps') or 'n/a'})",
            f"targets       up to {self.args.top_k} per iteration",
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

        cands = [c for it in self.state["iterations"] for c in it.get("candidates", [])]
        unions = [c for it in self.state["iterations"] for c in it.get("unions", [])]
        L += [scoring.render_sec_tally(scoring.sec_tally(cands)) + "  (specialists)",
              "", scoring.render_sec_tally(scoring.sec_tally(unions)) + "  (unions)",
              ""]

        cl = self.state.get("cleanup") or {}
        if cl:
            L += ["## Pre-synthesis pass", "",
                  ("adopted `%s` (score %s)" % (cl.get("id"), fmt(cl.get("score"))))
                  if cl.get("adopted") else
                  f"not adopted -- {cl.get('reason', 'no candidate cleared the gate')}",
                  ""]

        L.append("## Portfolio per iteration")
        L.append("")
        for it in self.state["iterations"]:
            pf = it.get("portfolio") or {}
            L.append(f"### Iteration {it['iteration']}")
            L.append("")
            L.append(f"{pf.get('k_effective', 0)} of {pf.get('k_requested', 0)} "
                     f"target(s) from {pf.get('cluster_count', 0)} distinct cone(s)"
                     + ("; one cone, cut into segments" if pf.get("collapsed") else ""))
            L.append("")
            L.append("| target | kind | value | P(limits clock) | severity "
                     "| delay share | cell mix |")
            L.append("|---|---|---|---|---|---|---|")
            for tg in pf.get("targets", []):
                mix = ", ".join(f"{n}x {f}" for f, n
                                in list((tg.get("cell_families") or {}).items())[:3])
                pc = tg.get("p_critical")
                L.append(f"| {tg['id']} | {tg['kind']} | {tg['value']:.3f} | "
                         f"{f'{pc:.0%}' if pc is not None else '-'} | "
                         f"{tg['value_terms']['severity']:.2f} | "
                         f"{tg['delay_share']:.0%} | {mix} |")
            L.append("")
            L.append("| candidate | pattern -> strategy | SEC | WNS | area | score |")
            L.append("|---|---|---|---|---|---|")
            for c in it.get("candidates", []) + it.get("unions", []):
                m = c.get("metrics") or {}
                sel = " **<-**" if c["id"] == it.get("selected") else ""
                L.append(
                    f"| {c['id']}{sel} | {c.get('pattern', '-')} -> "
                    f"{c.get('strategy', '-')} | "
                    f"{'pass' if (c.get('sec') or {}).get('equivalent') else 'FAIL'} | "
                    f"{fmt(m.get('wns_ns'))} | {fmt(m.get('area_um2'))} | "
                    f"{fmt(c.get('score'))} |")
            L.append("")

        L += ["## Did the merge agent earn its call?", "",
              "Negative means the agent's union beat the one assembled by text "
              "splicing for free; positive means it did not.", "",
              "| iteration | agent union | best mechanical | delta |",
              "|---|---|---|---|"]
        for it in self.state["iterations"]:
            ag = ((it.get("merge") or {}).get("agent")) or {}
            if not ag or ag.get("skipped") or ag.get("error"):
                L.append(f"| {it['iteration']} | "
                         f"{ag.get('skipped') or ag.get('error') or 'n/a'} | | |")
                continue
            L.append(f"| {it['iteration']} | {fmt(ag.get('score'))} | "
                     f"{ag.get('best_mechanical') or '-'} | "
                     f"{fmt(ag.get('vs_mechanical'))} |")
        L.append("")

        viol = [c for c in cands if (c.get("scope") or {}).get("violation")]
        if viol:
            L += [f"**Scope violations: {len(viol)}/{len(cands)}.** Specialists "
                  "edited RTL outside their assigned target this often. A high "
                  "rate here means the merge stage's premise -- that the "
                  "candidates edit disjoint text -- does not hold on this "
                  "design.", ""]

        L += ["## Skill library after this run", "", "```",
              skills_mod.render(self.lib.all(True), with_examples=False), "```"]

        u = self.state.get("usage") or {}
        if u.get("calls"):
            L += ["", f"Model calls: {u['calls']} "
                  f"(planned {self.calls_planned}), "
                  f"in {u['input_tokens']} + cache {u['cache_read_input_tokens']}, "
                  f"out {u['output_tokens']}, notional ${u['cost_usd']:.4f}."]

        out = self.outdir / "summary.md"
        out.write_text("\n".join(L) + "\n")
        info(f"summary: {out}")
        print()
        print("\n".join(L[:22]))


# ===========================================================================
# budget and cli
# ===========================================================================


def call_budget(args: argparse.Namespace) -> int:
    """Model calls this configuration will make, before it makes any.

    Path selection, the mechanical unions and every evaluation are free; only
    the specialists, the merge agent and skill learning cost anything.
    """
    if args.dry_run:
        return 0
    per_iter = args.top_k
    if not args.no_merge_agent:
        per_iter += 1
    if not args.no_skill_agent:
        per_iter += 1
    return args.clean + args.iters * per_iter


def build_parser() -> argparse.ArgumentParser:
    p = drrtl.build_parser()
    p.prog = "astra-portfolio"
    p.description = __doc__

    # --top-k means "how many targets", which is also how many specialists run.
    # It is the flag this mode is driven by, so it gets a short form too.
    for a in p._actions:
        if a.dest == "top_k":
            a.help = "path targets, and so specialists, per iteration"
            if "-k" not in a.option_strings:
                a.option_strings.insert(0, "-k")
                p._option_string_actions["-k"] = a
        if a.dest == "npaths":
            # The pool is one path per endpoint; 20 truncates the TNS
            # denominator on anything bigger than the shipped designs.
            a.default = 60
        if a.dest == "iters":
            # A ceiling, not a target: `--patience` stops a loop that has
            # stopped earning, so raising this costs calls only on runs where
            # later iterations are actually finding something. Every run
            # recorded so far peaked at iteration 1, with one exception, so
            # the headroom matters more than the expectation.
            a.default = 4
            a.help = "maximum iterations; --patience ends a stalled run sooner"
        if a.dest == "patience":
            # One more than the old default, so a four-iteration ceiling can
            # actually be reached: at patience 2 the loop always stopped at
            # three regardless of --iters.
            a.default = 3

    g = p.add_argument_group("portfolio")
    g.add_argument("--mmr-lambda", type=float, default=pathsel.MMR_LAMBDA,
                   help="diversity weight when selecting targets")
    g.add_argument("--cluster-at", type=float, default=pathsel.CLUSTER_AT,
                   help="similarity at which two paths are the same bottleneck")
    g.add_argument("--delay-sigma", type=float, default=pathsel.DELAY_SIGMA,
                   help="delay uncertainty as a fraction of the clock period, "
                        "used to rank targets by how likely each is to be what "
                        "limits the clock; 0 gives the deterministic answer")
    g.add_argument("--cone-rho", type=float, default=pathsel.CONE_RHO,
                   help="share of that uncertainty common to a whole cone")
    g.add_argument("--stage", choices=("sta", "pnr"), default="sta",
                   help="which timing report to select targets from")
    g.add_argument("--clean", type=int, default=1,
                   help="pre-synthesis cleanup candidates (0 disables the stage)")
    g.add_argument("--clean-gate", type=float, default=0.01,
                   help="Eq. 3 improvement a cleanup candidate must show")
    g.add_argument("--no-merge-agent", action="store_true",
                   help="mechanical unions only; saves one call per iteration")
    g.add_argument("--max-calls", type=int, default=0,
                   help="refuse to start if the plan exceeds this many calls")
    return p


def main() -> int:
    args = build_parser().parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    return PortfolioOrchestrator(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
