# The equivalence contract

What ASTRA means when it says a rewrite is equivalent, and — more usefully —
what it does not mean. This is HANDOFF.md §6.3, decided.

It exists because objectives 5 and 6 are in tension. Objective 5 (formal
equivalence) works *because* the designs are small and single-clock. Objective
6 (a ~50K-cell benchmark with five asynchronous domains) removes both
conditions. Without a written contract the loop keeps reporting the same word,
"equivalent", while the claim behind it quietly weakens.

---

## 1. The claim, on a single-clock design

Every candidate that reaches Eq. 4 has passed a sequential equivalence check
against the design it was derived from: for all reachable states, the two
designs produce the same outputs cycle for cycle.

Two engines produce this, and they do not prove the same thing:

| method | what it establishes |
|---|---|
| `induction` (Yosys), `pass` (eqy) | unbounded — holds for all reachable states |
| `bounded` | no counterexample within N cycles from reset |

`sec.py` records which, and the orchestrator keeps the difference rather than
flattening it. A bounded-only pass is a weaker claim than the paper's, and it
is labelled as one.

### A bounded pass is worth exactly what its depth can see

This is the sharpest edge in the whole document, it applies to the
single-clock bounded fallback as much as to the multi-clock path, and nothing
checks it automatically.

A bounded check says "no counterexample within N". If the difference between
the two designs cannot be *reached* within N, the check says the same thing it
would say about two identical designs — **equivalent** — and it is wrong.

Measured on `dual_clock`, a 1,307-cell design whose candidate had a genuine
functional bug (`acc[g-1] - term[g]` where the gold has `+`):

| depth | steps | broken candidate | equivalent candidate |
|---|---|---|---|
| 4 | 8 | **reported equivalent** — false pass, 3 s | proved, 2 s |
| 6 | 12 | refuted, 3 s | proved, 51 s |
| 8 | 16 | refuted, 5 s | no verdict in 300 s |

Eight steps cannot propagate the difference to an output, so the shallow check
passes a broken design in three seconds. Nothing in the tooling can tell that
apart from a real proof.

Note how narrow the usable window is: one step too shallow and the check is
vacuous, two too deep and it does not finish. Depth is not a dial to be set
once and forgotten, which is why a design can record the depth its author
actually verified (`sec_depth` in `config.json`) instead of inheriting a global
default that was calibrated on a different design.

Two consequences, both load-bearing:

- **Never tune depth down to make proofs finish.** A depth chosen so that
  proofs close is a depth chosen so that they are vacuous. `tools/sec.py`
  carries this measurement in a comment and a test guards against
  reintroducing the cap that was briefly added and then removed.
- **`induction` and `bounded` are not two grades of the same claim.** An
  induction pass holds for all reachable states. A bounded pass holds for a
  window whose adequacy nobody verified. Read `method` before believing a
  promotion.

**Latency is part of the contract.** Adding or removing a pipeline stage
changes the cycle-by-cycle relationship and the check fails by construction.
This is why pipelining is forbidden in every agent prompt (`drrtl.py`,
`portfolio.py`, `SKILL.md`). Allowing it means changing this contract first,
not editing a prompt.

---

## 2. The claim, on a multi-clock design

**Cycle-by-cycle equivalence is not well defined across asynchronous domains.**
Two clocks with no phase relationship have no shared notion of "the same
cycle", so there is no sequence of outputs to compare. The question the miter
answers is not the question that was asked.

HANDOFF §6.3 offered two routes. Only one of them is sound.

### Rejected: bounded equivalence with `set_clock_groups -asynchronous`

`set_clock_groups` is a *static timing analysis* construct. It tells OpenSTA
not to analyse paths between two clocks. It has no meaning to a SAT miter,
which has no concept of a clock group — it needs a concrete clock model, and
picking one relative frequency proves equivalence only at that ratio. Genuine
asynchrony is precisely the claim that no ratio is privileged, so a proof at
one ratio says nothing about the design. This option conflates an STA concept
with a formal one and is not adopted.

### Considered: per-domain SEC with the CDC boundaries cut

Partition by clock domain so each partition has one clock; at a crossing from
`X` to `Y`, cut the signal — an observed output on `X`'s side, a free
unconstrained input on `Y`'s. Sound, and it is what this document originally
adopted.

It was **superseded during implementation** by something simpler and strictly
stronger. Recorded here because the reasoning still matters: cutting is the
right idea, and the adopted method is that idea taken further.

### Adopted: whole-design equivalence with every clock free

The reason a miter needs one clock is an assumption buried in the solver, not a
property of the design. Yosys's `sat` models a flop as "Q at step t+1 is D at
step t" and ignores the clock entirely — which silently assumes every flop in
the design ticks together. True of one clock. False of five.

`clk2fflogic` removes the assumption instead of working around it. Every
clocked flop becomes explicit edge-detection logic over an ordinary input, so
the design has no clocks left at all and the solver chooses each clock's
waveform freely. A pass then holds for **every interleaving of the five
domains**, which is what asynchrony means.

This is better than partitioning on three counts:

- It proves the crossings too, rather than cutting them out of the claim. The
  three things the cut could not see — synchroniser depth, crossing protocol,
  multi-bit coherence — are inside the proof, because a design that behaves
  differently under some interleaving is exactly what a removed synchroniser
  stage produces.
- There is nothing to partition, so nothing to get wrong. A partitioner that
  mis-assigns one register produces a confident, vacuous pass; this has no such
  failure mode.
- It is about forty lines of Tcl rather than a new subsystem.

**What it still does not model: metastability.** Clock-domain crossings are
about analogue settling behaviour that no cycle-level model captures. A
synchroniser removed from a design that is otherwise equivalent under every
interleaving would likely be caught here, but "likely" is not a guarantee, and
metastability is not represented at all. So CDC logic stays out of the
optimiser's editable scope regardless — see below. This check is the second
line of defence, not the first.

---

## 3. What the code does today

`sec.check()` takes the design's `ClockSet` and routes on it:

| design | route | strongest verdict |
|---|---|---|
| single clock | miter, induction then bounded | `induction` — unbounded |
| multi clock | miter with `clk2fflogic` | `bounded` |
| multi clock, `engine="eqy"` | declines (`unsupported`) | — |

**A multi-clock verdict is always bounded.** With free clocks, temporal
induction converges neither way — measured, not assumed — so it is skipped
rather than run to burn the time budget. And an edge now takes two solver steps
instead of one, so the requested depth is doubled into steps to keep the cycle
count comparable.

eqy still declines: it partitions against a common clock and has no multiclock
mode, so it would answer the wrong question confidently.

Everything the old declining path protected still holds. `unsupported` remains
neither a pass nor a refutation, and a timeout is still `undecided` rather than
evidence of inequivalence — which matters more here than anywhere else, because
of the scale below.

## 4. Scale

SEC already hit the 1800 s timeout on a **7,343-cell** single-clock design. At
50K cells, bounded SEC will time out routinely.

Two things follow:

- **Per-domain cutting helps.** Each obligation is a fraction of the design, so
  partitioning is a scaling measure as much as a soundness one. This is also
  why `eqy` is preferred where installed: it partitions further.
- **`sec_decided()` becomes load-bearing.** At this scale a timeout is the
  common case, not a rarity. The distinction between "refuted" and "not
  decided" carries the honesty of every reported pass rate, and
  `score.sec_tally()` counts over decided candidates for exactly this reason.

A run whose SEC tally is mostly `undecided` has not demonstrated much. That
should be read off the tally, not inferred from the pass rate.

---

## 5. Related constraints

Not part of the equivalence claim, but the same clock model drives them:

- **CDC paths are excluded from setup analysis** via `set_clock_groups
  -asynchronous`, generated by `clocks.render_clock_groups()`. Without it every
  crossing reports as an enormous violation, and those false violations swamp
  the TNS shares and criticality distribution the portfolio ranks on — the
  selector would spend every agent call on a crossing that must not be touched.
- **Each path is normalised against its own clock's period.** See
  `docs/multi-clock.md`.
