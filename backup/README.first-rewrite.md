# ASTRA — agentic RTL timing optimisation

ASTRA takes a Verilog design that misses its timing target and uses AI agents
to rewrite the RTL until it is faster — while **proving** every rewrite still
behaves exactly like the original.

It has two parts:

1. **A containerised EDA flow.** One Docker image with Yosys (synthesis),
   OpenSTA (static timing analysis) and the nangate45 standard-cell library.
   Point it at a design and get a netlist and a timing report. Nobody installs
   EDA tools locally.
2. **Two closed-loop optimisers on top of it**, driven by Claude:
   - `make opt` — **Dr. RTL**, a reimplementation of *Dr. RTL: Autonomous
     Agentic RTL Optimization through Tool-Grounded Self-Improvement*
     (Fang et al., 2026, [paper](docs/papers/dr-rtl-2604.14989v2.pdf)).
   - `make portfolio` — **path-portfolio mode**, our extension: it splits the
     design into several distinct bottlenecks, gives each its own agent, and
     recombines their fixes.

```
            ┌──────────────────────────── repeat ────────────────────────────┐
            ▼                                                                │
RTL ──► synthesis + timing ──► pick bottlenecks ──► agents rewrite RTL ──► equivalence check
         (Yosys, OpenSTA)       (critical paths)      (Claude, in parallel)   (Yosys SAT)
                                                                              │
                                              keep the best proven rewrite ◄──┘
                                              and learn which tricks worked
```

## Results

Selected runs are committed, trimmed, under [`results/`](results/README.md).
All used Claude Haiku and a single iteration; every promoted rewrite passed the
equivalence check.

| design | what it is | WNS (ns) | TNS (ns) | area |
|---|---|---|---|---|
| `mac_chain` | 4-tap multiply-accumulate, 1 clock | −0.357 → **−0.162** | −3.74 → −1.55 | −0.1% |
| `dual_clock` | 2 async domains + divided clock | −0.052 → **+0.399** (timing met) | −0.68 → 0 | −2.1% |
| `soc_bench` | 13 clocks, ~50K cells | −6.51 → −6.51 | −65.1 → **−38.1** | −0.1% |
| `netproc` | 13 clocks, ~50K cells, no multipliers | −17.83 → **−4.84** | −764 → −304 | +1.0% |
| `vending_machine` | real-world FSM with 1,024-bit datapath | −8.03 → **−2.34** | −4416 → −207 | **−31.9%** |

WNS = worst negative slack (how far the slowest path misses the clock),
TNS = total negative slack over all failing endpoints. Closer to zero is better;
positive means timing is met.

## Quick start

You need Docker, `make`, `git` and Python 3. The optimisers additionally need
the [`claude` CLI](https://docs.claude.com/en/docs/claude-code) logged in on
the host.

```bash
git clone git@github.com:LunarLogician007/astra-vlsi.git && cd astra-vlsi

make build                     # build the image (~5–10 min, native arm64 or x86)
make doctor                    # check tools, PDK and every design
make run DESIGN=mac_chain      # synthesis + timing report
```

```
[astra] synthesis ok in 4.05s: 7343 cells, area 8942.9200 um^2
[astra] post-synthesis timing: VIOLATED
    WNS = -0.3573 ns    TNS = -3.7438 ns    violating endpoints = 15
```

Then optimise it:

```bash
make portfolio DESIGN=mac_chain PF_ARGS="--model haiku --iters 1"
make opt       DESIGN=mac_chain OPT_ARGS="--model haiku --iters 1"
```

Both write to `runs/<design>/<run-id>/`; read `summary.md` there first. Add
`--dry-run` to see the prompts without calling the model (the container is
still needed, since the baseline is evaluated for real).

No Docker? The core logic is still testable:

```bash
python3 tools/selftest.py      # 255 tests, no EDA tools, no model, no network
```

<details>
<summary><b>Platform notes</b> (Windows/WSL2, macOS)</summary>

**Windows (WSL2).** Run everything inside the WSL shell, and clone into the
WSL filesystem (`~`), not `/mnt/c` — Windows drives lose Unix permissions and
are slow.

```bash
curl -fsSL https://get.docker.com | sudo sh     # or Docker Desktop with the WSL2 backend
sudo usermod -aG docker $USER                   # then reopen the shell
sudo service docker start                       # after each WSL restart
sudo apt update && sudo apt install -y make git
```

If `make build` fails with `bad interpreter: /bin/bash^M`, git converted line
endings: `git config --global core.autocrlf false` and re-clone.

**macOS.**

```bash
brew install colima docker docker-buildx
colima start --cpu 6 --memory 10 --disk 60
```

**Memory.** Each tool container is capped at 12 GB (`DOCKER_MEM`). On an 8 GB
machine use `DOCKER_MEM=6g`, e.g. `make portfolio DESIGN=netproc DOCKER_MEM=6g`.

</details>

## Commands

Run from the host with `make`, or inside the container (`make shell`) with
`astra`. `DESIGN` defaults to `mac_chain`.

| make | what it does | needs |
|---|---|---|
| `make build` / `make doctor` | build the image / check it | Docker |
| `make shell` | shell in the container, repo mounted at `/work` | Docker |
| `make list` | designs and past runs | Docker |
| `make run DESIGN=x` | synthesis + timing | Docker |
| `make localise DESIGN=x` | map the critical path back to RTL lines | Docker |
| `make scan DESIGN=x` | pre-synthesis structural smells in the RTL | Python |
| `make paths DESIGN=x` | which distinct bottlenecks are worth an agent | Python + a run |
| `make advise DESIGN=x` | ask Claude how to close timing on the last run | `claude` |
| `make opt DESIGN=x` | Dr. RTL loop | Docker + `claude` |
| `make portfolio DESIGN=x` | path-portfolio loop | Docker + `claude` |
| `make skills` / `make skilldoc` | show the learned skill library / rebuild the skill doc | Python |
| `make selftest` | the test suite | Python |
| `make clean` / `make clean-skills` | delete `runs/` / reset the skill library | — |

Every flag and option is documented in [`docs/guide.md`](docs/guide.md).

## How it works

**Evaluate.** Yosys synthesises the RTL to nangate45 cells and OpenSTA reports
slack per path and per clock. Every run stores its exact inputs, so any number
can be traced back to the source that produced it.

**Choose what to fix.** `tools/rtl_map.py` maps each critical path back to RTL
lines. In portfolio mode, `tools/pathsel.py` clusters paths into logic cones and
picks *distinct* targets; if one cone dominates, it cuts that path into segments
where its cell mix changes. On `mac_chain` this recovers the design's three
hand-documented bottlenecks from the timing report alone.

**Rewrite.** Agents receive a brief for their target plus a skill document of
known transformations. Agents have no tool access — they only return RTL. In
portfolio mode each is scoped to its own target, so fixes can be spliced
together mechanically (every subset is tried at zero model cost).

**Verify.** Each candidate goes through sequential equivalence checking
(`tools/sec.py`) before it is ever synthesised. Multi-clock designs first try
register correspondence, which proves unchanged logic structurally and only
sends the edited cone to a SAT solver. A rewrite that changes behaviour is
thrown away, however fast it is.

**Select and learn.** Candidates are scored on WNS, TNS and area
(Eq. 3 of the paper). The best proven one becomes the next parent, and
`skills/library.json` records which pattern→strategy pairs actually helped, so
later runs — on any design — start from that knowledge.

The full explanation, including the equations, costs and the honest
differences from the paper, is in [`docs/guide.md`](docs/guide.md).

## Repository layout

```
designs/<name>/            the benchmark designs: rtl/, constraints/*.sdc, config.json
flow/                      Tcl for synthesis, STA, equivalence; per-PDK config
tools/                     all Python
    astra.py                 the CLI
    drrtl.py                 Dr. RTL loop (make opt)
    portfolio.py             path-portfolio loop (make portfolio)
    pathsel.py, rtl_map.py   bottleneck selection, path → RTL mapping
    sec.py, regcorr.py       equivalence checking
    score.py, skills.py      scoring equations, skill library
    selftest.py              the test suite
skills/library.json        learned optimisation skills (persists across runs)
.claude/skills/            skill document injected into every agent prompt
docker/                    Dockerfile (lean) and Dockerfile.orfs (+ OpenROAD)
docs/
    guide.md                 full manual
    HANDOFF.md               development notes, measured runs, known pitfalls
    equivalence-contract.md  what "equivalent" means here, and its limits
    multi-clock.md           the clock model
    astra-portfolio-report.tex   technical report (LaTeX)
    papers/                  the Dr. RTL paper (arXiv 2604.14989) and abstract.pdf
benchmarks/
    rtl_dataset/             real-world RTL, a pool for new designs
    sec_cases/               good/broken rewrites for testing the equivalence checker
results/                   selected, trimmed optimisation runs
runs/                      your local run outputs (gitignored)
```

## The designs

| design | clocks | why it exists |
|---|---|---|
| `alu32` | 1 | sanity check — meets timing |
| `mac_chain` | 1 | small, violates on purpose; the main single-clock benchmark |
| `dual_path` | 1 | two unrelated slow datapaths, to exercise cone selection |
| `dual_clock` | 3 | small multi-clock design where equivalence proofs close quickly |
| `soc_bench` | 13 | scale: five async domains, CDC, ~50K cells |
| `netproc` | 13 | like `soc_bench` but without multipliers |
| `vending_machine` | 1 | from the real-world dataset |

To add your own, see "Adding a design" in [`docs/guide.md`](docs/guide.md#adding-a-design).

## Limitations

- **Open-source synthesis.** Yosys produces weaker arithmetic than commercial
  tools, so a slack gain here is a smaller claim than the same number in the
  paper.
- **Post-synthesis timing only.** No wire delay; place-and-route exists
  (`make build DOCKERFILE=docker/Dockerfile.orfs`, then `make pnr`) but the
  optimisers do not use it yet.
- **Some proofs are bounded.** When a proof cannot close, SEC falls back to a
  bounded check and records `method: bounded` — read that field before trusting
  a result.
- **Model calls cost plan usage.** The loops call `claude -p`, which draws from
  your Claude subscription limits. `--max-calls` caps a run, and the plan is
  printed before anything is spent.
