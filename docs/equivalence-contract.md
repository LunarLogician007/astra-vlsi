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

### Adopted: per-domain SEC with the CDC boundaries cut

The design is partitioned by clock domain. Each partition has exactly one
clock, which is the case both engines can actually discharge.

At a crossing from domain `X` to domain `Y`, the crossing signal is **cut**:

- on `X`'s side it becomes an observed output — so a rewrite that changes what
  `X` launches is caught;
- on `Y`'s side it becomes a free input, unconstrained — so `Y` must be proved
  correct for *every* value and arrival, which is what asynchrony means.

The result is a set of per-domain obligations, each a well-posed single-clock
SEC. Their conjunction is the design's verdict: every domain equivalent, and
every crossing launching the same values.

### What this does not prove

The cut is where the proof stops, and three things live in the cut:

1. **Synchronizer structure.** Flop depth, and that a synchronizer is there at
   all. Cutting the boundary makes a removed synchronizer invisible.
2. **Crossing protocol.** Handshakes, request/acknowledge sequencing, FIFO
   pointer discipline.
3. **Multi-bit coherence.** Gray coding, and that a multi-bit crossing is not
   silently re-encoded so two bits can change in one destination cycle.

None of these are timing bottlenecks and none of them should ever be edited to
recover slack. So they are handled the same way pipelining is — **out of scope
for the optimiser, structurally, rather than checked after the fact.**

This is enforced, not merely requested. A design declares what is off limits:

```json
"protected": ["cdc_*", "clk_*_div*"]
```

and `tools/protect.py` rejects any candidate that added, removed or altered a
line mentioning a matching identifier. It runs **before** synthesis and before
SEC — deliberately ahead of the gate that cannot catch it. A rejection records
as *undecided*, never a refutation, so it cannot teach the skill library that a
sound transformation breaks equivalence.

---

## 3. What the code does today

`sec.check()` takes the design's `ClockSet`. On a multi-clock design it
**declines** and returns `method: "unsupported"` with the reason.

This is deliberate and is the whole point of writing the contract down:

- `unsupported` is **not** a refutation. `score.sec_decided()` treats it as
  undecided, so it never teaches the skill library that a sound transformation
  breaks equivalence — the failure mode that already cost this codebase a
  fragmented library once.
- `unsupported` is **not** a pass. `score.sec_passed()` is False, so Eq. 4
  cannot promote the candidate.
- It appears in the SEC tally under `undecided`, so a run on a multi-clock
  design reports honestly that nothing was proven, instead of reporting a pass
  rate over checks that did not mean anything.

The per-domain partitioning of §2 is **not implemented**. Until it is, a
multi-clock design cannot promote any candidate. That is the correct failure
mode — it stops rather than over-claims — but it does mean the benchmark of
HANDOFF §6.2 cannot run the optimisation loop end to end until the partitioner
exists.

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
