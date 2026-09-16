---
name: rtl-timing-optimization
description: >-
  Structural Verilog transformations that recover setup slack without changing
  function, latency or interface. Use when rewriting RTL against a measured
  critical path.
---

# RTL timing optimisation

Recovering setup slack means removing logic levels between two flops. Nothing
else here matters as much as that sentence: a transformation that moves gates
around without shortening the longest path from a launch flop to a capture
flop has not helped, however much better the code reads afterwards.

## The invariants
<!-- roles: cleanup specialist merge -->

Checked by tools after you answer, so there is nothing to gain by bending them:

1. **Functional equivalence.** A sequential equivalence check runs against the
   original design. Bit-exact outputs, cycle for cycle. A rewrite that changed
   behaviour is discarded no matter how much slack it recovered.
2. **Latency and interface are fixed.** Same module name, ports, widths,
   pipeline depth. You may redistribute or duplicate registers. You may not
   add or remove a pipeline stage. Keep existing register names and rename or
   add intermediate wires freely: the equivalence check pairs registers by
   name, so an edit that keeps them is proved in seconds, and one that moves
   or renames a register takes a far slower check that may not finish.
3. **Synthesisable Verilog-2005.** No initial blocks, no delays, no testbench
   constructs.

## What synthesis already does -- doing it again wastes the iteration
<!-- roles: cleanup specialist -->

Modern synthesis performs these before you see a timing report. Redoing them in
RTL changes nothing and costs an equivalence proof and a synthesis run:

- **Constant folding and propagation.** A cell with constant inputs is already
  replaced by its value.
- **Common subexpression elimination and identical-cell merging.** Writing an
  expression once into a temporary is not an optimisation; the tool already
  merged the duplicates.
- **Boolean minimisation and technology mapping.** Restating logic with
  different operators -- De Morgan, `&`/`|` swaps, `~^` for `^~` -- produces
  the same gates. The tool canonicalises before mapping.
- **Trivial resource sharing.** Two adders in mutually exclusive branches are
  already shared where it is profitable.
- **Retiming**, where the flow enables it: registers are moved across
  combinational logic automatically.
- **Buffer insertion and gate sizing** for load and fanout.

What synthesis cannot do is change the *algorithm*. It will not turn a serial
chain into a tree, will not decide that a comparison is decidable from earlier
values, and will not restructure a datapath into a redundant number
representation. Those require knowing what the code means. That is where you
add value, and it is the only place you do.

## Where an LLM is reliable at this, and where it is not
<!-- roles: cleanup specialist -->

Published evaluations of LLM-driven RTL optimisation find the ability is
uneven, and it is worth knowing which side of the line you are on:

- **Reliable:** datapath restructuring, arithmetic reassociation, multiplexer
  and select-network rework, Boolean simplification. Models match or beat
  classical tooling here.
- **Unreliable:** finite-state-machine restructuring -- redundant and
  pass-through states are consistently left in place -- and anything crossing
  a clock domain, where models tend to add synchroniser complexity rather than
  remove it, and can break the design outright.

So: prefer datapath work. If the target is an FSM or a clock-domain crossing,
be conservative, and prefer returning the file unchanged over a rewrite you
cannot argue precisely.

## How to read a critical path
<!-- roles: specialist -->

Instance names in an OpenSTA report (`_14637_`, or a chain like
`acc_out[33]_DFF_X1_Q_D_AOI21_X1_ZN`) are synthesis-generated and carry no
design meaning. The *sequence of cell types* does:

| what you see | what it is |
|---|---|
| a wall of AND/OR feeding a wide reduction | partial-product generation, or a comparison |
| a long run of XOR with AOI/OAI between | carry propagation through an adder |
| repeated MUX cells in series | a priority chain or a select cascade |
| one stage with fanout in the tens | a net driving a whole datapath |
| delay spread evenly over many stages | the depth itself is the problem, not any cell |

Where the delay sits decides which transformation applies. A path with its
delay concentrated in a few of many stages has a local fix; one where it is
spread evenly does not, and needs the structure changed.

## What reliably works
<!-- roles: cleanup specialist -->

Ordered by how often it recovers real slack.

**Rebalance a serial chain into a tree.** `((a+b)+c)+d` is three adders deep;
`(a+b)+(c+d)` is two. For n terms the depth falls from n-1 to ceil(log2 n), and
the gain grows with every term. The single most productive rewrite on
accumulate and reduction logic.

**Speculate, then select late.** The canonical critical path in a pipelined
processor is not the arithmetic alone -- it is a select network *in series
with* it: hazard detection, then operand forwarding, then the ALU, one after
another. The fix is to compute every candidate result in parallel and choose
between them with one late mux, so the selection is off the arithmetic path
instead of in front of it. The same shape appears as carry-select adders and
as branch-condition speculation.

**Move late work off the path.** If a condition is computed *after* a long
arithmetic result and then used to select against it, derive it beside the
arithmetic from the partial results instead. A saturation clamp at the end of a
wide accumulate is the textbook case: overflow is decidable from the operands,
not only from the sum.

**Carry-save intermediate sums.** Every full-width add resolves a carry chain.
Keeping a running total as a redundant (sum, carry) pair makes each
intermediate add a constant-depth 3:2 compression, and pays for one
carry-propagate adder at the very end instead of one per term. A multi-operand
expression should reduce through a single compression tree with one final
adder. The depth is the number of 3:2 reducers a signal passes, not the number
of additions written.

**Replicate a high-fanout driver.** A net driving many loads needs a large
driver or a buffer tree, and both sit on the launching path. Duplicating the
logic that produces it splits the load without changing the value. Register
high-fanout control, enable and reset signals rather than fanning out
combinational logic.

**Narrow a comparison to the bits that decide it.** A wide compare against a
constant usually turns on a handful of high bits, especially when the constant
is at or near a power of two. So does an equality test against a sparse
constant.

**Flatten a priority cascade.** A chain of `?:`, or a `for` loop where each
iteration consumes the previous one, is a mux chain as deep as the loop trip
count. Decoding the selects in parallel and reducing with a balanced tree
makes it logarithmic.

**Retime across registers that already exist.** Where a pipeline has stages,
work can be moved between them. Latency must not change: this redistributes
logic across existing flops, it does not add one.

## Coding patterns that block the tool's own datapath optimisation
<!-- roles: cleanup specialist -->

These are not slow in themselves -- they stop synthesis from applying the
architecture-level optimisations it would otherwise apply to an arithmetic
chain, which is worse, because the loss is invisible in the source:

- **A truncated intermediate.** Assigning `a*b + c` to a narrower wire and
  widening again later splits one datapath into two, and each is optimised
  alone. Widen the intermediate to the width the arithmetic actually produces.
- **A register inside the datapath.** A flop between two operators prevents
  them being merged. Put the register at the *output* of the arithmetic and
  let retiming place it, rather than pipelining by hand mid-chain.
- **Mixed signedness in one expression.** Signed and unsigned operands force
  separate operator implementations. Make the intent explicit and consistent.
- **Arithmetic split across a module boundary.** An operator chain that crosses
  hierarchy cannot be merged into one block.
- **An explicitly instantiated arithmetic component.** A hand-instantiated
  adder or multiplier is a black box; the inferred operator lets the tool pick
  an architecture suited to its context.

One caveat, and it matters here: how much of this applies depends on the
synthesis tool. A flow with a real datapath extractor gains a great deal from
these; a simpler open-source flow may not merge operators at all, and there
*narrowing* an intermediate to the width the data provably occupies can be the
winning move rather than the mistake. Trust the measurement over the rule.

## What wastes an iteration
<!-- roles: cleanup specialist merge -->

- Renaming signals, reordering independent statements, adding attributes.
- Restating the same logic with different operators.
- Reformatting. The file is combined with other agents' edits by splicing
  text; a reflowed file cannot be combined with anything and is discarded.
- Attacking a construct that is not on the critical path. An area saving
  elsewhere buys no slack here.
- Adding a pipeline stage. It fails the equivalence check, and costs a proof
  to find that out.

## Arguing equivalence
<!-- roles: cleanup specialist merge -->

Every rewrite needs the reason its outputs are bit-identical. Be specific about
the two things that actually break rewrites:

- **Reset.** Does the restructured logic reset to the same values, in the same
  cycle?
- **Width and sign.** Reassociating a sum moves where intermediates are
  truncated. `(a+b)+c` and `a+(b+c)` are equal in unbounded arithmetic and can
  differ in fixed width if an intermediate overflows. Widen intermediates, or
  state why they cannot overflow. Sign extension before a reassociated add is
  a frequent source of a failed check.

## Transformation catalogue
<!-- roles: cleanup specialist -->

An index, not a reference: whichever of these match your target are supplied in
full alongside it, with their rationale and their measured record. **The
entries carry no evidence on their own** -- the loop records how each actually
fares, so a later run can tell which work on real designs.


| pattern | transformation |
|---|---|
| Carry propagation in 40-bit final accumulate | No equivalent optimization available |
| Carry propagation in accumulator tree | Replace multi-level add tree with carry-save 3:2 compression |
| High-fanout multiplier operand from single register | Replicate operand register to reduce fanout cone |
| High-fanout operand driving multiplier with 38+ loads | Replicate operand registers to split fanout |
| Intermediate sign-extension wires in accumulator tree | Inline sign-extension concatenations into summation expressions |
| Multi-level addition tree in accumulator | Replace tree with carry-save 3:2 compression |
| Ripple carry propagation in 40-bit final accumulator | Replace ripple carry with carry-save 3:2 compression tree |
| Sign-extension through intermediate wires in adder tree | Inline sign-extension expressions to reduce intermediate nodes |
| arithmetic operator chain split across a module boundary | keep the whole chain in one hierarchy so it can merge |
| carry propagation across a wide accumulator | accumulate in carry-save form and resolve with a single carry-propagate adder |
| carry propagation in sequential 40-bit additions | unable to reduce depth safely while maintaining equivalence |
| deep FSM or decode logic | pre-decode the next state one cycle early and register the decoded form |
| hand-instantiated arithmetic component | use the inferred operator so the tool picks the architecture |
| high-fanout operand driving 16x16 multiplier | register replication already attempted; insufficient margin |
| high-fanout signal on the critical path | replicate the driver so each load group has its own copy |
| intermediate arithmetic result truncated between operators | widen the intermediate so the operator chain stays one datapath |
| long path through an operand shared by many consumers | isolate the operand per consumer so an idle consumer stops loading it |
| loop-carried dependency in a generate loop | reduce with a balanced tree instead of a linear scan |
| mixed signed and unsigned operands in one expression | make signedness explicit and consistent across the expression |
| mux-heavy selection logic | flatten the serial mux chain into a single balanced select |
| priority encoder or leading-one chain | reduce with a balanced tree instead of a linear priority scan |
| register placed inside an arithmetic chain | register the datapath output and let retiming place it |
| reset logic in series with the datapath | move the reset out of the data path and onto the register's own reset |
| saturation or clamp after a long arithmetic path | form the clamp condition beside the arithmetic and apply it as a select, not as a compare-then-select |
| select network in series with a long arithmetic path | speculate every outcome in parallel and select at the end |
| serial accumulate chain | rebalance the chain into an adder tree |
| sign extension replicated before every operand of a wide add | add the narrow values and sign-extend once at the end |
| unpipelined multiplier array in one cycle | redistribute the existing registers across the array without changing the latency |
| wide comparison at the end of the datapath | precompute the comparison from partial results in parallel with the final arithmetic |

## Choosing among them
<!-- roles: specialist -->

Prefer the transformation that removes the most logic levels from the *measured*
path. When two are comparable, prefer the one whose equivalence argument is
easier to make: a rewrite that cannot be proven equivalent is worth nothing,
and the proof is run on every candidate.
