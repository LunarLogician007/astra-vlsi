# Multi-clock support

The framework's clock model, and the two places it deliberately loses
information. This is HANDOFF.md §6.1, implemented.

## Why it had to change first

The data model was singular: one `clock` object, one name, one period. Every
consumer divided by it. That is correct for a single-clock design and silently
wrong for anything else.

The specific trap, and the reason this had to land before the benchmark rather
than alongside it: `pathsel.severity()` divides slack by a period. With five
domains and one period, paths in four of them are normalised against the wrong
number. Nothing raises. Nothing looks odd. The portfolio picks the wrong
targets and reports confident numbers while doing it — so the benchmark would
have produced measurements that looked fine and meant nothing.

## The model

`tools/clocks.py`. Stdlib only, no imports from the rest of the tree, so every
consumer can resolve a period the same way without an import cycle.

```json
{
  "clocks": [
    {"name": "clk_sys",  "port": "clk_sys",  "period_ns": 2.0},
    {"name": "clk_io",   "port": "clk_io",   "period_ns": 8.0},
    {"name": "clk_sys_div4", "generated_from": "clk_sys", "divide_by": 4,
     "port": "div4/Q"}
  ]
}
```

- A **generated clock** may state `divide_by` instead of `period_ns`; the
  period is derived. Making the author restate the product is how the two drift
  apart.
- The singular `clock` object still loads and still works. It is kept as a
  one-element alias in both `config.json` and `metrics.json`, so the three
  shipped designs need no edit and a reader predating this change does not
  read `None`.
- **Grouping:** a generated clock is synchronous to its master and joins its
  group. Independent masters are asynchronous to each other, and
  `set_clock_groups -asynchronous` is generated from that.

## SDC

Three placeholders, in increasing order of expressiveness:

| placeholder | expands to |
|---|---|
| `@CLK_PERIOD@` | the primary clock's period — what every existing design uses |
| `@CLK_PERIOD:<name>@` | a named clock's period |
| `@ASTRA_CLOCK_DEFS@` | `create_clock` / `create_generated_clock` for every clock, plus the async groups |

A `@CLK_PERIOD:<name>@` naming a clock that is not declared is caught at
substitution time. Left alone it would reach OpenSTA as a syntax error saying
nothing about the cause.

## Where a path's period comes from

OpenSTA names a path group after the clock that captures the path, and
`parse_sta` has always recorded `Path Group` per path — the information was
there and was being discarded. It is now the join key.

- `PathFeatures.period_ns` is the period of the clock that captured **that**
  path, with `period_exact` recording whether the lookup hit or fell back.
- `pathsel.criticality()` scales its delay uncertainty per path. `sigma` is a
  *fraction* of a period, so 5% of a 10 ns cycle is five times the absolute
  slop of 5% of a 2 ns cycle. One shared scale hands the slow domain's
  uncertainty to the fast one.
- A fallback is reported in the selection artifact under `clocks.unresolved`,
  never swallowed. A path ranked against a period that is not its own is
  exactly the silent failure this change exists to prevent, so it has to be
  visible in the artifact rather than only in the conclusion drawn from it.

## Per-clock-group WNS/TNS

`sta_common.tcl` emits `<tag>.group.<clock>.wns_ns` / `.tns_ns` /
`.violating_endpoints`, bucketed by capture clock. Every call is guarded: on a
build where path objects do not expose `slack` through `get_property`, no group
KVs are emitted and `parse_sta.clock_groups()` falls back to bucketing the
*reported* paths itself. That fallback is labelled `source: "reported"` and
`complete: false`, because it sees only the reported paths — its TNS is a
floor, not a total, and a truncated TNS used as a denominator inflates every
share computed from it.

---

## Two deliberate losses of information

### 1. `abc -D` targets the tightest period

Yosys maps the whole design in one pass and `abc` takes one delay target, so a
per-domain target is not expressible without partitioning the netlist — which
fights the `flatten` the flow relies on for path-to-RTL naming.

**The tightest period is used.** It never under-constrains, so no domain is
mapped too slowly to meet its own clock.

The cost is real: logic in a slow domain is optimised against a target it did
not need, and `abc` may buy delay with area that domain did not need either. On
a design with a 2 ns and an 8 ns domain, the 8 ns logic is mapped as though it
had to close at 2 ns. Expect area inflation in slow domains, and do not read it
as a property of the RTL.

`astra syn` prints the target and this caveat whenever the design has more than
one clock. The alternative — partitioning the netlist per domain and mapping
each with its own target — is the accurate answer and a substantial Yosys flow
change. `ClockSet.synthesis_period` is the single place that decision lives.

### 2. Eq. 3 scales by one period

WNS and TNS are design-wide scalars, so the zero-band floor in
`score.norm_timing()` needs one number. `score.critical_period()` uses the
period of the group that owns the worst slack — that group is where the
reported WNS came from, so it is the cycle the number is already relative to.

With nothing violating, it falls back to the **tightest** clock, which gives
the smallest floor, so the zero band never swallows a real change in a fast
domain.

## What is not done

- Per-domain synthesis targets (above).
- Sequential equivalence across domains — see
  [`equivalence-contract.md`](equivalence-contract.md). `sec.check()` declines
  on a multi-clock design rather than returning a confident answer to the
  wrong question.
- No multi-clock design ships in `designs/`, so the per-group STA path has unit
  coverage but has not been exercised against real OpenSTA output. That is the
  first thing the §6.2 benchmark will test.
