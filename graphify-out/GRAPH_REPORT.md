# Graph Report - astra  (2026-09-10)

## Corpus Check
- 38 files · ~84,499 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1090 nodes · 1886 edges · 60 communities (44 shown, 15 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 21 edges (avg confidence: 0.85)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `6f39a02f`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- pathsel.py
- drrtl.py
- rtlscan.py
- astra.py
- ClockSet
- skills.py
- portfolio.py
- rtl_map.py
- score.py
- merge.py
- parse_sta.py
- ASTRA — shared VLSI container
- skillgen.py
- ._cs
- sec.py
- TestRtlMap
- TestClockSet
- Multi-clock support
- TestSkillDoc
- selftest.py
- _NoRtl
- HANDOFF — ASTRA path-portfolio addon
- TestEq3
- astra_advise.py
- TestClockResolution
- TestSkills
- TestEquivalenceContract
- TestPathSelCriticality
- RTL timing optimisation
- TestMerge
- TestRtlScan
- TestPortfolioBudget
- TestSkillMerging
- TestPathSelSegments
- TestEq5
- _path
- TestClockGroupParsing
- TestReplyParsing
- ._setup
- TestInconclusiveSec
- TestPathSelOnRealRun
- TestEq4
- protect.py
- TestStageParameterisation
- TestPortfolioAgents
- TestPathSelMetrics
- alu32.v
- dual_path.v
- mac_chain.v
- soc_bench.v
- entrypoint.sh
- TestProtectedRegions
- The equivalence contract
- Path-portfolio mode
- Dr. RTL optimisation loop
- Setup
- dual_clock.v
- netproc.v
- TestPathSelValue

## God Nodes (most connected - your core abstractions)
1. `_path()` - 30 edges
2. `ClockSet` - 26 edges
3. `TestRtlMap` - 22 edges
4. `build_parser()` - 18 edges
5. `SkillLibrary` - 18 edges
6. `RtlIndex` - 16 edges
7. `scan()` - 16 edges
8. `Orchestrator` - 15 edges
9. `TestClockSet` - 15 edges
10. `TestSkillDoc` - 15 edges

## Surprising Connections (you probably didn't know these)
- `render_doc()` --references--> `SkillLibrary`  [EXTRACTED]
  tools/skillgen.py → tools/skills.py
- `ensure_seeds()` --references--> `SkillLibrary`  [EXTRACTED]
  tools/skillgen.py → tools/skills.py
- `path_features()` --references--> `RtlIndex`  [EXTRACTED]
  tools/pathsel.py → tools/rtl_map.py
- `_scope()` --references--> `RtlIndex`  [EXTRACTED]
  tools/pathsel.py → tools/rtl_map.py
- `_span_regions()` --references--> `RtlIndex`  [EXTRACTED]
  tools/pathsel.py → tools/rtl_map.py

## Import Cycles
- None detected.

## Communities (60 total, 15 thin omitted)

### Community 0 - "pathsel.py"
Cohesion: 0.06
Nodes (62): Counter, build_targets(), _by_cluster(), _cell_count(), _clock_coverage(), cluster(), cluster_similarity(), cluster_value() (+54 more)

### Community 1 - "drrtl.py"
Cohesion: 0.09
Nodes (28): build_parser(), die(), EvaluationAgent, extract_json(), extract_verilog(), fmt(), info(), main() (+20 more)

### Community 2 - "rtlscan.py"
Cohesion: 0.08
Nodes (50): Assign, assignments(), blank_for_headers(), dead_comparisons(), Decl, declarations(), deep_expressions(), expr_depth() (+42 more)

### Community 3 - "astra.py"
Cohesion: 0.14
Nodes (51): base_env(), build_parser(), _c(), _clock_deltas(), cmd_doctor(), cmd_list(), cmd_localise(), cmd_opt() (+43 more)

### Community 4 - "ClockSet"
Cohesion: 0.06
Nodes (32): Clock, ClockError, ClockSet, from_metrics(), _is_comment(), Any, ValueError, Resolve every clock's period, following generated-from chains. A generated… (+24 more)

### Community 5 - "skills.py"
Cohesion: 0.09
Nodes (30): classify(), confidence(), is_verdict(), main(), make_id(), mean_advantage(), _now(), overlap() (+22 more)

### Community 6 - "portfolio.py"
Cohesion: 0.10
Nodes (20): PathTarget, build_parser(), call_budget(), main(), MergeAgent, PathSpecialistAgent, PortfolioOrchestrator, Any (+12 more)

### Community 7 - "rtl_map.py"
Cohesion: 0.09
Nodes (34): analyse(), base_ident(), diagnose(), _family(), from_run(), is_mangled(), load_netlist_index(), main() (+26 more)

### Community 8 - "score.py"
Cohesion: 0.09
Nodes (38): advantages(), critical_period(), cycles_available(), group_stats(), norm_area(), norm_timing(), normalize(), Any (+30 more)

### Community 9 - "merge.py"
Cohesion: 0.11
Nodes (27): _apply(), components(), conflict_report(), Hunk, hunks(), main(), mechanical_union(), normalise() (+19 more)

### Community 10 - "parse_sta.py"
Cohesion: 0.13
Nodes (28): clock_groups(), _coerce(), _group_agreement(), _header_columns(), _labeled_value(), _logic_depth(), main(), _nest() (+20 more)

### Community 11 - "ASTRA — shared VLSI container"
Cohesion: 0.18
Nodes (11): Adding a design, ASTRA — shared VLSI container, Every command, Layout, More than one clock, The example designs, Timing advisor (optional), Use (+3 more)

### Community 12 - "skillgen.py"
Cohesion: 0.09
Nodes (24): RuntimeError, call(), LLMError, Any, Thin wrapper around the `claude` CLI, shared by the agent implementations.…, Run one prompt and return the model's text. Retries only on transport-level…, usage(), ensure_seeds() (+16 more)

### Community 13 - "._cs"
Cohesion: 0.07
Nodes (19): SDC generation. One substitution cannot express five clocks., The three shipped designs use it and must render unchanged., Without this every crossing is analysed as a setup path between clocks with no…, One clock cannot be asynchronous to itself, and emitting the command anyway…, A master clock arrives on a port; a generated clock's source is an internal…, SDC is line-oriented and the clock block is many lines, so expanding it inside…, Only lines that *start* with # are comments; a trailing comment must not stop…, Left in the file it reaches OpenSTA as a syntax error that says nothing about… (+11 more)

### Community 14 - "sec.py"
Cohesion: 0.14
Nodes (23): check(), check_eqy(), check_yosys(), _fail(), main(), Any, Path, Sequential equivalence checking -- the SEC_i constraint of Eq. 4. The paper… (+15 more)

### Community 15 - "TestRtlMap"
Cohesion: 0.08
Nodes (5): The regex fallback has to carry the case where src is gone., `wire p0 = a0_q * b0_q;` declares p0, not a0_q., One stage's delay must land on one region, not on every mention., `bus_A0` must resolve to itself, not be truncated to `bus`., TestRtlMap

### Community 16 - "TestClockSet"
Cohesion: 0.09
Nodes (9): The data model. A design used to carry one clock; every consumer divided by it., Every design shipped before multi-clock support uses `clock`. Dropping that…, A divider states a ratio, not a period. Making the author restate the product…, A ripple divider generates each stage from the one above it, so the source of a…, A divided clock is synchronous to what it divides. Only independent masters are…, Yosys maps in one pass and takes one delay target, so it must be the one that…, --period on a multi-clock design is ambiguous. Collapsing every clock onto one…, A reader predating multi-clock support reads `clock` and must not get None. (+1 more)

### Community 17 - "Multi-clock support"
Cohesion: 0.18
Nodes (11): 1. `abc -D` targets the tightest period, 2. Eq. 3 needs one scalar, so it uses cycles, Multi-clock support, Per-clock-group WNS/TNS, SDC, The model, Two deliberate losses of information, What is not done (+3 more)

### Community 18 - "TestSkillDoc"
Cohesion: 0.10
Nodes (6): Statistics live in the library, where they are measured. A skill document…, It runs before synthesis, so a timing report does not exist yet., Filtering must never silently empty a hand-written document., A seed is an untested suggestion and must stay one until a run gives it a…, The catalogue is an index. Statistics belong in the library, where they were…, TestSkillDoc

### Community 19 - "selftest.py"
Cohesion: 0.12
Nodes (6): Self-tests for the Dr. RTL layer: the paper's equations, the RTL mapper, the…, TestEvaluationFailures, TestExploration, TestRtlScanDepth, TestSkillLearning, TestToolEnv

### Community 20 - "_NoRtl"
Cohesion: 0.23
Nodes (6): _empty_index(), _NoRtl, Regression guard on the bug this replaced: criticality used to be hard-zeroed…, The other side of it: a cone that is nowhere near limiting the clock is not…, A register bank produces many paths and one bottleneck., TestPathSelClustering

### Community 21 - "HANDOFF — ASTRA path-portfolio addon"
Cohesion: 0.12
Nodes (15): 1. What this repo is, 2. Files, 3. Measured results, 4. Defects already found and fixed, 5. Objectives status, 6.1 Multi-clock support — DONE, 6.2 The benchmark design — BUILT (`designs/soc_bench/`), 6.3 The equivalence contract — DECIDED (+7 more)

### Community 22 - "TestEq3"
Cohesion: 0.12
Nodes (6): The published weights and the published normalisation., While the baseline is negative, our form IS (x_i - x_b)/x_b., A flat 0.5 at area_norm > 0.10, and nothing at or below it., The paper's stated purpose: discourage excessive area overhead., Outside the paper's regime the sign must not invert., TestEq3

### Community 23 - "astra_advise.py"
Cohesion: 0.30
Nodes (14): build_prompt(), call_claude(), die(), endpoint_summary(), find_run(), info(), load_json(), main() (+6 more)

### Community 24 - "TestClockResolution"
Cohesion: 0.29
Nodes (3): Resolving a path group to a period. This is the piece HANDOFF section 6.1 calls…, The failure mode being guarded: falling back to the primary period is sometimes…, TestClockResolution

### Community 26 - "TestEquivalenceContract"
Cohesion: 0.18
Nodes (8): docs/equivalence-contract.md, enforced. `sat` models a flop as "Q at t+1 is D…, The behaviour that replaced declining: it is checked, not refused, and the Tcl…, The trap this guards. Capping the depth so multi-clock proofs finish makes them…, Adequate depth is a property of the design, not a global default: on dual_clock…, eqy partitions against a common clock and has no multiclock mode, so asking it…, Wherever `unsupported` is still returned it must not promote a candidate, and…, Induction does not converge with free clocks, so every multi-clock verdict is…, TestEquivalenceContract

### Community 27 - "TestPathSelCriticality"
Cohesion: 0.33
Nodes (3): P(this path is the one limiting the clock). Static timing analysis is…, The case that motivates the whole distribution: minimising delay against a…, TestPathSelCriticality

### Community 28 - "RTL timing optimisation"
Cohesion: 0.17
Nodes (11): Arguing equivalence, Choosing among them, Coding patterns that block the tool's own datapath optimisation, How to read a critical path, RTL timing optimisation, The invariants, Transformation catalogue, What reliably works (+3 more)

### Community 29 - "TestMerge"
Cohesion: 0.23
Nodes (3): A model that re-emits the file with trailing spaces has not edited it, and must…, Two agents inserting at the same point conflict, even though neither replaced…, TestMerge

### Community 30 - "TestRtlScan"
Cohesion: 0.27
Nodes (3): `{{(ACC-2*W){p[31]}}, p}` is a sign extension, not a multiply., mac_chain declares three bottlenecks; alu32 declares none., TestRtlScan

### Community 31 - "TestPortfolioBudget"
Cohesion: 0.26
Nodes (4): A four-iteration ceiling is only reachable if patience allows it: at patience 2…, The addon must be runnable on the same model as the base loop, or a comparison…, 20 paths is one per endpoint, which truncates the TNS denominator on anything…, TestPortfolioBudget

### Community 33 - "TestPathSelSegments"
Cohesion: 0.31
Nodes (4): When a design has one cone, its path is cut into disjoint spans., Within a cell of the transition. The objective trades purity against which span…, _stage(), TestPathSelSegments

### Community 34 - "TestEq5"
Cohesion: 0.29
Nodes (3): Scores are minimised, so the winner is the one below the mean., A z-score must not care that one design's slacks are 10x another's., TestEq5

### Community 35 - "_path"
Cohesion: 0.15
Nodes (13): _path(), The trap: paths normalised against a period that is not their own. Nothing…, Half a nanosecond short of a 2 ns cycle is a quarter of the budget. Half a…, Pinning the bug this replaced: with one period both paths score identically, so…, Back-compat: every single-clock caller passes a float and must get exactly the…, Raw slack cannot order paths in different domains. Here the slow path has the…, The uncertainty model must survive the change of units: two paths a few…, A path ranked against a period that is not its own is the exact silent failure.… (+5 more)

### Community 36 - "TestClockGroupParsing"
Cohesion: 0.22
Nodes (5): Per-group slack, from the Tcl side or derived from the report., The parser has always captured Path Group per path and nothing has ever read…, The unit trap. `get_property <path> slack` is in library units, unlike…, The worst slack over all groups IS the design's worst slack. They come from…, TestClockGroupParsing

### Community 39 - "TestInconclusiveSec"
Cohesion: 0.25
Nodes (3): A solver timeout is not evidence that a rewrite is wrong. Regression test for a…, Eq. 4 is unchanged: undecided still means not promotable., TestInconclusiveSec

### Community 40 - "TestPathSelOnRealRun"
Cohesion: 0.29
Nodes (3): The honesty guard, pinned to a committed artifact. If a weight tweak ever makes…, The design declares three bottlenecks by hand in config.json. The selector…, TestPathSelOnRealRun

### Community 42 - "protect.py"
Cohesion: 0.13
Nodes (20): _assigns_protected(), check_files(), describe(), matches(), _normalise(), protected_lines(), Any, Every mention of a protected signal, in order, with its bit select. The select… (+12 more)

### Community 51 - "TestProtectedRegions"
Cohesion: 0.08
Nodes (12): CDC and clock generation sit on the far side of the cut the equivalence proof…, The whole point is to permit the optimisation, not to freeze the file., Two flops to one. Passes SEC, and is broken silicon., A rule that fires on whitespace teaches the loop to avoid the file entirely,…, An easy way past a per-file check., Every existing single-clock design declares no protected patterns and must…, A protected list that matches nothing is worse than none -- it reads as…, The defect this replaced. On netproc all three specialists were rejected for… (+4 more)

### Community 52 - "The equivalence contract"
Cohesion: 0.18
Nodes (11): 1. The claim, on a single-clock design, 2. The claim, on a multi-clock design, 3. What the code does today, 4. Scale, 5. Related constraints, A bounded pass is worth exactly what its depth can see, Adopted: whole-design equivalence with every clock free, Considered: per-domain SEC with the CDC boundaries cut (+3 more)

### Community 53 - "Path-portfolio mode"
Cohesion: 0.22
Nodes (9): A pre-synthesis pass, Cost, For a head-to-head, Path-portfolio mode, The skill document, The three changes, What this does not yet show, Which path limits the clock (+1 more)

### Community 54 - "Dr. RTL optimisation loop"
Cohesion: 0.40
Nodes (5): Dr. RTL optimisation loop, Honest differences from the paper, The equations, The skill library, Where it runs

### Community 55 - "Setup"
Cohesion: 0.50
Nodes (4): Linux, macOS, Setup, Windows (WSL2) — x86

### Community 59 - "TestPathSelValue"
Cohesion: 0.18
Nodes (3): Both halves matter. A met design must be rankable (the old gate zeroed it), and…, alu32's real numbers: WNS +0.3701 at a 2.5 ns period. Under the old gate this…, TestPathSelValue

## Knowledge Gaps
- **68 isolated node(s):** `alu32`, `dual_clock`, `dual_path`, `mac_chain`, `netproc` (+63 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 496 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **15 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `TestRtlScan` connect `TestRtlScan` to `selftest.py`?**
  _High betweenness centrality (0.045) - this node is a cross-community bridge._
- **Why does `_path()` connect `_path` to `TestPathSelSegments`, `TestClockGroupParsing`, `._setup`, `TestPathSelValue`, `TestStageParameterisation`, `TestPathSelMetrics`, `selftest.py`, `_NoRtl`, `TestPathSelCriticality`?**
  _High betweenness centrality (0.040) - this node is a cross-community bridge._
- **Why does `TestEq3` connect `TestEq3` to `selftest.py`?**
  _High betweenness centrality (0.040) - this node is a cross-community bridge._
- **What connects `alu32`, `dual_clock`, `dual_path` to the rest of the system?**
  _68 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `pathsel.py` be split into smaller, more focused modules?**
  _Cohesion score 0.06321334503950835 - nodes in this community are weakly interconnected._
- **Should `drrtl.py` be split into smaller, more focused modules?**
  _Cohesion score 0.08686868686868687 - nodes in this community are weakly interconnected._
- **Should `rtlscan.py` be split into smaller, more focused modules?**
  _Cohesion score 0.08392156862745098 - nodes in this community are weakly interconnected._