#!/usr/bin/env python3
"""Build the RTL timing-optimisation skill document.

Every agent in this repo runs with all tools denied, so a Claude Code skill
cannot be loaded the way skills normally are -- there is no Read tool to load
it with, and giving one back would turn a one-shot call into an agent that
greps the repo. The document is therefore read from disk by the orchestrator
and concatenated into the system prompt.

Two modes, and the mechanical one is the default:

  ``skillgen build``          renders SKILL.md from the committed skill library
                              plus a hand-written preamble. Zero model calls,
                              deterministic, diffable.
  ``skillgen build --llm``    one research call that may consult public
                              documentation on RTL and synthesis optimisation
                              and expand the catalogue. The result is committed
                              to git, so nothing at run time depends on it.

The research call is the only place in this repo where a model is given network
tools. It is deliberately quarantined here rather than routed through
``llm.call``, whose blanket tool denial is load-bearing for the loop's cost
model and must not be relaxed for everything just because one one-off step
needs it.

Whatever the mode, harvested transformations enter ``skills/library.json`` as
seeds: source "seed", zero occurrences, zero confidence. They are suggestions,
and they earn statistics from real runs like anything else. Writing plausible
numbers next to them would be fabricating evidence, and the library's whole
value is that its numbers mean something.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skills as skills_mod  # noqa: E402

ROOT = Path(os.environ.get("ASTRA_ROOT", Path(__file__).resolve().parent.parent))
SKILL_DIR = ROOT / ".claude" / "skills" / "rtl-timing-optimization"
SKILL_PATH = SKILL_DIR / "SKILL.md"

# The document is pasted into every agent's system prompt, once per candidate.
# It must fit whole: the catalogue sits at the end, so a budget below the
# document's length silently deletes it and nobody notices. Roughly 3k tokens
# against the ~10k of harness overhead each call already carries.
BUDGET = 14000

# Which agent needs which section. The document goes into a system prompt once
# per candidate, so a section an agent cannot act on is pure cost: it pushes
# the RTL and the target brief further down a prompt the model holds all of.
#
# The merge agent is the clear case. It reconciles diffs between rewrites that
# already exist and never invents a transformation, so the catalogue and the
# whole path-reading apparatus say nothing it can use. What it does need is the
# equivalence rules and the warning about reformatting -- a reflowed file is
# precisely what makes a merge impossible.
#
# The cleanup agent runs before anything has been synthesised, so instructions
# for reading a timing report describe evidence it does not have yet.
ALL_ROLES = ("cleanup", "specialist", "merge")

_SECTION_ROLES: dict[str, tuple[str, ...]] = {
    "The invariants": ALL_ROLES,
    "What synthesis already does": ("cleanup", "specialist"),
    "Where an LLM is reliable": ("cleanup", "specialist"),
    "How to read a critical path": ("specialist",),
    "What reliably works": ("cleanup", "specialist"),
    "Coding patterns that block": ("cleanup", "specialist"),
    "What wastes an iteration": ALL_ROLES,
    "Arguing equivalence": ALL_ROLES,
    "Transformation catalogue": ("cleanup", "specialist"),
    "Choosing among them": ("specialist",),
}

_ROLE_MARK = "<!-- roles:"

_FRONTMATTER = """\
---
name: rtl-timing-optimization
description: >-
  Structural Verilog transformations that recover setup slack without changing
  function, latency or interface. Use when rewriting RTL against a measured
  critical path.
---
"""

_PREAMBLE = """\
# RTL timing optimisation

Recovering setup slack means removing logic levels between two flops. Nothing
else here matters as much as that sentence: a transformation that moves gates
around without shortening the longest path from a launch flop to a capture
flop has not helped, however much better the code reads afterwards.

## The invariants

Checked by tools after you answer, so there is nothing to gain by bending them:

1. **Functional equivalence.** A sequential equivalence check runs against the
   original design. Bit-exact outputs, cycle for cycle. A rewrite that changed
   behaviour is discarded no matter how much slack it recovered.
2. **Latency and interface are fixed.** Same module name, ports, widths,
   pipeline depth. You may redistribute or duplicate registers. You may not
   add or remove a pipeline stage.
3. **Synthesisable Verilog-2005.** No initial blocks, no delays, no testbench
   constructs.

## What synthesis already does -- doing it again wastes the iteration

Modern synthesis performs these before you see a timing report. Redoing them in
RTL changes nothing and costs an equivalence proof and a synthesis run:

- **Constant folding and propagation.** A cell with constant inputs is already
  replaced by its value.
- **Common subexpression elimination and identical-cell merging.** Writing an
  expression once into a temporary is not an optimisation; the tool already
  merged the duplicates.
- **Boolean minimisation and technology mapping.** Restating logic with
  different operators -- De Morgan, `&`/`|` swaps, `~^` for `^~` -- produces
  the same gates. The tool canonicalises before mapping.
- **Trivial resource sharing.** Two adders in mutually exclusive branches are
  already shared where it is profitable.
- **Retiming**, where the flow enables it: registers are moved across
  combinational logic automatically.
- **Buffer insertion and gate sizing** for load and fanout.

What synthesis cannot do is change the *algorithm*. It will not turn a serial
chain into a tree, will not decide that a comparison is decidable from earlier
values, and will not restructure a datapath into a redundant number
representation. Those require knowing what the code means. That is where you
add value, and it is the only place you do.

## Where an LLM is reliable at this, and where it is not

Published evaluations of LLM-driven RTL optimisation find the ability is
uneven, and it is worth knowing which side of the line you are on:

- **Reliable:** datapath restructuring, arithmetic reassociation, multiplexer
  and select-network rework, Boolean simplification. Models match or beat
  classical tooling here.
- **Unreliable:** finite-state-machine restructuring -- redundant and
  pass-through states are consistently left in place -- and anything crossing
  a clock domain, where models tend to add synchroniser complexity rather than
  remove it, and can break the design outright.

So: prefer datapath work. If the target is an FSM or a clock-domain crossing,
be conservative, and prefer returning the file unchanged over a rewrite you
cannot argue precisely.

## How to read a critical path

Instance names in an OpenSTA report (`_14637_`, or a chain like
`acc_out[33]_DFF_X1_Q_D_AOI21_X1_ZN`) are synthesis-generated and carry no
design meaning. The *sequence of cell types* does:

| what you see | what it is |
|---|---|
| a wall of AND/OR feeding a wide reduction | partial-product generation, or a comparison |
| a long run of XOR with AOI/OAI between | carry propagation through an adder |
| repeated MUX cells in series | a priority chain or a select cascade |
| one stage with fanout in the tens | a net driving a whole datapath |
| delay spread evenly over many stages | the depth itself is the problem, not any cell |

Where the delay sits decides which transformation applies. A path with its
delay concentrated in a few of many stages has a local fix; one where it is
spread evenly does not, and needs the structure changed.

## What reliably works

Ordered by how often it recovers real slack.

**Rebalance a serial chain into a tree.** `((a+b)+c)+d` is three adders deep;
`(a+b)+(c+d)` is two. For n terms the depth falls from n-1 to ceil(log2 n), and
the gain grows with every term. The single most productive rewrite on
accumulate and reduction logic.

**Speculate, then select late.** The canonical critical path in a pipelined
processor is not the arithmetic alone -- it is a select network *in series
with* it: hazard detection, then operand forwarding, then the ALU, one after
another. The fix is to compute every candidate result in parallel and choose
between them with one late mux, so the selection is off the arithmetic path
instead of in front of it. The same shape appears as carry-select adders and
as branch-condition speculation.

**Move late work off the path.** If a condition is computed *after* a long
arithmetic result and then used to select against it, derive it beside the
arithmetic from the partial results instead. A saturation clamp at the end of a
wide accumulate is the textbook case: overflow is decidable from the operands,
not only from the sum.

**Carry-save intermediate sums.** Every full-width add resolves a carry chain.
Keeping a running total as a redundant (sum, carry) pair makes each
intermediate add a constant-depth 3:2 compression, and pays for one
carry-propagate adder at the very end instead of one per term. A multi-operand
expression should reduce through a single compression tree with one final
adder. The depth is the number of 3:2 reducers a signal passes, not the number
of additions written.

**Replicate a high-fanout driver.** A net driving many loads needs a large
driver or a buffer tree, and both sit on the launching path. Duplicating the
logic that produces it splits the load without changing the value. Register
high-fanout control, enable and reset signals rather than fanning out
combinational logic.

**Narrow a comparison to the bits that decide it.** A wide compare against a
constant usually turns on a handful of high bits, especially when the constant
is at or near a power of two. So does an equality test against a sparse
constant.

**Flatten a priority cascade.** A chain of `?:`, or a `for` loop where each
iteration consumes the previous one, is a mux chain as deep as the loop trip
count. Decoding the selects in parallel and reducing with a balanced tree
makes it logarithmic.

**Retime across registers that already exist.** Where a pipeline has stages,
work can be moved between them. Latency must not change: this redistributes
logic across existing flops, it does not add one.

## Coding patterns that block the tool's own datapath optimisation

These are not slow in themselves -- they stop synthesis from applying the
architecture-level optimisations it would otherwise apply to an arithmetic
chain, which is worse, because the loss is invisible in the source:

- **A truncated intermediate.** Assigning `a*b + c` to a narrower wire and
  widening again later splits one datapath into two, and each is optimised
  alone. Widen the intermediate to the width the arithmetic actually produces.
- **A register inside the datapath.** A flop between two operators prevents
  them being merged. Put the register at the *output* of the arithmetic and
  let retiming place it, rather than pipelining by hand mid-chain.
- **Mixed signedness in one expression.** Signed and unsigned operands force
  separate operator implementations. Make the intent explicit and consistent.
- **Arithmetic split across a module boundary.** An operator chain that crosses
  hierarchy cannot be merged into one block.
- **An explicitly instantiated arithmetic component.** A hand-instantiated
  adder or multiplier is a black box; the inferred operator lets the tool pick
  an architecture suited to its context.

One caveat, and it matters here: how much of this applies depends on the
synthesis tool. A flow with a real datapath extractor gains a great deal from
these; a simpler open-source flow may not merge operators at all, and there
*narrowing* an intermediate to the width the data provably occupies can be the
winning move rather than the mistake. Trust the measurement over the rule.

## What wastes an iteration

- Renaming signals, reordering independent statements, adding attributes.
- Restating the same logic with different operators.
- Reformatting. The file is combined with other agents' edits by splicing
  text; a reflowed file cannot be combined with anything and is discarded.
- Attacking a construct that is not on the critical path. An area saving
  elsewhere buys no slack here.
- Adding a pipeline stage. It fails the equivalence check, and costs a proof
  to find that out.

## Arguing equivalence

Every rewrite needs the reason its outputs are bit-identical. Be specific about
the two things that actually break rewrites:

- **Reset.** Does the restructured logic reset to the same values, in the same
  cycle?
- **Width and sign.** Reassociating a sum moves where intermediates are
  truncated. `(a+b)+c` and `a+(b+c)` are equal in unbounded arithmetic and can
  differ in fixed width if an intermediate overflows. Widen intermediates, or
  state why they cannot overflow. Sign extension before a reassociated add is
  a frequent source of a failed check.
"""


_CATALOGUE_HEAD = """\
## Transformation catalogue

An index, not a reference: whichever of these match your target are supplied in
full alongside it, with their rationale and their measured record. **The
entries carry no evidence on their own** -- the loop records how each actually
fares, so a later run can tell which work on real designs.
"""

_FOOTER = """\
## Choosing among them

Prefer the transformation that removes the most logic levels from the *measured*
path. When two are comparable, prefer the one whose equivalence argument is
easier to make: a rewrite that cannot be proven equivalent is worth nothing,
and the proof is run on every candidate.
"""


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _roles_for(heading: str) -> tuple[str, ...]:
    for prefix, roles in _SECTION_ROLES.items():
        if heading.startswith(prefix):
            return roles
    return ALL_ROLES        # an unrecognised section goes to everyone


def _tag_sections(md: str) -> str:
    """Put a role marker under every `##` heading.

    Written into the document rather than kept only in this file, so a
    regenerated or hand-edited SKILL.md carries its own routing instead of
    silently falling back to sending every section to every agent.
    """
    out: list[str] = []
    for line in md.splitlines():
        out.append(line)
        if line.startswith("## "):
            out.append(f"{_ROLE_MARK} {' '.join(_roles_for(line[3:].strip()))} -->")
    return "\n".join(out)


def render_doc(lib: skills_mod.SkillLibrary) -> str:
    """The whole document, assembled from the library and the preamble."""
    entries = sorted(lib.all(include_invalid=False),
                     key=lambda e: (e.get("pattern") or ""))
    rows = [_CATALOGUE_HEAD, "", "| pattern | transformation |", "|---|---|"]
    for e in entries:
        pat = str(e["pattern"]).replace("|", "/")
        strat = str(e["strategy"]).replace("|", "/")
        rows.append(f"| {pat} | {strat} |")
    rows.append("")
    body = _tag_sections("\n".join([_PREAMBLE, "\n".join(rows), _FOOTER]))
    return f"{_FRONTMATTER}\n{body}\n"


def load_doc(path: Path | None = None, role: str | None = None) -> str:
    """The skill body, without its frontmatter or its role markers.

    ``role`` keeps only the sections that role can act on. A document carrying
    no markers -- hand-written, or produced before the markers existed -- is
    returned whole, so filtering can never silently empty it.

    Callers degrade rather than fail: the pipeline runs without a skill
    document, it just runs with less guidance.
    """
    p = Path(path or SKILL_PATH)
    try:
        text = p.read_text()
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            text = text[end + 4:]
    text = text.strip()

    kept: list[str] = []
    keep, tagged = True, False
    for line in text.splitlines():
        if line.startswith("## "):
            kept.append(line)
            keep = True                     # settled by the marker beneath it
            continue
        if line.startswith(_ROLE_MARK):
            tagged = True
            keep = role is None or role in line
            if not keep and kept and kept[-1].startswith("## "):
                kept.pop()                  # drop the heading of a cut section
            continue
        if keep:
            kept.append(line)
    if not tagged:
        return text

    # Collapse the blank runs left behind where sections were removed.
    out: list[str] = []
    blanks = 0
    for line in kept:
        blanks = blanks + 1 if not line.strip() else 0
        if blanks < 3:
            out.append(line)
    return "\n".join(out).strip()


def inject(system: str, doc: str, budget: int = BUDGET) -> str:
    """Append the skill document to a system prompt, truncated at a line.

    A system prompt that stops mid-sentence is worse than one that is missing a
    section, so the cut is made at a line boundary and marked, rather than at
    an arbitrary character.
    """
    if not doc:
        return system
    if len(doc) > budget:
        cut = doc.rfind("\n", 0, budget)
        doc = doc[: cut if cut > 0 else budget].rstrip()
        doc += f"\n\n[skill document truncated at {budget} characters]"
    return f"{system}\n\n# Reference: RTL timing optimisation\n\n{doc}"


# ---------------------------------------------------------------------------
# the research call
# ---------------------------------------------------------------------------

RESEARCH_SYSTEM = """\
You are compiling a reference document on RTL timing optimisation for an
automated Verilog-rewriting agent.

Research what established RTL and synthesis optimisation tools actually do to
recover setup slack -- vendor documentation for commercial synthesis and RTL
optimisation flows, open-source synthesis passes, and standard digital design
references. Extract the *transformations*, not the marketing.

Reply with exactly one JSON object and nothing else:

{
  "skills": [
    {
      "pattern": "<the structural bottleneck, at most 10 words, phrased so it
                   applies to a design nobody has seen -- no identifiers from
                   any specific design>",
      "strategy": "<the transformation, at most 10 words>",
      "rationale": "<the mechanism: why this removes logic levels. Two or three
                     sentences.>",
      "example": "<a few lines of generic, synthesisable Verilog-2005 showing
                   the shape of the transformation>"
    }
  ],
  "notes": "<what your sources agreed on, and where they disagreed>"
}

Rules that matter:

- Pattern and strategy are library keys. Two entries that mean the same thing
  must be phrased the same way, or their statistics fragment across near
  duplicates and neither ever accumulates enough evidence to be trusted.
- Only transformations that preserve function, latency and interface. Adding a
  pipeline stage is out of scope: this agent may not change latency.
- Report no success rates, percentages, or trial counts. Every entry enters the
  library untested and earns its statistics from real runs. Numbers you supply
  would be indistinguishable from measured ones and would corrupt the library.
- At most 16 entries. A short catalogue of transformations that genuinely work
  beats a long one padded with restatements."""

RESEARCH_PROMPT = """\
Compile the transformation catalogue described in your instructions.

Context for scoping it: the consuming agent rewrites small Verilog-2005 modules
(tens to hundreds of lines) to close setup timing after synthesis with Yosys
and static timing analysis with OpenSTA on a Nangate45 standard-cell library.
It sees a critical path localised back to RTL lines, and it must return a
complete rewritten file whose behaviour is bit-identical, cycle for cycle.

Existing entries, which you should not merely restate -- add what is missing,
and phrase anything overlapping the same way so it merges rather than forks:

{existing}
"""

# The one call in this repo that gets network tools, and only these.
RESEARCH_TOOLS = "WebSearch,WebFetch"


def research(existing: list[dict[str, Any]], model: str = "opus",
             timeout: int = 900, dry_run: bool = False) -> dict[str, Any]:
    """One research call. Returns the parsed reply, or raises RuntimeError."""
    listing = "\n".join(f"- {e['pattern']} -> {e['strategy']}" for e in existing)
    prompt = RESEARCH_PROMPT.format(existing=listing or "(none)")
    if dry_run:
        return {"prompt": prompt, "system": RESEARCH_SYSTEM, "skills": []}

    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("`claude` not found on PATH -- install Claude Code, "
                           "or run `skillgen build` without --llm")
    cmd = [exe, "-p", "--output-format", "json", "--model", model,
           "--append-system-prompt", RESEARCH_SYSTEM,
           "--allowedTools", RESEARCH_TOOLS,
           "--strict-mcp-config"]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"research call timed out after {timeout}s") from e
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: "
                           f"{(proc.stderr or '').strip()[:300]}")
    try:
        res = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"unparsable output: {proc.stdout[:300]}") from e
    if res.get("is_error"):
        raise RuntimeError(f"claude reported an error: "
                           f"{str(res.get('result', ''))[:300]}")

    import drrtl                                     # noqa: PLC0415 -- cycle
    parsed = drrtl.extract_json(str(res.get("result", ""))) or {}
    u = res.get("usage") or {}
    parsed["_usage"] = {
        "input_tokens": u.get("input_tokens"),
        "output_tokens": u.get("output_tokens"),
        "cost_usd": res.get("total_cost_usd"),
    }
    return parsed


# Transformations distilled from vendor datapath-synthesis guidance, from
# published work on where LLM RTL optimisation succeeds and fails, and from the
# documented critical paths of standard reference designs. They enter the
# library as seeds like everything else -- zero occurrences, zero confidence --
# to be confirmed or refuted by runs.
RESEARCHED_SEEDS: list[dict[str, str]] = [
    {
        "pattern": "select network in series with a long arithmetic path",
        "strategy": "speculate every outcome in parallel and select at the end",
        "rationale": (
            "The canonical critical path in a pipelined datapath is not the "
            "arithmetic alone but a selection standing in front of it: hazard "
            "detection, then operand forwarding, then the ALU, in series. "
            "Computing each candidate result concurrently and choosing with "
            "one late multiplexer takes the selection off the arithmetic path. "
            "A carry-select adder exploits the same shape."),
        "example": (
            "// before: select, then compute\n"
            "wire [W-1:0] op = fwd ? ex_result : rf_data;\n"
            "wire [W-1:0] y  = op + b;\n"
            "// after: compute both, select last\n"
            "wire [W-1:0] y0 = rf_data   + b;\n"
            "wire [W-1:0] y1 = ex_result + b;\n"
            "wire [W-1:0] y  = fwd ? y1 : y0;"),
    },
    {
        "pattern": "intermediate arithmetic result truncated between operators",
        "strategy": "widen the intermediate so the operator chain stays one datapath",
        "rationale": (
            "Assigning a product-plus-sum to a narrower wire and widening again "
            "downstream splits one arithmetic block into two, each optimised "
            "alone, so no compression tree spans them. Tool-dependent: a flow "
            "without a datapath extractor gains nothing here, and there "
            "narrowing to the width the data provably occupies can instead be "
            "the winning move."),
        "example": ("wire [15:0] t = a * b + c;   // splits the datapath\n"
                    "wire [16:0] t = a * b + c;   // keeps it whole"),
    },
    {
        "pattern": "register placed inside an arithmetic chain",
        "strategy": "register the datapath output and let retiming place it",
        "rationale": (
            "A flop between two operators stops them being merged, so each is "
            "implemented separately and no carry architecture can be chosen "
            "across the pair. Registering the end of the chain and letting "
            "retiming move the boundary gives the same latency and a better "
            "structure."),
    },
    {
        "pattern": "mixed signed and unsigned operands in one expression",
        "strategy": "make signedness explicit and consistent across the expression",
        "rationale": (
            "A signed subexpression combined with an unsigned one forces two "
            "operator implementations where one would serve, and puts "
            "conversion logic on the path between them."),
    },
    {
        "pattern": "arithmetic operator chain split across a module boundary",
        "strategy": "keep the whole chain in one hierarchy so it can merge",
        "rationale": (
            "Hierarchy is a hard boundary for operator merging. A multiply in "
            "one module feeding an add in its parent cannot become a single "
            "compression tree, however obviously the two belong together."),
    },
    {
        "pattern": "hand-instantiated arithmetic component",
        "strategy": "use the inferred operator so the tool picks the architecture",
        "rationale": (
            "An explicitly instantiated adder or multiplier is a black box with "
            "a fixed architecture. The inferred operator lets synthesis choose "
            "one suited to its context and merge it with its neighbours."),
    },
    {
        "pattern": "loop-carried dependency in a generate loop",
        "strategy": "reduce with a balanced tree instead of a linear scan",
        "rationale": (
            "A generate loop whose body consumes the previous iteration's "
            "output elaborates into a chain as deep as the trip count. A "
            "priority cascade, a ripple carry and a shift chain are all written "
            "this way. The source shows one assignment and synthesis produces N "
            "levels, which is why reading the code does not reveal it."),
        "example": ("// depth N\n"
                    "for (i = N-1; i >= 0; i = i - 1)\n"
                    "  assign h[i] = r[i] | h[i+1];\n"
                    "// depth ceil(log2 N)\n"
                    "assign any = |r;"),
    },
]


def ensure_seeds(lib: skills_mod.SkillLibrary,
                 entries: list[dict[str, Any]]) -> list[str]:
    """Add harvested transformations as seeds. Never as evidence."""
    added: list[str] = []
    for e in entries[:16]:
        pattern, strategy = (e.get("pattern") or "").strip(), (e.get("strategy") or "").strip()
        if not pattern or not strategy:
            continue
        before = len(lib)
        entry = lib.add_seed(pattern, strategy, e.get("rationale", ""),
                             e.get("example", ""))
        if len(lib) > before:
            added.append(entry["id"])
    return added


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="astra-skilldoc",
        description="Build the RTL timing-optimisation skill document.")
    ap.add_argument("action", nargs="?", default="build",
                    choices=("build", "show", "path"))
    ap.add_argument("--llm", action="store_true",
                    help="one research call that may consult public sources")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--dry-run", action="store_true",
                    help="print the research prompt and call nothing")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing SKILL.md")
    ap.add_argument("--skills", type=Path, default=None)
    args = ap.parse_args(argv[1:])

    if args.action == "path":
        print(SKILL_PATH)
        return 0
    if args.action == "show":
        doc = load_doc()
        print(doc or "(no skill document -- run: astra skilldoc build)")
        return 0

    lib = skills_mod.SkillLibrary(args.skills)
    added = ensure_seeds(lib, RESEARCHED_SEEDS)
    if added:
        lib.save()
        print(f"[skillgen] added {len(added)} researched seed(s) to {lib.path}")

    if args.llm:
        try:
            parsed = research(lib.all(True), args.model, args.timeout, args.dry_run)
        except RuntimeError as e:
            print(f"[skillgen] ERROR: {e}", file=sys.stderr)
            return 1
        if args.dry_run:
            print(parsed["system"])
            print("\n" + "=" * 70 + "\n")
            print(parsed["prompt"])
            print(f"\n[skillgen] dry run: ~{len(parsed['prompt']) // 4} prompt "
                  f"tokens, nothing called")
            return 0
        added = ensure_seeds(lib, parsed.get("skills") or [])
        lib.save()
        print(f"[skillgen] harvested {len(parsed.get('skills') or [])} "
              f"transformation(s), {len(added)} new seed(s) in {lib.path}")
        if parsed.get("notes"):
            print(f"[skillgen] notes: {parsed['notes']}")
        u = parsed.get("_usage") or {}
        if u.get("cost_usd") is not None:
            print(f"[skillgen] 1 call, in {u.get('input_tokens')} "
                  f"out {u.get('output_tokens')}, notional ${u['cost_usd']:.4f}")

    if SKILL_PATH.is_file() and not args.force and not args.llm:
        print(f"[skillgen] {SKILL_PATH} already exists; --force to rebuild")
        return 0

    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    SKILL_PATH.write_text(render_doc(lib))
    print(f"[skillgen] wrote {SKILL_PATH} "
          f"({len(SKILL_PATH.read_text())} chars, {len(lib)} catalogue entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
