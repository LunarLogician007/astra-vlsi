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

### Kept as the fallback: whole-design equivalence with every clock free

Adopted first, and still the check that refutes. Register correspondence (next
section) now runs ahead of it, because this one costs what the *design* costs.

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

**What it costs.** One flat miter over gold and gate, unrolled `2 × sec_depth`
times. On `netproc` and `soc_bench` that is ~100K cells copied twelve times,
and the solver has to prove *every* output equal although the edit touched one
always-block. A broken candidate is refuted fast, since one counterexample
suffices. A correct one does not finish: timed out at 1800 s, or OOM-killed.

### Adopted: register correspondence first

Make the cost follow the size of the edit, not the size of the design.

**The claim.** Pair each register in the candidate with the register of the
same name in the reference. If every pair has the same clock (and polarity),
the same reset activity, the same reset value, and the same next-state
function — as functions of the *paired register values* and the primary
inputs — then the two designs, started in the same state, stay in the same
state after **any** clock event on **any** domain in **any** order, and after
any reset. By induction over events they are equal forever, and so are their
outputs. That is an unbounded claim over every interleaving. It needs no
`clk2fflogic` and no depth, and it is strictly stronger than the twelve-step
bounded check it replaces.

The "same start" is the assumption the bounded miter already made with
`-set-init-zero`. Every register in the shipped benchmarks has an async reset,
whose value is itself one of the checked obligations.

**How it is checked** (`tools/regcorr.py`, `flow/scripts/sec_regcorr.tcl`):

1. Each side is elaborated (`proc; flatten; alumacc`, deliberately no `opt`)
   and written out as JSON. `alumacc` folds every tree of additions into one
   `$macc` cell, identically on both sides, so a serial accumulate and a
   balanced adder tree over the same terms become the same cell up to term
   order.
2. Flop bits are paired by any shared public name at the same bit, accepted
   only when the match is unique in both directions. Aliases matter:
   `wire rot_in = xd_q` makes every bit of `xd_q` also a bit of `rot_in`, and
   Yosys will happily name the flop after either.
3. Every paired flop is cut open: its Q becomes a primary input shared by both
   sides, and `D`, clock (inverted for negedge), reset-active (inverted for
   active-low; constant 0 for a flop with no reset) and reset value become
   outputs. An unpaired flop's Q is left undriven — a free value nothing can
   be proved from.
4. The problem is now purely combinational. An exact structural hash settles
   every output built from identical cells over identical inputs in identical
   order. Nothing boolean is normalised, so `a & b` and `b & a` are *not* the
   same structure; a missed match costs solver time, never soundness. The one
   exception is arithmetic: a `$macc` is hashed by the multiset of its terms,
   because a sum does not depend on the order of its addends, and each plain
   term is compared as its bits extended to the output width (padded with its
   top bit if signed, with 0 if not), because a sum mod 2^width cannot tell a
   signed 32-bit operand from the same operand sign-extended to 40 bits by
   hand. Zero- and sign-extension stay different; subtract flags, every
   operand bit, and a product's operands in order are exact. A `CONFIG` that
   fails to decode falls back to the exact ordered key.
5. Only the cones of outputs that still differ go to `equiv_make` /
   `equiv_simple`. Before they do, every value nothing drives inside a cone
   (an unpaired register's Q, a wire the candidate never drove) and every
   `x`/`z` constant becomes a primary input **unique to its side**; each side
   is padded with the other's as unused ports so the port lists still match.
   A pass requires every `$equiv` proven and a nonzero count. If internal
   names block the proof, it is retried once with only ports matched.

**Why it is not the per-domain partitioner rejected above.** There is no
partitioner to assign a register to the wrong domain. The crossings stay in
the claim: a synchroniser flop is paired and checked like any other, and a
clock divider's Q is exactly what the flops it clocks compare their clock
ports against. Pairing is not trusted either. A wrong pairing produces a false
obligation, which stays unproven, and **when correspondence cannot be found
the result is undecided (`regcorr-unproven`), never a pass**. Nothing here
refutes, since an unproven output may just be a renamed register. The
`clk2fflogic` check above then runs, one at a time, under its own timeout, and
it is what refutes.

**A false pass, found and fixed.** The first `dual_clock` portfolio run with
regcorr promoted a mechanical union, `u13`, that was not equivalent. The
splice kept `f_acc_p <= acc_final` but dropped the hunk that declared and
drove `acc_final`. Verilog's implicit-net rule made it an undriven 1-bit wire
rather than an error, and synthesis deleted the whole accumulate: 1,307 cells
became 103. regcorr sent the cone to the solver correctly, since the
candidate's unused registers were gone, gold's 192 went unpaired, and undriven
bits never hash equal. But **Yosys does not treat an undriven bit as a free
value**: a toy with gold's output undriven and gate's tied to 0 "proves" equal.
So gold's unpaired registers and the candidate's undriven wire compared equal,
and a broken design was certified. Undriven bits and `x`/`z` in a cone are now
side-unique primary inputs (step 5), the toy comes back unproven, and `u13` and
its sibling `u23` are permanent `fail` cases. A scan of all 69 recorded cones
found the defect could have affected exactly five passes: those two, and a
`soc_bench` candidate that duplicates a register (96 free bits a side, in three
runs). On the fixed code that candidate is **undecided** (192 of 507 signals
unproven): its duplicate `sys_b_q_dup` has no name partner, so its earlier pass
was unearned too, and the `soc_bench` promotion that rested on it is withdrawn. The regression toys never exercised an
undriven bit, which is how it got through; the lesson is that "a free value"
has to be made free explicitly, not assumed of the tool.

**Dead ends, measured, so nobody walks them again:**

- `equiv_simple` proves no register output at all, even on a four-flop toy:
  it does not reason through a flop.
- `equiv_induct` does, but in lockstep. Given `a <= x` on `clk`, `b <= x` on
  `clk2`, it happily proves `out = a` equal to `out = b`, which is false the
  moment the clocks differ.
- `equiv_struct -icells` pairs cells by shape. It matched `a & b` against
  `b & a`, and `clk` against `clk2`, producing obligations false by
  construction that block correct candidates. That was the "~6,800 unproven
  for both" of HANDOFF §6.4.
- `expose -evert-dff` cuts flops, but it names the new ports after whichever
  alias the signal map prefers. On `netproc` that is `par_chain[1]`, a wire
  the candidate had deleted, so the port lists never match. Hence doing the
  evert in Python.
- Proving the whole everted design with `equiv_simple` bit by bit is correct
  but slow: ~30 ms per bit, and ~39K bits on `netproc` even when nothing
  changed. Hence the structural hash.
- A 16-term, 260-bit serial accumulate rebuilt as a tree (a real `netproc`
  cleanup candidate) left 261 ports and 5,659 cells for the solver, and
  `equiv_simple` used its whole 300 s budget. ABC's `cec` on the same cones
  (18 MB of BLIF a side) had not finished after ten minutes either. Adder
  rearrangement is hard for SAT at that width; `alumacc` plus order-free
  `$macc` hashing turns it into a structural match, and the candidate proves
  in 8.5 s.

**Evidence** (`tools/secbench.py`, bounded fallback off, so every verdict is
regcorr's own; September 2026, Yosys 0.33). The final elaboration, with
`alumacc` and order-free `$macc` hashing, was re-gated on every row below; an
earlier gate without them gave the same verdicts, except that the `dual_clock`
tree then took 103 s of solver time and the tx-tree candidate timed out.

| case | expected | verdict | time |
|---|---|---|---|
| 6 real `netproc` parity-tree candidates from portfolio runs | pass | **regcorr**, 54,888 of 54,889 outputs structural, ~805 `$equiv` in the cone | 7.8–9.5 s each |
| `netproc` cleanup candidate: parity tree **and** the 16-term tx accumulate as an adder tree | pass | **regcorr**, 54,888 of 54,889 structural | 8.5 s |
| `dual_clock` 8-term adder chain rebuilt as a tree | pass | **regcorr**, all 1,035 outputs structural, no solver | 0.4 s |
| `soc_bench` aux parity chain rebuilt as a reduction XOR (24 multipliers untouched) | pass | **regcorr**, 11,353 of 11,354 outputs structural | 3.3 s |
| `soc_bench`: that reduction missing one bit | fail | unproven | 3.5 s |
| `soc_bench` portfolio candidate: 12-product sys accumulate as a tree, sign-extension moved into the `$macc` (timed out at 300 s before extension-aware hashing) | pass | **regcorr**, all 11,354 outputs structural, no solver | 3.1 s |
| `soc_bench`: that tree with the products zero-extended instead | fail | unproven (solver budget, 300 s) | 300 s |
| `soc_bench` portfolio candidate that duplicates a high-fanout input register (96 gold / 192 candidate flop bits unpaired) | any | undecided (192 of 507 unproven); it passed before the free-value fix, wrongly | 127 s |
| `dual_clock` mechanical unions `u13`, `u23` reading an undeclared `acc_final` | fail | unproven (28 of 28); both passed before the free-value fix | 3 s |
| `netproc`: one XOR of a correct tree flipped to XNOR | fail | unproven | 7 s |
| `netproc`: one bit of `rx_crc`'s async-reset value | fail | unproven | 7 s |
| `netproc`: `l_idx_p` moved from `clk_look` to `clk_look_div2_r` | fail | unproven | 8 s |
| `netproc`: one `cdc_xfrm_from_look` synchroniser stage bypassed | fail | unproven (1 of 512) | 31 s |
| `netproc`: CAM priority index off by one | fail | unproven (solver budget, 300 s) | 300 s |
| `netproc`: two `rx_status` output bits swapped | fail | unproven | 5 s |
| `netproc`: `clk_look_div4_r` re-clocked from `clk_look` | fail | unproven | 5 s |
| `netproc`: that tx adder tree with its final `+` turned into `-` | fail | unproven (solver budget, 300 s) | 300 s |
| `dual_clock`: one `+` in the adder tree turned into `-` | fail | unproven | 4 s |

Toy cases in the same style cover what the benchmarks do not exercise: a
negedge twin, an active-high reset twin, and a register swapped onto a
different clock with an identical next-state function all stay unproven, and
`tools/selftest.py` (`TestRegcorr`) pins each obligation without Yosys.

**Limits.** Retiming moves or renames registers, so those cones stay unproven
and take the bounded path — the agent prompts now ask for register names to
be kept for exactly this reason. A cone whose logic is genuinely hard for SAT
(the CAM priority cascade above) can exhaust the regcorr budget; that is
undecided, not a pass. Latches, set/reset or async-load flops, memories that
were not flattened into registers, and blackboxes make the design unsupported
for regcorr, rather than being guessed at.

**The fallback is limited by memory, not just time.** On a 7.6 GB WSL host
with a 6 GB container cap, the whole-design `clk2fflogic` miter on `netproc`
was OOM-killed (exit 137) for all three mutants tried (XNOR, bit swap, clock
move), one at a time, after one to four minutes each. On such a host a broken
`netproc` candidate therefore ends *undecided* rather than refuted, whatever
the fallback timeout, and `--sec-fallback-timeout 0` reaches the same verdict
without the wait. The fast `soc_bench` refutations in HANDOFF §6.4 were
measured with more memory. The container cap is what keeps an OOM confined to
its own check (`DOCKER_MEM` in the Makefile).

---

## 3. What the code does today

`sec.check()` takes the design's `ClockSet` and routes on it:

| design | route | strongest verdict |
|---|---|---|
| any, text identical after comments/whitespace | no tool | `identical` |
| single clock | miter, induction then bounded | `induction` — unbounded |
| multi clock | register correspondence | `regcorr` — unbounded |
| multi clock, regcorr left something unproven | miter with `clk2fflogic`, one at a time | `bounded` |
| multi clock, `--sec-fallback-timeout 0` and regcorr unproven | stops there | `regcorr-unproven` — undecided |
| multi clock, `engine="eqy"` | declines (`unsupported`) | — |

A decided verdict is cached by the content of both sides, so a union that
recombines texts already checked costs nothing.

**The bounded fallback is always bounded.** With free clocks, temporal
induction converges neither way — measured, not assumed — so it is skipped
rather than run to burn the time budget. And an edge now takes two solver steps
instead of one, so the requested depth is doubled into steps to keep the cycle
count comparable.

eqy still declines: it partitions against a common clock and has no multiclock
mode, so it would answer the wrong question confidently.

Everything the old declining path protected still holds. `unsupported` and
`regcorr-unproven` remain neither a pass nor a refutation, and a timeout is
still `undecided` rather than evidence of inequivalence — which matters more
here than anywhere else, because of the scale below.

## 4. Scale

SEC already hit the 1800 s timeout on a **7,343-cell** single-clock design. At
50K cells, bounded SEC times out routinely.

Two things follow:

- **Cutting is the scaling measure.** Register correspondence cuts at every
  register rather than at domain boundaries, so an obligation is one
  register's next-state cone, and structural hashing removes every obligation
  the edit did not touch. On `netproc` a parity rewrite leaves one of 54,889
  outputs for the solver. This is also why `eqy` is preferred on single-clock
  designs where installed: it partitions further.
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
