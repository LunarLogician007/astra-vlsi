# ASTRA guide

The full manual: every command, how both optimisation loops work, costs, and how
to add a design. For setup, start with the [README](../README.md). For
development history and known pitfalls, see [HANDOFF.md](HANDOFF.md).

## Commands

Run inside the container (`make shell`) as `astra ...`, or from the host with
the `make` target. `DESIGN=` defaults to `mac_chain`.

**Flow (no model calls)**

| command | make | what it does |
|---|---|---|
| `astra doctor` | `make doctor` | check tools, PDKs and that every design resolves |
| `astra list` | `make list` | designs, their clocks, and past runs |
| `astra syn <d>` | `make syn` | Yosys synthesis only |
| `astra sta <d>` | — | OpenSTA timing on the last synthesis |
| `astra run <d>` | `make run` | synthesis + timing (`--period 1.8` to change the clock) |
| `astra run <d> --pnr` | `make pnr` | + place & route (needs the ORFS image) |
| `astra report <d>` | — | print `metrics.json`; `--timing` for the report |
| `astra localise <d>` | `make localise` | critical path → RTL lines + root causes |
| `astra score <d>` | `make score` | score a run against a baseline run |
| `astra sec <top>` | `make sec` | sequential equivalence between two designs |
| `astra scan <d>` | `make scan` | slow RTL constructs, before synthesis |
| `astra paths <d>` | `make paths` | the distinct bottlenecks worth an agent |
| `astra skills` | `make skills` | the learned skill library |
| `astra skilldoc` | `make skilldoc` | rebuild the skill document |

**Loops (call Claude)**

| command | make | what it does |
|---|---|---|
| `astra opt <d>` | `make opt` | Dr. RTL: analyse → N rewrites → verify → keep best |
| `astra portfolio <d>` | `make portfolio` | k scoped specialists, then combine |

Both accept `--model`, `--iters` and `--dry-run` (writes prompts, skips model
calls, but still needs the container to evaluate the baseline). Pass them via
`OPT_ARGS="..."` / `PF_ARGS="..."`.

**Tests:** `python3 tools/selftest.py` runs 255 tests with no EDA tools, model
or network.

## Where things run

The EDA tools live in the container; `claude` and its login live on your host.
`make opt`, `make portfolio` and `make advise` run on the host and send each
tool call into the image (see `tools/toolenv.py`). The repo is mounted into the
container, so both sides read and write the same `runs/` folder.

The loops call `claude -p`, which draws from your Claude plan limits rather than
billing per token. For CI or large sweeps, use an API key instead.

## Timing advisor

`make advise DESIGN=x` sends a finished run (metrics, parsed critical paths, RTL,
SDC, and the `notes` in `config.json`) to Claude and writes `advice.md` into the
run folder. One call per run; `ADVISE_ARGS=--force` re-advises,
`ADVISE_ARGS=--dry-run` prints the prompt only.

The advisor does not edit RTL and its claims are unverified. Treat it as a
reviewer, not an oracle.

## Dr. RTL loop (`make opt`)

Reimplements *Dr. RTL: Autonomous Agentic RTL Optimization through
Tool-Grounded Self-Improvement* (Fang et al., 2026). Each iteration uses four
strictly separated roles:

| role | does | never |
|---|---|---|
| Timing analysis | maps the top paths to RTL lines, names root causes | proposes fixes |
| RTL optimisation | writes N rewrites in parallel, guided by the skill library | runs tools |
| Evaluation | Yosys, OpenSTA, equivalence check | interprets results |
| Skill learning | compares the group, extracts pattern→strategy pairs | sees one candidate alone |

```bash
make opt DESIGN=mac_chain OPT_ARGS="-n 4 --iters 3"
```

Cost: N candidates × iterations, plus one analysis call per iteration. The
default `-n 4 --iters 3` is up to 15 calls.

### Scoring

Each candidate gets a score (Eq. 3 in the paper); lower is better:

```
Score = 0.50·WNS_norm + 0.35·TNS_norm + 0.15·Area_norm + penalty
penalty = 0.5 if area grows more than 10%, else 0
```

Values are normalised against the starting design. The best-scoring candidate
**that passes equivalence** is kept (Eq. 4). Candidates are also ranked
against each other (Eq. 5, a z-score), and that relative signal is what the
skill library learns from.

One deviation from the paper: timing terms use `−(x−x_base)/|x_base|`, which
equals the paper's formula whenever the baseline is negative, but keeps the
right sign once a design meets timing.

### Skill library

`skills/library.json` persists across runs and designs. Each entry is a
pattern→strategy pair with occurrence count, equivalence pass count and mean
relative advantage, combined into:

```
confidence = sec_pass_rate × logistic(−mean_advantage) × n/(n+3)
```

A transformation that breaks equivalence scores zero however fast it was, and a
single lucky trial stays provisional. Entries that usually fail are marked
`invalid` and shown to agents as known-bad. Seed entries start at zero
confidence and must earn it. `make clean` keeps the library;
`make clean-skills` resets it.

## Path-portfolio loop (`make portfolio`)

An extension built on the Dr. RTL loop. It exists because in plain `make opt`
all N candidates attack the same path, and each rewrites the whole file, so two
good partial fixes can never be combined.

```bash
make portfolio DESIGN=mac_chain PF_ARGS="--model haiku --iters 1"
make portfolio DESIGN=mac_chain PF_ARGS="-k 2 --iters 1"
```

Each iteration:

1. **Pre-synthesis cleanup** (first iteration only). `tools/rtlscan.py` flags
   slow constructs in the source (serial chains, wide operators, compares that
   can never fire); one agent fixes them. Kept only if it measurably beats the
   original. `--clean 0` skips it, but that tends to make later agents converge
   on the same fix.
2. **Pick distinct targets.** `tools/pathsel.py` clusters critical paths into
   logic cones and picks up to `k` that are both important and different from
   each other. Each cone is scored on *impact* (how likely it is to hold the
   clock-limiting path), *severity* (how close it is to the constraint) and
   *tractability* (whether it maps cleanly to RTL). If there are fewer cones
   than agents, the worst path is cut into segments where its cell mix changes.
3. **One agent per target.** Each is told to change only its target's logic.
4. **Combine.** Every subset of successful fixes is spliced together and
   evaluated at no model cost; a merge agent handles overlaps. The best of
   specialists, unions and the parent wins, so a union has to earn it.

On `mac_chain`, step 2 recovers the three hand-documented bottlenecks from the
timing report alone:

| target | share of delay | cell signature | actual bottleneck |
|---|---|---|---|
| T1 | 36% | AND/OR, fanout 42 | four 16×16 multipliers |
| T2 | 28% | AOI/OAI, XOR | serial adder chain |
| T3 | 36% | AOI/OAI | 40-bit saturation compare |

Check it yourself, with no tools or model:

```bash
python3 tools/pathsel.py results/mac_chain/opt-20260915-103411/baseline -k 3
```

### Which path limits the clock

Post-synthesis timing has no wire delay, so paths a few picoseconds apart are
not reliably ordered yet. `pathsel` adds sampled noise to each path's delay
(`--delay-sigma`, default 5% of the period, correlated within a cone by
`--cone-rho`, default 0.7) and reports how often each cone ends up worst. This
works the same on designs that already meet timing. `--delay-sigma 0` gives the
plain deterministic ranking.

### Skill document

`.claude/skills/rtl-timing-optimization/SKILL.md` is generated from the skill
library and pasted into every agent's prompt (agents have no tools, so they
cannot load it themselves). `make skilldoc SKILLDOC_ARGS=--llm` adds one
research call that may use web sources; results enter the library as
zero-confidence seeds.

### Cost

Per iteration: `k` specialists + 1 merge + 1 skill learning = **k + 2** calls
(path selection and mechanical unions are free). Defaults
(`-k 3 --iters 4`) plan up to 21 calls; `--patience 3` stops a stalled run
early and `--max-calls N` refuses to start over budget. In every recorded run
the best result came from iteration 1.

### Comparing the two loops fairly

```bash
make clean-skills
make opt       DESIGN=mac_chain OPT_ARGS="--model haiku --iters 3"
make clean-skills
make portfolio DESIGN=mac_chain PF_ARGS="--model haiku --iters 3"
```

Reset the library between runs and set `--iters` explicitly: the defaults
differ (3 vs 4).

## Equivalence checking

Every candidate must be proven to behave like the original before it can win.

- **Single clock:** `eqy` if installed, otherwise a Yosys miter proved by
  induction, falling back to a bounded check of `--sec-depth` cycles.
- **Multiple clocks:** registers are first paired by name and compared
  structurally (*register correspondence*); only edited logic reaches a solver,
  so a 50K-cell design proves in seconds. Anything left over goes to a bounded
  whole-design check. `--sec-regcorr-timeout` and `--sec-fallback-timeout`
  limit each stage.

Results record `method: regcorr`, `induction` or `bounded`. A bounded pass is
not a full proof, so check that field. Details:
[`equivalence-contract.md`](equivalence-contract.md).

To test the checker itself without a model, run it against the known-good and
broken cases in `benchmarks/sec_cases/`:

```bash
python3 tools/secbench.py netproc --case bug:fail:benchmarks/sec_cases/netproc/m1_parity_xnor.v
```

It exits non-zero if a broken case is reported equivalent.

## Adding a design

```bash
mkdir -p designs/mydesign/{rtl,constraints}
```

`designs/mydesign/config.json`:

```json
{
  "top": "mydesign",
  "rtl": ["rtl/mydesign.v"],
  "sdc": "constraints/mydesign.sdc",
  "pdk": "nangate45",
  "clock": { "name": "clk", "port": "clk", "period_ns": 2.0 }
}
```

Start the SDC from `designs/alu32/constraints/alu32.sdc`. Write `@CLK_PERIOD@`
where the period goes, so `--period` works without editing files. Then run
`astra doctor`.

### Multiple clocks

Replace `clock` with a `clocks` list:

```json
"clocks": [
  { "name": "clk_sys", "port": "clk_sys", "period_ns": 2.0 },
  { "name": "clk_io",  "port": "clk_io",  "period_ns": 8.0 },
  { "name": "clk_sys_div2", "generated_from": "clk_sys", "divide_by": 2,
    "port": "clk_sys_div2_r_DFFR_X1_Q/Q" }
]
```

A generated clock gives a ratio instead of a period; its `port` is the
post-synthesis pin of its divider flop. SDC placeholders:

| placeholder | expands to |
|---|---|
| `@CLK_PERIOD@` | the primary clock's period |
| `@CLK_PERIOD:<name>@` | a named clock's period |
| `@ASTRA_CLOCK_DEFS@` | all `create_clock` / `create_generated_clock` lines, plus async clock groups |

Protect logic the optimiser must not touch (clock-domain crossings, clock
dividers):

```json
"protected": ["cdc_*", "clk_*_div*"]
```

Any candidate that edits a line mentioning a matching name is rejected before
synthesis. Each path is timed against the clock that captures it; see
[`multi-clock.md`](multi-clock.md).

OpenSTA pitfall: it cannot parse bus subscripts like `get_ports {foo[*]}`
(fails with a bare `Error: stoi`), so write clock dividers as simple toggle
flops. More pitfalls in [`HANDOFF.md`](HANDOFF.md) §7.

## Example designs

| design | clock(s) | baseline |
|---|---|---|
| `alu32` | 2.5 ns | met, WNS +0.37, 1,318 cells |
| `mac_chain` | 2.0 ns | WNS −0.357, TNS −3.74, 15 failing endpoints |
| `dual_path` | 1.5 ns | not yet run |
| `dual_clock` | 3 clocks, 2 async | WNS −0.052, TNS −0.68, 1,307 cells |
| `soc_bench` | 13 clocks, 5 async | WNS −6.51, TNS −65.1, 49,935 cells |
| `netproc` | 13 clocks, 5 async | WNS −17.83, TNS −764, 50,502 cells |
| `vending_machine` | 5.0 ns | WNS −8.03, TNS −4,416, 14,293 cells |

- **`alu32`** — sanity check; a 32-bit adder that meets timing.
- **`mac_chain`** — 4-tap 16×16 multiply-accumulate with a serial adder chain
  and saturation compare. Violates on purpose.
- **`dual_path`** — two unrelated slow datapaths, meant to exercise picking
  several cones at once (not yet demonstrated).
- **`dual_clock`** — small multi-clock design, no multipliers, so equivalence
  proofs close quickly.
- **`soc_bench`** — scale test: five async domains, generated clocks, CDC
  synchronisers, ~50K cells. Synthesis takes ~2 min per candidate.
- **`netproc`** — like `soc_bench` without multipliers.
- **`vending_machine`** — from `benchmarks/rtl_dataset/`; an FSM whose output
  computes two 1,024-bit sums and muxes them.

## Known gaps

- Yosys is weaker than commercial synthesis, so gains are smaller claims than
  the paper's.
- Place-and-route timing is not used by the loops yet.
- Picking several independent cones has only fired once (on `netproc`, where one
  cone still dominated).
- Path segmentation is validated on `mac_chain` only.

## Build variants

```bash
make build PDKS="nangate45 sky130hd"          # add another PDK
make build PLATFORM=linux/amd64               # cross-build for x86
make build DOCKERFILE=docker/Dockerfile.orfs  # + OpenROAD place & route (20 GB+, amd64 only)
make build EQY=0                              # skip building eqy
```
