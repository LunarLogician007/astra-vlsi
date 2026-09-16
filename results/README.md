# Selected runs

A handful of optimisation runs worth keeping, copied out of `runs/` (which is
gitignored). Each one is **trimmed**: netlists, raw tool logs, full timing
reports and SEC cone dumps are left out, because they run to hundreds of MB and
can be regenerated. What stays is everything needed to follow the run: the
summary, the trajectory, every candidate's RTL, proposal and model reply, the
equivalence verdicts, per-candidate `metrics.json`, and the winning design.

Where to look first in any run: `summary.md`, then `best/`.

| run | loop | design | WNS (ns) | TNS (ns) | area | notes |
|---|---|---|---|---|---|---|
| [`mac_chain/opt-20260915-103411`](mac_chain/opt-20260915-103411/summary.md) | `make opt` (Dr. RTL) | mac_chain, 1 clock | −0.357 → **−0.162** | −3.74 → −1.55 | −0.1% | SEC 4/4. Its `baseline/` keeps `02_sta/timing.json`, which the self-test uses as the path-selection reference |
| [`mac_chain/pf-20260913-160548`](mac_chain/pf-20260913-160548/summary.md) | `make portfolio` | mac_chain, 1 clock | −0.357 → **−0.166** | −3.74 → −1.62 | +0.1% | winner came from the pre-synthesis cleanup agent |
| [`dual_clock/pf-20260915-113841`](dual_clock/pf-20260915-113841/summary.md) | `make portfolio` | dual_clock, 3 clocks | −0.052 → **+0.399 (met)** | −0.68 → 0 | −2.1% | adder tree; proved by register correspondence |
| [`soc_bench/pf-20260915-114830`](soc_bench/pf-20260915-114830/summary.md) | `make portfolio` | soc_bench, 13 clocks, ~50K cells | −6.51 → −6.51 | −65.1 → **−38.1** | −0.1% | accumulate rewritten as a tree |
| [`netproc/pf-20260915-095208`](netproc/pf-20260915-095208/summary.md) | `make portfolio` | netproc, 13 clocks, ~50K cells | −17.83 → **−4.84** | −764 → −304 | +1.0% | parity tree; first run where cone selection fired |
| [`vending_machine/pf-20260915-223543`](vending_machine/pf-20260915-223543/summary.md) | `make portfolio` | vending_machine, 1 clock | −8.03 → **−2.34** | −4416 → −207 | **−31.9%** | two 1,024-bit adders muxed into one |

All runs used `--model haiku` and one iteration. Some `summary.md` files print
"not adopted" for the cleanup step; that refers only to the pre-synthesis pass,
not to the run's final result.

## Layout of a run

```
<design>/pf-<timestamp>/          make portfolio
    summary.md                    start here: PPA table, SEC tallies, per-iteration detail
    trajectory.json               the full hierarchical log
    budget.json                   planned vs actual model calls
    best/                         the winning RTL and where it came from
    baseline/                     D_0: the unmodified design, evaluated
    cleanup/                      the pre-synthesis pass (rtlscan findings + one agent)
    iter_01/
        pathsel.{txt,json}        which bottlenecks were chosen, and why
        briefs/TN.md              what each specialist agent was told
        tN/                       one per target: proposal, reply, rtl/, sec/, eval/
        merge/                    mechanical unions (uNM/), agent merge (umerge/), diffs

<design>/opt-<timestamp>/         make opt
    iter_01/analysis.txt          the critical-path to RTL mapping the agents got
    iter_01/candN/                proposal, reply, rtl/, sec/, eval/
```

To keep a new run, copy it here the same way: drop `netlist.*`, `*.log`,
`timing.rpt`, `timing.json`, `check_types.rpt` and the `regcorr_*`/`*_cone.json`
SEC dumps.
