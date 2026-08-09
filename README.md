# ASTRA — shared VLSI container

One Docker image with **Yosys + OpenSTA + nangate45**. Point it at a design,
get a netlist and a timing report. Nobody on the team installs EDA tools
locally.

```
RTL + SDC  ──►  Yosys  ──►  netlist  ──►  OpenSTA  ──►  timing report
                                                        (WNS / TNS / paths)
```

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

---

## Layout

```
designs/<name>/          inputs
    rtl/*.v
    constraints/*.sdc
    config.json          top module, clock, PDK

flow/config/*.tcl        PDK paths (liberty, LEF, site, layers)
flow/scripts/*.tcl       synth.tcl, sta.tcl, doctor.tcl

tools/astra.py           the CLI
tools/parse_sta.py       OpenSTA report text → JSON

runs/<design>/<timestamp>/       outputs (gitignored)
    00_inputs/           the exact RTL + SDC used
    01_synth/            netlist.v, netlist.json, synth_stat.txt
    02_sta/              timing.rpt, timing.json, check_types.rpt
    logs/                raw tool output
    metrics.json         WNS, TNS, area, cell count, paths to artifacts
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

---

## The two example designs

| design | period | result |
|---|---|---|
| `alu32` | 2.5 ns | **MET**, WNS +0.3701, 1318 cells, 1871 µm² |
| `mac_chain` | 2.0 ns | **VIOLATED**, WNS −0.3573, TNS −3.7438, 15 endpoints |

`alu32` is the sanity check. Its critical path is the 32-bit ripple-carry adder
Yosys infers — ~70 gates deep, which is why it needs 2.5 ns and not 1.0.
Yosys produces weaker arithmetic than commercial synthesis; expect that.

`mac_chain` is the interesting one: a 4-tap 16×16 MAC with a serial accumulate
chain and a saturation compare at the end of the path, 86 gates deep. It
violates on purpose, so there's something real in the report to look at.

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
