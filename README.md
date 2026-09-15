# ASTRA — shared VLSI container

One Docker image with **Yosys + OpenSTA + nangate45**. Point it at a design,
get a netlist and a timing report. Nobody on the team installs EDA tools
locally.

```
RTL + SDC  ──►  Yosys  ──►  netlist  ──►  OpenSTA  ──►  timing report
                                                        (WNS / TNS / paths)
```

On top of that flow, `make opt` runs the [Dr. RTL](#dr-rtl-optimisation-loop)
closed loop: analyse the critical path, write N rewrites in parallel, verify
each for equivalence, keep the best, and learn from the comparison.
`make portfolio` runs the [path-portfolio](#path-portfolio-mode) addon, which
splits the design into *k* disjoint bottlenecks and scopes one agent to each.

Designs may declare [more than one clock](#more-than-one-clock), including
generated and divided ones. Every path is then ranked against the period of the
clock that actually captured it.

Image is **1.1 GB** and builds natively on arm64 and x86 — no emulation.

---

## Setup

### Windows (WSL2) — x86

Run everything **inside the WSL shell**, not PowerShell.

```bash
# 1. Docker: either Docker Desktop with the WSL2 backend enabled,
#    or install the engine directly inside WSL:
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER      # then close and reopen the WSL shell
sudo service docker start          # needed after each WSL restart

sudo apt update && sudo apt install -y make git

# 2. Clone into the WSL filesystem — NOT /mnt/c.
#    Windows drives lose Unix file permissions and are much slower.
cd ~ && git clone <repo-url> astra && cd astra

# 3. Build and check
make build      # ~5-10 min
make doctor
make run DESIGN=mac_chain
```

You're on x86, so this builds natively — no `PLATFORM` flag, no emulation.

If `make build` fails with `bad interpreter: /bin/bash^M`, git rewrote the
files with Windows line endings. `.gitattributes` should prevent it; if it
still happens: `git config --global core.autocrlf false` and re-clone.

### macOS

```bash
brew install colima docker docker-buildx
colima start --cpu 6 --memory 10 --disk 60

make build      # ~5 min
make doctor
```

### Linux

Docker from your package manager, then `make build`.

Everyone builds their own image from this repo — there's no image to download
and nothing to keep in sync beyond the source.

---

## Use

```bash
make shell                        # repo mounted at /work, host edits are live

astra list                        # designs and past runs
astra run mac_chain               # synthesis + timing report
astra run mac_chain --period 3.0  # different clock target
astra report mac_chain --timing   # print the timing report
astra report mac_chain            # print metrics.json
```

Without entering the container: `make run DESIGN=mac_chain`.

Real output:

```
[astra] synthesis ok in 4.05s: 7343 cells, area 8942.9200 um^2
[astra] post-synthesis timing: VIOLATED
    WNS = -0.3573 ns    TNS = -3.7438 ns    violating endpoints = 15
    worst path: _14637_ -> _14515_  (logic depth 86)
```

### Every command

Inside the container (`make shell`), or from the host via the `make` target on
the right. `DESIGN=` defaults to `mac_chain`.

**Flow — no model, no API key.**

| command | make | what it does |
|---|---|---|
| `astra doctor` | `make doctor` | check tools, PDKs and that every design resolves |
| `astra list` | `make list` | designs and past runs, with each design's clocks |
| `astra syn <d>` | `make syn` | Yosys synthesis only |
| `astra sta <d>` | — | OpenSTA timing only, on the last synthesis |
| `astra run <d>` | `make run` | synthesis + timing |
| `astra run <d> --pnr` | `make pnr` | + OpenROAD place & route (needs the ORFS image) |
| `astra report <d>` | — | print `metrics.json`; `--timing` for the report |
| `astra localise <d>` | `make localise` | critical path → RTL lines + structural root causes |
| `astra score <d>` | `make score` | Eq. 3 for one run against a baseline run |
| `astra sec <top>` | `make sec` | sequential equivalence between two designs |
| `astra scan <d>` | `make scan` | pre-synthesis structural smells, lexical — runs with no tools at all |
| `astra paths <d>` | `make paths` | the distinct critical-path targets worth an agent call |
| `astra skills` | `make skills` | inspect the learned skill library |
| `astra skilldoc` | `make skilldoc` | rebuild the RTL timing-optimisation skill document |

**Loops — these call a model and cost tokens.**

| command | make | what it does |
|---|---|---|
| `astra opt <d>` | `make opt` | Dr. RTL: analyse → N rewrites → verify → keep the best |
| `astra portfolio <d>` | `make portfolio` | the addon: k scoped specialists, then a merge |

Both take `--dry-run`, which writes the prompts and skips the model calls — but
**still needs the container**, because it evaluates the baseline for real.

**Verify without any tools or a model:**

```bash
python3 tools/selftest.py         # 225 tests: no EDA install, no model, no network
make selftest                     # the same, inside the container
```

---

## Timing advisor (optional)

`make advise` hands a finished run to Claude and asks what to change to close
timing. It reads the artifacts the run already produced — `metrics.json`, the
parsed `02_sta/timing.json`, the RTL, the SDC, and the `notes` block from
`config.json` — and writes `advice.md` into the run directory.

```bash
make run    DESIGN=mac_chain
make advise DESIGN=mac_chain
```

It runs **on the host, not in the container** — the image has no `claude`
binary and no credentials. The repo is bind-mounted at `/work`, so the host
reads the same run directory the container wrote.

```bash
make advise DESIGN=mac_chain ADVISE_ARGS=--dry-run   # print the prompt, call nothing
make advise DESIGN=mac_chain ADVISE_ARGS=--force     # re-advise an already-advised run
python3 tools/astra_advise.py mac_chain --run 20260824-132334
```

### What it costs

It shells out to `claude -p`, which authenticates with your Claude
subscription rather than an API key — no per-token bill, but the usage draws
from the **same plan limits as your interactive Claude sessions**. Note that
the separate Agent SDK credit announced for June 15 2026 was paused and is not
available; there is nothing to claim.

So the tool is built to be frugal, and those choices are deliberate:

- **One call per finished run.** It is not wired into `make run`, and it
  refuses to re-advise a run that already has `advice.md` unless you pass
  `--force`.
- **The prompt is a digest, not a dump.** A 91-stage OpenSTA path becomes a
  cell-type histogram, the top stages by delay, and the ordered cell walk —
  about 2k tokens instead of 20k. Each invocation also carries ~10k tokens of
  Claude Code harness overhead, which the digest is sized against.
- **No tools, no MCP.** The advisor gets every fact inline and is denied file
  and network tools, so one call stays one call instead of turning into an
  agent that greps the repo.
- **Cost is printed** after each call — notional USD plus token counts — so
  you can see what a run drew from your plan.

If you want this in CI or on a nightly sweep across many designs, put it on an
API key instead. Unattended automation is exactly the workload that drains a
subscription and leaves you rate-limited in your own editor.

### Worth knowing

The advisor is a reviewer, not an oracle — it does not edit RTL, and its
suggestions are unverified until you re-run the flow. Treat `advice.md` as a
starting point and check the claims. On the shipped `mac_chain` it correctly
identified that the saturation compare is dead logic (`|s3| ≤ 2³²` against a
`LIMIT` of `2³⁷−1`, 32× of headroom), which is a real finding sitting in the
critical-path tail — but verify that kind of claim yourself before acting on it.

---

## Dr. RTL optimisation loop

`make opt` runs the closed loop from **Dr. RTL: Autonomous Agentic RTL
Optimization through Tool-Grounded Self-Improvement** (Fang et al., HKUST,
2026) — analyse the critical path, write several rewrites in parallel, verify
each one, keep the best, and learn from the comparison.

```bash
make run DESIGN=mac_chain                        # see the starting point
make opt DESIGN=mac_chain                        # the loop
make opt DESIGN=mac_chain OPT_ARGS="-n 6 --iters 4"
make opt DESIGN=mac_chain OPT_ARGS=--dry-run     # write the prompts, call nothing
```

The paper's loop is Eq. 2 — `D_t → {D_t^(i)}_{i=1..N} → D_{t+1}` — run by an
orchestrator over four roles that are kept strictly apart:

| role | does | does not |
|---|---|---|
| **Timing Analysis** (§4.1.2) | localises the top-*k* paths back to RTL lines, names root causes | propose fixes |
| **RTL Optimization** (§4.1.3) | writes *N* candidate rewrites in parallel, guided by the skill library | run any tool |
| **Evaluation** (§4.1.4) | Yosys, OpenSTA, equivalence check | read reports or reason |
| **Skill Learning** (§4.2) | compares the group, distils pattern→strategy pairs | see a single candidate alone |

The separation is the point: the agent that decides never runs the tools, and
the agent that runs the tools never interprets them, so no optimisation
decision can rest on a misread report.

### The equations

Every candidate is scored by **Eq. 3**, and lower is better:

```
Score_i = α·WNS^norm + β·TNS^norm + γ·Area^norm + penalty_i
          α=0.50      β=0.35      γ=0.15
          penalty_i = 0.5  when Area^norm > 0.10, else 0
```

**Eq. 4** promotes `argmin Score_i` *subject to* `SEC_i = 1` — a rewrite that
changed the design's behaviour is discarded no matter how much slack it
recovered. **Eq. 5** then z-scores the group, `A_i = (score_i − μ_t)/σ_t`, and
that relative signal — not the raw PPA — is what the skill library learns
from. Absolute slack numbers are noisy and design-specific; "this
transformation beat its siblings under identical conditions" transfers.

Any of it can be run on its own:

```bash
astra localise mac_chain                       # path → RTL lines + root causes
astra score mac_chain --baseline <run-id>      # Eq. 3, with the breakdown
astra sec mac_chain --gold a.v --gate b.v      # the SEC constraint alone
astra skills                                   # the library and its statistics
make selftest                                  # the maths, no container needed
```

### The skill library

`skills/library.json` persists across runs *and across designs* — it is what
makes the loop self-improving rather than merely iterative. Each entry is one
pattern→strategy pair carrying the three statistics the paper names:
occurrence count, SEC-pass count, and mean relative advantage. Those fold into
a confidence:

```
confidence = sec_pass_rate  ×  logistic(−mean A_i)  ×  n/(n+3)
             correctness       benefit                support
```

A product rather than a sum, so each factor can veto on its own: a
transformation that breaks equivalence is worth nothing however fast it was,
and one lucky trial stays provisional. Entries that fail SEC more often than
not are marked `invalid` and are shown to the optimiser *as* known-bad, which
is more useful than hiding them and letting it rediscover the same dead end.

The shipped library holds **12 seed entries** with zero occurrences and zero
confidence. They are textbook transformations offered as untested suggestions
and must earn their statistics from real runs — the paper's own 47 entries
were learned from its runs, and shipping look-alike statistics would be
fabricated evidence. `make clean` keeps the library; `make clean-skills`
resets it.

### Where it runs

`make opt` is the one target needing both halves at once: `claude` and its
credentials on the host, Yosys/OpenSTA/eqy in the container. It runs host-side
and dispatches each tool call into the image (`ASTRA_TOOL_PREFIX`, see
`tools/toolenv.py`). Nothing is copied — the repo is bind-mounted, so both
sides read and write the same `runs/` directory.

Cost scales as *N* candidates × *K* iterations, plus one analysis call per
iteration — so the default `-n 4 --iters 3` is up to 15 model calls, not one.
Everything in the [advisor's cost notes](#what-it-costs) applies with that
multiplier.

### Honest differences from the paper

Same method, weaker instruments, and the gaps are worth stating plainly:

- **Open-source synthesis.** The paper uses commercial synthesis and argues
  the point explicitly: weak synthesis makes trivial rewrites look effective.
  Yosys is weaker, so a slack win here is a smaller claim than the same
  number would be there.
- **Equivalence checking is best-effort.** On a single clock, with `eqy`
  installed you get its partitioned SEC. Without it the fallback is a Yosys
  miter discharged by Yosys's own SAT engine, which tries temporal induction
  first (an unbounded proof) and falls back to a bounded check of
  `--sec-depth` cycles. On several clocks, register correspondence runs first
  (`method: regcorr`, unbounded) and the bounded whole-design check only
  covers what it cannot prove. A bounded pass is recorded as
  `method: bounded`, not silently promoted to a proof — read that field
  before trusting a result.
- **Scale.** The paper evaluates 20 designs averaging 812 lines. This repo
  ships two designs of ~90 lines. The loop is the same; the evidence it
  produces is not comparable.
- **One generalisation of Eq. 3.** The paper's `(x−x_base)/x_base`
  normalisation is only well-behaved while the timing baseline is negative,
  which is its regime. Timing terms here use `−(x−x_base)/|x_base|` —
  algebraically identical whenever `x_base < 0`, and still correct once a
  design closes and the sign would otherwise invert. `make selftest` asserts
  the equivalence.


---

## Path-portfolio mode

`make portfolio` is an addon to the loop above. It changes three things and
reuses everything else — `make opt` is untouched.

```bash
make skilldoc                                   # once: build the skill document
make portfolio DESIGN=mac_chain
make portfolio DESIGN=mac_chain PF_ARGS="--model haiku"
make portfolio DESIGN=mac_chain PF_ARGS="-k 2 --iters 1 --clean 0"
make portfolio DESIGN=mac_chain PF_ARGS=--dry-run
```

### Why

The base loop's ceiling is visible in its own artifacts. In the run committed
under `runs/mac_chain/opt-20260901-025558-run1`, WNS improved 13.5% and TNS
47.6%, the SEC pass rate was 5/12, and iteration 3 produced four candidates of
which all four failed. Two structural reasons:

**All N candidates attack the same thing.** They share one analysis and differ
only by a directive string. The "top 3 critical paths" they are handed are, on
`mac_chain`, twenty bit-slices of one logic cone — same startpoint, endpoints
`acc_out[20..39]`, and a pairwise region-set Jaccard of 1.00.

**Whole-file rewrites.** Each candidate risks the whole design's equivalence to
fix one thing, and two good partial fixes in different candidates can never be
combined.

### The three changes

**1. `pathsel` picks *distinct* targets.** Paths are clustered into logic cones
by how much RTL, which cone-origin nets and which cell mix they share, and
selection is maximal-marginal-relevance, so the second target is the best one
that is also *different* from the first. Each cone is scored on three terms:

| term | what it is |
|---|---|
| **impact** | P(this cone holds the path that limits the clock) — see below |
| **severity** | how close its worst path is to the constraint: 0 = a full period of headroom, 0.5 = exactly at it, 1.0 = a period over |
| **tractability** | localisation coverage × whether a structural finding fired × whether the library knows this shape |

Neither term is gated on a violation, deliberately. **The target here is a
timing constraint, not a violation** — a design that already meets timing is
still worth speeding up if you want a tighter period, and that is the case
where "which path is worst" still matters but "which path is failing" has no
answer at all.

`k` is a ceiling, not a quota. On `mac_chain` the twenty paths correctly
collapse to **one** cone, and the selector says so rather than padding.

### Which path limits the clock

STA is deterministic: a slack is computed, not estimated, and the worst path is
known exactly. So the distribution below is **not** a probability that the
design fails, and it is not statistical STA over process variation.

It answers a question that *is* open before layout. Post-synthesis timing
contains no real wire delay, so two paths a few picoseconds apart are not yet
reliably ordered — and committing three agents to the nominally-worst one is a
bet on an ordering the flow has not established. Each path delay is treated as
`-slack + noise` and the noise is sampled:

```
noise_i = sigma · ( sqrt(rho)·z_cone(i)  +  sqrt(1-rho)·z_i )
```

`sigma` is a fraction of the clock period (`--delay-sigma`, default 0.05) and
`rho` is how much of that uncertainty a whole cone shares (`--cone-rho`,
default 0.7). The cone term matters: without it, twenty bit-slices of one
bottleneck would each be assigned 1/20 of the criticality and the cone that
owns all of it would look unimportant.

```
Which cone limits the clock (delay sigma 5% of the period, correlation 0.70):
  cone 0   100.0%    20 path(s)   worst slack -0.3573

Per path, most likely first:
   13.3%  slack -0.3573  (VIOLATED)  cone 0  -> acc_out[33]_DFF_X1_Q
   13.2%  slack -0.3573  (VIOLATED)  cone 0  -> acc_out[35]_DFF_X1_Q
   ...
```

Two properties worth knowing. `--delay-sigma 0` collapses it to the
deterministic answer exactly — all the weight on the worst path's cone. And it
works unchanged on a design that **meets** timing, because it ranks by slack
rather than by violation: two cones with +0.05 ns and +0.30 ns of headroom come
out at 97% and 3%, where the old violation-gated score could not separate them
at all.

Sensitivity is what you would want it to be — two cones 15 ps apart:

| `--delay-sigma` | cone A (+0.100) | cone B (+0.115) |
|---|---|---|
| 0 (deterministic) | 100% | 0% |
| 0.01 | 71% | 29% |
| 0.05 (default) | 55% | 45% |
| 0.20 | 51% | 49% |

The numbers are only as good as `sigma`, which is an assumption about how much
the estimate can move before layout, not a measurement. Set it to 0 if you want
the report taken at face value.

**When a design has fewer cones than agents, the cone's path is cut into
segments instead.** A long path is not homogeneous, and the cut is made where
its cell-family signature changes. On `mac_chain` that recovers exactly the
three bottlenecks `config.json` declares by hand — from the STA report alone,
without ever reading that field:

| target | stages | delay | signature | declared bottleneck |
|---|---|---|---|---|
| T1 | 0–24 | 36% | 18× AND/OR, **fanout 42** | four unpipelined 16×16 multipliers |
| T2 | 24–50 | 28% | 14× AOI/OAI, 12× XOR | serial adder chain |
| T3 | 50–91 | 36% | 36× AOI/OAI | 40-bit saturation compare |

Run it yourself against the committed artifact — no tools, no model:

```bash
python3 tools/pathsel.py runs/mac_chain/opt-20260901-025558-run1/baseline -k 3
```

**2. One specialist per target, scoped to it.** Each agent is told to change
only its target's logic and leave every other line byte-identical. Smaller
edits are likelier to survive the equivalence check, and they are what makes
step 3 possible at all. Scope is checked mechanically against the diff and
recorded — never auto-rejected, because discarding good work over an added
`wire` is worse than the ambiguity.

**3. The fixes are recombined, and measurement picks the winner.** Every subset
of the surviving candidates that composes by text splicing is assembled and
evaluated — four extra fully-evaluated designs per iteration for **zero model
calls**. A merge agent then reconciles the parts that genuinely overlap. Eq. 4
selects over `{specialists, mechanical unions, agent union, parent}`, so a union
does not win for being a union: combined fixes can trip the area penalty, or
expose a fourth path neither agent saw.

The mechanical unions are also the control on the merge agent. `summary.md`
reports `score(agent union) − score(best mechanical union)` every iteration. If
that is never negative, the merge call is not paying for itself.

### A pre-synthesis pass

`tools/rtlscan.py` reads the source before any tool has run and flags
constructs that synthesise into deep logic — serial reduction chains, chains
elaborated by a `generate` loop, wide operators, comparisons against bounds the
operand cannot reach. On `mac_chain` it finds the serial chain, the four
multiplies, and the dead saturation compare; on `alu32` it finds nothing
serious, which is the negative control.

```bash
make scan DESIGN=mac_chain
```

One agent then acts on it, and **it is adopted only if it measurably beats
D_0** under Eq. 3. This is the blindest step in the pipeline — there is no
timing report yet — and the [advisor's own recorded failure](#worth-knowing) is
exactly its failure mode. `--clean 0` skips it.

### The skill document

`.claude/skills/rtl-timing-optimization/SKILL.md` is rendered from the skill
library plus a written preamble, and concatenated into every agent's system
prompt. It has to be injected rather than loaded, because these agents run with
all tools denied — there is no Read tool to load a skill with, and giving one
back would turn a one-shot call into an agent that greps the repo.

`make skilldoc SKILLDOC_ARGS=--llm` adds one research call that may consult
public sources. It is the only place in this repo a model is given network
tools, and it is quarantined in `tools/skillgen.py` for that reason. Whatever
it finds enters the library as a **seed** — zero occurrences, zero confidence —
like everything else.

### Cost

| step | calls |
|---|---|
| path selection | **0** — it is arithmetic |
| specialists | k |
| mechanical unions, and evaluating them | **0** |
| merge agent | 1 |
| skill learning | 1 |
| **per iteration** | **k + 2 = 5** |

Defaults (`-k 3 --iters 4 --clean 1`) plan **21 calls**, but that is a ceiling:
`--patience 3` ends a run that has stopped earning, so the extra iterations
cost calls only when they are finding something. `--max-calls N` refuses to
start over budget; the plan is printed before anything runs and written to
`budget.json`.

Worth knowing before reading too much into a long run: across every run
recorded here, the best score came from **iteration 1**, and no later iteration
improved on it — the one exception being an early Opus run where iteration 2
did. The headroom is there for the cases where iterating pays, not because it
usually does.

### For a head-to-head

`--model` is plumbed through both loops, so the comparison measures the method
rather than the model:

```bash
make clean-skills          # the library persists; reset it between arms
make opt       DESIGN=mac_chain OPT_ARGS="--model haiku --iters 3"
make clean-skills
make portfolio DESIGN=mac_chain PF_ARGS="--model haiku --iters 3"
```

Match `--iters` explicitly: the two loops ship different defaults (3 and 4), so
leaving them out compares different budgets rather than different methods.

Compare the two `summary.md` files on WNS/TNS/area, and on SEC pass rate — the
scoped edits are the change most likely to move that number, and 5/12 is the
figure to beat.

### What this does not yet show

**The cone-selection half is undemonstrated.** Neither shipped design can
exercise it: `mac_chain` is one cone and `alu32` meets timing, so on both of
them the portfolio falls back to segments. `designs/dual_path/` exists for
this — one module, two unrelated slow datapaths (a serial MAC and a 40-way
priority cascade) that share no signal and synthesise to different cell mixes.
Its acceptance criterion is that `astra paths dual_path -k 3` returns **two**
cone targets whose pairwise similarity is below `--cluster-at`, and that their
mechanical union applies with no conflicts. That has not been run.

**Segmentation is a heuristic with a validation set of one.** It reproduces
`mac_chain`'s hand-declared bottlenecks, which is suggestive, not proof.

**Post-place-and-route is not built.** `rtl_map.from_run` and `pathsel.from_run`
take a `stage` argument and `--stage pnr` reads `03_pnr/timing.json`, which
`astra run --pnr` already writes in an identical shape. The second pass over it
is not wired up.

**Yosys remains the ceiling**, exactly as [noted above](#honest-differences-from-the-paper).

---

## Layout

```
designs/<name>/          inputs
    rtl/*.v
    constraints/*.sdc
    config.json          top module, clock(s), PDK

flow/config/*.tcl        PDK paths (liberty, LEF, site, layers)
flow/scripts/*.tcl       synth.tcl, sta.tcl, doctor.tcl
flow/scripts/sta_common.tcl  shared reporting procs, incl. per-clock-group slack

flow/scripts/sec.tcl     equivalence miter, discharged by Yosys's SAT engine
flow/scripts/sec_regcorr.tcl  register-correspondence SEC: elaborate, then prove the cones

tools/astra.py           the CLI
tools/clocks.py          the clock model — periods, groups, SDC generation
tools/parse_sta.py       OpenSTA report text → JSON
tools/astra_advise.py    run artifacts → Claude → advice.md (host-side)

tools/drrtl.py           the loop: orchestrator + the four agents
tools/pathsel.py         which distinct bottlenecks are worth an agent call
tools/portfolio.py       the path-portfolio orchestrator (subclasses drrtl)
tools/rtlscan.py         pre-synthesis structural smells, lexical
tools/merge.py           order-independent mechanical union + conflict report
tools/skillgen.py        renders SKILL.md, routes sections per agent role
tools/score.py           Eq. 1/3/4/5 — objective, selection, advantage
tools/rtl_map.py         critical path → RTL lines + structural root causes
tools/sec.py             SEC driver (regcorr on multi-clock, eqy, else the Yosys miter)
tools/regcorr.py         pair registers, cut them open, structural hash, SAT cones
tools/secbench.py        SEC over fixed candidates with no model: inner loop + soundness gate
tools/skills.py          the confidence-aware skill library
tools/llm.py             `claude -p` wrapper shared by the agents
tools/toolenv.py         dispatch EDA calls into the container
tools/protect.py         regions the optimiser may not touch, and the gate
tools/selftest.py        255 tests over the above (needs no EDA tools, no model)

docs/multi-clock.md      the clock model, and what it deliberately gives up
docs/equivalence-contract.md   what "equivalent" means, and where it stops

skills/library.json      learned pattern→strategy pairs (persists across runs)

graphify-out/            the code knowledge graph — derived, rebuilt by
    GRAPH_REPORT.md      `graphify update .`; the report names the commit it
    graph.json           was built from, so check that before trusting it
    graph.html

runs/<design>/<timestamp>/       outputs (gitignored)
    00_inputs/           the exact RTL + SDC used
    01_synth/            netlist.v, netlist.json, synth_stat.txt
    02_sta/              timing.rpt, timing.json, check_types.rpt
    logs/                raw tool output
    metrics.json         WNS, TNS, area, cell count, paths to artifacts
    advice.md            `make advise` output, if run

runs/<design>/pf-<timestamp>/    `make portfolio` outputs
    cleanup/             the pre-synthesis pass and whether it was adopted
    baseline/            D_0
    iter_NN/
        pathsel.json     the targets, with the value breakdown that chose them
        briefs/TN.md     what each specialist was given
        tN/              one per target: proposal, rtl, sec, eval
        merge/
            diffs/       unified diff per candidate
            conflicts.json   which regions more than one candidate claimed
            uNM/         mechanical subset unions (no model call)
            umerge/      the merge agent's union
    budget.json          planned vs actual model calls
    best/  trajectory.json  summary.md

runs/<design>/opt-<timestamp>/   `make opt` outputs
    baseline/            D_0 evaluated — what Eq. 3 is normalised against
    iter_NN/
        analysis.txt     the path→RTL mapping the agents were given
        candN/
            proposal.json    pattern, strategy, rationale, expected recovery
            reply.md         the model's full reply
            rtl/             the candidate design
            sec/             equivalence check log and verdict
            eval/            its own synthesis + STA run
    best/                the winning RTL, with its provenance
    trajectory.json      the three-layer hierarchical log (§4.2)
    summary.md           per-iteration tables, PPA deltas, SEC pass rate
```

`00_inputs/` holds a copy of the RTL and SDC each run used, so a timing number
can always be traced back to the source that produced it. `metrics.json` is the
one-file summary — read that instead of grepping logs. `02_sta/timing.json` has
the critical paths already parsed into structured form (per-stage cell, fanout,
capacitance, transition, slack).

---

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

Copy `designs/alu32/constraints/alu32.sdc` as your SDC starting point. Two
things about it worth knowing:

- Write `@CLK_PERIOD@` where the period goes. `--period 1.8` then updates both
  the constraint and the Yosys delay target, so sweeping frequency needs no
  file edits.
- It uses a `foreach` name filter to exclude the clock port from
  `set_input_delay`, **not** `remove_from_collection` — that's a Synopsys
  command and OpenSTA doesn't have it.

Then `astra doctor` to confirm it resolves.

### More than one clock

Replace the singular `clock` with a `clocks` list. The singular form still
works and is kept as a one-element alias, so nothing below is required for a
single-clock design.

```json
"clocks": [
  { "name": "clk_sys", "port": "clk_sys", "period_ns": 2.0 },
  { "name": "clk_io",  "port": "clk_io",  "period_ns": 8.0 },
  { "name": "clk_sys_div2", "generated_from": "clk_sys", "divide_by": 2,
    "port": "clk_sys_div2_r_DFFR_X1_Q/Q" }
]
```

A generated clock states its ratio instead of its period, and the period is
derived — including along a chain, so a ripple divider's second stage works.
Its `port` is the post-synthesis **pin** its divider flop drives.

Three SDC placeholders:

| placeholder | expands to |
|---|---|
| `@CLK_PERIOD@` | the primary clock's period — what every single-clock design uses |
| `@CLK_PERIOD:<name>@` | one named clock's period |
| `@ASTRA_CLOCK_DEFS@` | `create_clock` per master, `create_generated_clock` per divider, and `set_clock_groups -asynchronous` across independent masters |

Placeholders inside `#` comments are left alone, so documenting them in a
header is safe.

Declare anything the optimiser must not touch — clock domain crossings and
clock generation, which the equivalence check cannot see:

```json
"protected": ["cdc_*", "clk_*_div*"]
```

A candidate that adds, removes or alters any line mentioning a matching
identifier is rejected mechanically, **before** synthesis.

Then `astra run <design>`, and check the per-clock-group slack in
`02_sta/timing.json` under `clock_groups`. Each path is ranked against the
period of the clock that captured it, not the design's primary one — see
[`docs/multi-clock.md`](docs/multi-clock.md) for why that matters and what the
model deliberately gives up.

> **Sequential equivalence on a multi-clock design asks two questions, cheap
> one first.** A miter normally assumes every flop ticks together, which is
> false with five clocks. So registers are first paired by name and cut open
> (*register correspondence*): each pair must have the same clock, reset,
> reset value and next state, which holds for every interleaving of the
> domains and needs no depth. Unchanged logic is settled by structural
> hashing, so only the edited cone reaches a solver — seconds on a 50K-cell
> design. A pass is `method: regcorr`, unbounded. Whatever it cannot prove
> (typically a retimed candidate) goes to the whole-design `clk2fflogic`
> check, where every clock is a free input and the verdict is `bounded`.
> [`docs/equivalence-contract.md`](docs/equivalence-contract.md) is the
> reasoning. `--sec-regcorr-timeout` and `--sec-fallback-timeout` bound the two
> stages; `--sec-fallback-timeout 0` skips the bounded one. Note
> `--engine eqy` declines on multi-clock; it has no multiclock mode.
>
> To work on SEC itself without a model in the loop:
> `python3 tools/secbench.py netproc --case good:pass:path/to/cand.v --case bug:fail:path/to/mutant.v`.
> It exits non-zero if any `fail` case is reported equivalent.

Four things bite when writing a multi-clock SDC, all of which fail loudly but
uninformatively. They are indexed by symptom in `HANDOFF.md` §7; the shortest
version is that OpenSTA cannot parse a bus subscript in a `get_ports` pattern
(`Error: stoi`), which also means an instance name containing `[` is unusable —
so write clock dividers as pure toggle flops.

---

## The example designs

| design | clocks | result |
|---|---|---|
| `alu32` | 2.5 ns | **MET**, WNS +0.3701, 1318 cells, 1871 µm² |
| `mac_chain` | 2.0 ns | **VIOLATED**, WNS −0.3573, TNS −3.7438, 15 endpoints |
| `dual_path` | 1.5 ns | not yet run — see [path-portfolio mode](#what-this-does-not-yet-show) |
| `soc_bench` | 13 clocks, 5 async | **VIOLATED**, WNS −6.5126, TNS −65.1401, 60 endpoints, 49,935 cells |
| `dual_clock` | 3 clocks, 2 async | **VIOLATED**, WNS −0.0523, TNS −0.6798, 13 endpoints, 1,307 cells |
| `netproc` | 13 clocks, 5 async | **VIOLATED** in 4 groups, WNS −17.83, TNS −764.26, 184 endpoints, 50,502 cells |

`alu32` is the sanity check. Its critical path is the 32-bit ripple-carry adder
Yosys infers — ~70 gates deep, which is why it needs 2.5 ns and not 1.0.
Yosys produces weaker arithmetic than commercial synthesis; expect that.

`mac_chain` is the interesting one: a 4-tap 16×16 MAC with a serial accumulate
chain and a saturation compare at the end of the path, 86 gates deep. It
violates on purpose, so there's something real in the report to look at.

`soc_bench` is the scale-and-clocks benchmark: five independent asynchronous
masters (2, 3, 4, 5, 8 ns), eight generated clocks at ratios 2/4/8, six
two-flop synchronisers plus a gray-coded bus crossing between them, a seven-state
packet framer, and ~50K cells. Each of its five domains carries one deliberate
bottleneck that RTL can fix without changing latency. Synthesis takes ~125 s, so
budget about two minutes per candidate evaluation.

```bash
astra run soc_bench                        # ~2 min: synthesis + multi-clock STA
python3 tools/pathsel.py runs/soc_bench/<run> -k 3
```

`dual_clock` is the small multi-clock design: two asynchronous domains, one
generated clock, one CDC crossing, ~1.3K cells and **no multipliers**. That
last part is deliberate — a bounded equivalence check unrolls the design once
per solver step, and unpipelined multipliers make that intractable, so
`soc_bench` can have a bad candidate refuted quickly but cannot have a good one
proved. `dual_clock` exists so the multi-clock loop can be exercised where the
proof actually closes. It is not a substitute for the benchmark.

Register correspondence has since removed most of that asymmetry: a rewrite
that keeps register names is proved in seconds on `netproc` and `soc_bench`
alike, multipliers included, because unchanged logic never reaches the solver.
Read the caveat in [More than one clock](#more-than-one-clock) for what is
still bounded.

---

## Variants

```bash
make build PDKS="nangate45 sky130hd"          # add another PDK
make build PLATFORM=linux/amd64               # image for x86 teammates
make build DOCKERFILE=docker/Dockerfile.orfs  # + OpenROAD place & route
```

The lean image is synthesis and timing only. `docker/Dockerfile.orfs` builds on
`openroad/orfs` instead, which adds OpenROAD and all four PDKs — but it's 20 GB+
and amd64-only, so only reach for it when you actually need place & route
(`astra run <design> --pnr`).
