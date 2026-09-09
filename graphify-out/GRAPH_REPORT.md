# Graph Report - astra  (2026-09-10)

## Corpus Check
- 33 files · ~73,351 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 996 nodes · 1752 edges · 51 communities (39 shown, 12 thin omitted)
- Extraction: 99% EXTRACTED · 1% INFERRED · 0% AMBIGUOUS · INFERRED: 21 edges (avg confidence: 0.85)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `ad76a160`
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
- TestClockSdc
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
- ._feats
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
- TestPathSelValue
- TestClockGroupParsing
- TestReplyParsing
- _path
- TestInconclusiveSec
- TestPathSelOnRealRun
- TestEq4
- TestClockResolution
- TestStageParameterisation
- TestPortfolioAgents
- TestPathSelMetrics
- alu32.v
- dual_path.v
- mac_chain.v
- soc_bench.v
- entrypoint.sh

## God Nodes (most connected - your core abstractions)
1. `_path()` - 27 edges
2. `ClockSet` - 25 edges
3. `TestRtlMap` - 22 edges
4. `build_parser()` - 18 edges
5. `SkillLibrary` - 18 edges
6. `RtlIndex` - 16 edges
7. `scan()` - 16 edges
8. `TestClockSet` - 15 edges
9. `TestSkillDoc` - 15 edges
10. `die()` - 14 edges

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

## Communities (51 total, 12 thin omitted)

### Community 0 - "pathsel.py"
Cohesion: 0.06
Nodes (62): Counter, build_targets(), _by_cluster(), _cell_count(), _clock_coverage(), cluster(), cluster_similarity(), cluster_value() (+54 more)

### Community 1 - "drrtl.py"
Cohesion: 0.09
Nodes (27): build_parser(), die(), EvaluationAgent, extract_json(), extract_verilog(), fmt(), info(), main() (+19 more)

### Community 2 - "rtlscan.py"
Cohesion: 0.08
Nodes (50): Assign, assignments(), blank_for_headers(), dead_comparisons(), Decl, declarations(), deep_expressions(), expr_depth() (+42 more)

### Community 3 - "astra.py"
Cohesion: 0.15
Nodes (49): base_env(), build_parser(), _c(), cmd_doctor(), cmd_list(), cmd_localise(), cmd_opt(), cmd_paths() (+41 more)

### Community 4 - "ClockSet"
Cohesion: 0.06
Nodes (30): Clock, ClockError, ClockSet, from_metrics(), _is_comment(), Any, ValueError, Resolve every clock's period, following generated-from chains. A generated… (+22 more)

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
Cohesion: 0.10
Nodes (32): advantages(), critical_period(), group_stats(), norm_area(), norm_timing(), normalize(), Any, ValueError (+24 more)

### Community 9 - "merge.py"
Cohesion: 0.11
Nodes (27): _apply(), components(), conflict_report(), Hunk, hunks(), main(), mechanical_union(), normalise() (+19 more)

### Community 10 - "parse_sta.py"
Cohesion: 0.13
Nodes (28): clock_groups(), _coerce(), _group_agreement(), _header_columns(), _labeled_value(), _logic_depth(), main(), _nest() (+20 more)

### Community 11 - "ASTRA — shared VLSI container"
Cohesion: 0.07
Nodes (27): A pre-synthesis pass, Adding a design, ASTRA — shared VLSI container, Cost, Dr. RTL optimisation loop, For a head-to-head, Honest differences from the paper, Layout (+19 more)

### Community 12 - "skillgen.py"
Cohesion: 0.09
Nodes (24): RuntimeError, call(), LLMError, Any, Thin wrapper around the `claude` CLI, shared by the agent implementations.…, Run one prompt and return the model's text. Retries only on transport-level…, usage(), ensure_seeds() (+16 more)

### Community 13 - "TestClockSdc"
Cohesion: 0.10
Nodes (12): SDC generation. One substitution cannot express five clocks., The three shipped designs use it and must render unchanged., Without this every crossing is analysed as a setup path between clocks with no…, One clock cannot be asynchronous to itself, and emitting the command anyway…, A master clock arrives on a port; a generated clock's source is an internal…, SDC is line-oriented and the clock block is many lines, so expanding it inside…, Only lines that *start* with # are comments; a trailing comment must not stop…, Left in the file it reaches OpenSTA as a syntax error that says nothing about… (+4 more)

### Community 14 - "sec.py"
Cohesion: 0.15
Nodes (22): check(), check_eqy(), check_yosys(), _fail(), main(), Any, Path, A check that was not run because it would not have meant anything. Distinct… (+14 more)

### Community 15 - "TestRtlMap"
Cohesion: 0.08
Nodes (5): The regex fallback has to carry the case where src is gone., `wire p0 = a0_q * b0_q;` declares p0, not a0_q., One stage's delay must land on one region, not on every mention., `bus_A0` must resolve to itself, not be truncated to `bus`., TestRtlMap

### Community 16 - "TestClockSet"
Cohesion: 0.09
Nodes (9): The data model. A design used to carry one clock; every consumer divided by it., Every design shipped before multi-clock support uses `clock`. Dropping that…, A divider states a ratio, not a period. Making the author restate the product…, A ripple divider generates each stage from the one above it, so the source of a…, A divided clock is synchronous to what it divides. Only independent masters are…, Yosys maps in one pass and takes one delay target, so it must be the one that…, --period on a multi-clock design is ambiguous. Collapsing every clock onto one…, A reader predating multi-clock support reads `clock` and must not get None. (+1 more)

### Community 17 - "Multi-clock support"
Cohesion: 0.10
Nodes (19): 1. The claim, on a single-clock design, 2. The claim, on a multi-clock design, 3. What the code does today, 4. Scale, 5. Related constraints, Adopted: per-domain SEC with the CDC boundaries cut, Rejected: bounded equivalence with `set_clock_groups -asynchronous`, The equivalence contract (+11 more)

### Community 18 - "TestSkillDoc"
Cohesion: 0.10
Nodes (6): Statistics live in the library, where they are measured. A skill document…, It runs before synthesis, so a timing report does not exist yet., Filtering must never silently empty a hand-written document., A seed is an untested suggestion and must stay one until a run gives it a…, The catalogue is an index. Statistics belong in the library, where they were…, TestSkillDoc

### Community 19 - "selftest.py"
Cohesion: 0.12
Nodes (6): Self-tests for the Dr. RTL layer: the paper's equations, the RTL mapper, the…, TestEvaluationFailures, TestExploration, TestRtlScanDepth, TestSkillLearning, TestToolEnv

### Community 20 - "_NoRtl"
Cohesion: 0.22
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

### Community 24 - "._feats"
Cohesion: 0.23
Nodes (7): The trap: paths normalised against a period that is not their own. Nothing…, Half a nanosecond short of a 2 ns cycle is a quarter of the budget. Half a…, Pinning the bug this replaced: with one period both paths score identically, so…, Back-compat: every single-clock caller passes a float and must get exactly the…, sigma is a fraction of a period, so 5% of 10 ns is five times the absolute slop…, A path ranked against a period that is not its own is the exact silent failure.…, TestMultiClockRanking

### Community 26 - "TestEquivalenceContract"
Cohesion: 0.27
Nodes (6): docs/equivalence-contract.md, enforced. Both SEC engines build a miter over one…, It must never promote a candidate through Eq. 4., The distinction that keeps the skill library honest: recording 'this…, The guard must not disturb the case that already works., Declining on clocks must not mask an ordinary failure., TestEquivalenceContract

### Community 27 - "TestPathSelCriticality"
Cohesion: 0.23
Nodes (4): P(this path is the one limiting the clock). Static timing analysis is…, Without the shared-cone term, twenty bit-slices of one bottleneck would each…, The case that motivates the whole distribution: minimising delay against a…, TestPathSelCriticality

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

### Community 35 - "TestPathSelValue"
Cohesion: 0.20
Nodes (3): Both halves matter. A met design must be rankable (the old gate zeroed it), and…, alu32's real numbers: WNS +0.3701 at a 2.5 ns period. Under the old gate this…, TestPathSelValue

### Community 36 - "TestClockGroupParsing"
Cohesion: 0.22
Nodes (5): Per-group slack, from the Tcl side or derived from the report., The parser has always captured Path Group per path and nothing has ever read…, The unit trap. `get_property <path> slack` is in library units, unlike…, The worst slack over all groups IS the design's worst slack. They come from…, TestClockGroupParsing

### Community 38 - "_path"
Cohesion: 0.43
Nodes (4): _path(), One cone must yield one cone-target, never three padded ones., A synthetic OpenSTA path, in the shape parse_sta produces. Paths through one…, TestPathSelMMR

### Community 39 - "TestInconclusiveSec"
Cohesion: 0.25
Nodes (3): A solver timeout is not evidence that a rewrite is wrong. Regression test for a…, Eq. 4 is unchanged: undecided still means not promotable., TestInconclusiveSec

### Community 40 - "TestPathSelOnRealRun"
Cohesion: 0.29
Nodes (3): The honesty guard, pinned to a committed artifact. If a weight tweak ever makes…, The design declares three bottlenecks by hand in config.json. The selector…, TestPathSelOnRealRun

### Community 42 - "TestClockResolution"
Cohesion: 0.29
Nodes (3): Resolving a path group to a period. This is the piece HANDOFF section 6.1 calls…, The failure mode being guarded: falling back to the primary period is sometimes…, TestClockResolution

## Knowledge Gaps
- **64 isolated node(s):** `alu32`, `dual_path`, `mac_chain`, `soc_bench`, `entrypoint.sh script` (+59 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 450 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **12 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `TestRtlMap` connect `TestRtlMap` to `selftest.py`?**
  _High betweenness centrality (0.045) - this node is a cross-community bridge._
- **Why does `TestClockSet` connect `TestClockSet` to `selftest.py`?**
  _High betweenness centrality (0.038) - this node is a cross-community bridge._
- **Why does `ClockSet` connect `ClockSet` to `sec.py`?**
  _High betweenness centrality (0.036) - this node is a cross-community bridge._
- **What connects `alu32`, `dual_path`, `mac_chain` to the rest of the system?**
  _64 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `pathsel.py` be split into smaller, more focused modules?**
  _Cohesion score 0.06321334503950835 - nodes in this community are weakly interconnected._
- **Should `drrtl.py` be split into smaller, more focused modules?**
  _Cohesion score 0.09071117561683599 - nodes in this community are weakly interconnected._
- **Should `rtlscan.py` be split into smaller, more focused modules?**
  _Cohesion score 0.08392156862745098 - nodes in this community are weakly interconnected._