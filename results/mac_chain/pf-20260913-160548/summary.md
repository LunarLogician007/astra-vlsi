# Path-portfolio optimisation -- mac_chain

run           pf-20260913-160548
target clock  2 ns
stage         sta
iterations    1 (converged at n/a)
targets       up to 3 per iteration
weights       alpha=0.5 beta=0.35 gamma=0.15

| metric | D_0 | best | change |
|---|---|---|---|
| WNS (ns) | -0.3573 | -0.1661 | +53.5% |
| TNS (ns) | -3.7438 | -1.6220 | +56.7% |
| area (um^2) | 8942.9200 | 8950.3680 | +0.1% |
| cells | 7343 | 7333 | -0.1% |
| Eq. 3 score | 0.0000 | -0.4658 | |

SEC pass rate 1/3 (33% of decided)  (specialists)

SEC: no candidate reached a verdict (0 attempted)  (unions)

## Pre-synthesis pass

adopted `clean0` (score -0.4658)

## Portfolio per iteration

### Iteration 1

3 of 3 target(s) from 5 distinct cone(s); one cone, cut into segments

| target | kind | value | P(limits clock) | severity | delay share | cell mix |
|---|---|---|---|---|---|---|
| T1 | segment | 0.418 | 100% | 0.54 | 17% | 2x flop, 2x aoi_oai, 2x and_or |
| T2 | segment | 0.499 | 100% | 0.54 | 32% | 22x and_or |
| T3 | segment | 0.590 | 100% | 0.54 | 51% | 52x aoi_oai, 2x nand_nor, 2x and_or |

| candidate | pattern -> strategy | SEC | WNS | area | score |
|---|---|---|---|---|---|
| t1 | high-fanout operand register driving multiplier -> replicate input register, split multiplier bits across copies | FAIL | n/a | n/a | n/a |
| t2 **<-** | 40-bit saturation comparison after accumulation -> Remove unused saturation parameters to prevent inadvertent synthesis | pass | -0.1661 | 8950.3680 | -0.4658 |
| t3 | Carry propagation in accumulator tree -> Replace multi-level add tree with carry-save 3:2 compression | FAIL | n/a | n/a | n/a |

## Did the merge agent earn its call?

Negative means the agent's union beat the one assembled by text splicing for free; positive means it did not.

| iteration | agent union | best mechanical | delta |
|---|---|---|---|
| 1 | n/a | | |

**Scope violations: 3/3.** Specialists edited RTL outside their assigned target this often. A high rate here means the merge stage's premise -- that the candidates edit disjoint text -- does not hold on this design.

## Skill library after this run

```
- [PROVISIONAL] High-fanout operand driving multiplier with 38+ loads  ->  Replicate operand registers to split fanout
    why: A single register driving 38 gates requires large width and exhibits heavy transitional delay. Two identical registers halve the per-register load; synthesis can distribute the multiplier inputs across both copies, reducing driver-to-load capacitance and slew on each path through the Wallace tree.
    record: 9 decided use(s), SEC pass 7/9, confidence 0.44, mean advantage -0.579, seen on mac_chain
- [ATTEMPTED] carry propagation in sequential 40-bit additions
    a previous run tried this and recorded: unable to reduce depth safely while maintaining equivalence
    NOTE: that is an outcome, not a transformation. It does not mean the pattern is unfixable -- read the record below and decide for yourself.
    why: Three 40-bit additions (s0+s1, s2+s3, then those two) form 22-stage carry chains. Parallel prefix or carry-save approaches either require hand-coded complex adders (no synthesis guarantee) or change overflow truncation behavior, breaking bit-exact equivalence. Reordering violates the 2-cycle latency constraint.
    record: 1 decided use(s), SEC pass 1/1, confidence 0.21, mean advantage -0.890, seen on mac_chain
- [ATTEMPTED] Carry propagation in 40-bit final accumulate
    a previous run tried this and recorded: No equivalent optimization available
    NOTE: that is an outcome, not a transformation. It does not mean the pattern is unfixable -- read the record below and decide for yourself.
    why: Carry-save 3:2 compression was attempted in iteration 1, SEC-passed but synthesized to slower hardware. The balanced tree structure is already standard practice. Ripple carry depth is inherent to the 4-term 40-bit sum without algorithmic restructuring.
    record: 1 decided use(s), SEC pass 1/1, confidence 0.20, mean advantage -0.707, seen on mac_chain
- [PROVISIONAL] 40-bit saturation comparison after accumulation  ->  Remove unused saturation parameters to prevent inadvertent synthesis
    why: The SAT_MAX, SAT_MIN, and LIMIT parameters are defined but never used in the RTL. The sat_flag is always 0, and sat_val is just s3 with no actual saturation logic. These unused parameters may trigger synthesis to generate unnecessary comparison logic on the critical path.
    record: 2 decided use(s), SEC pass 2/2, confidence 0.20, mean advantage +0.000, seen on mac_chain
- [PROVISIONAL] Serial 4-term adder chain instead of balanced tree  ->  Restructure accumulate as binary tree to parallelize carry
    why: Original s3 = ((s0+p1)+p2)+p3 chains three 40-bit ripple carries in series, a critical depth-3 dependency. Rebalanced s3 = (s01||s23) + s3 runs p0+p1 and p2+p3 in parallel, then combines them, reducing critical carry-propagation path from 3 sequential adders to 2, cutting depth by ~33%. The carry chain depth is the dominant delay in stages 50-91, so this directly shortens the path.
    record: 2 decided use(s), SEC pass 2/2, confidence 0.20, mean advantage +0.000, seen on mac_chain
- [ATTEMPTED] 40-bit carry propagation in balanced adder tree
    a previous run tried this and recorded: Unable to optimize while maintaining fixed latency
    NOTE: that is an outcome, not a transformation. It does not mean the pattern is unfixable -- read the record below and decide for yourself.
    why: The 40-bit additions require full carry propagation. A balanced tree (2 levels) is already optimal for this width without adding pipeline stages. Carry-save would add complexity and isn't safely equivalent given the constraints.
    record: 1 decided use(s), SEC pass 1/1, confidence 0.12, mean advantage +0.000, seen on mac_chain
- [ATTEMPTED] 40-bit carry ripple in balanced accumulator tree
    a previous run tried this and recorded: No further transformation valid under constraints
    NOTE: that is an outcome, not a transformation. It does not mean the pattern is unfixable -- read the record below and decide for yourself.
    why: RTL already uses optimal balanced tree; carry-save compression was attempted and synthesized slower; delay spread evenly offers no local fix point; 2-cycle latency constraint prevents pipelining or topology restructuring.
    record: 1 decided use(s), SEC pass 1/1, confidence 0.12, mean advantage +0.000, seen on mac_chain
- [PROVISIONAL] Serial accumulation chain with 4 operands  ->  Rebalance serial add chain into binary tree
    why: Three-stage ripple-carry chain (3 adders deep) becomes two stages (2 adders deep) by computing p0+p1 and p2+p3 in parallel, then combining. Each 40-bit adder carries ~5-8 gate delays; removing one stage recovers ~0.4 ns on the critical path and closes the -0.3573 ns slack shortfall.
    record: 1 decided use(s), SEC pass 1/1, confidence 0.12, mean advantage +0.000, seen on mac_chain
- [PROVISIONAL] Carry propagation in accumulator tree  ->  Replace multi-level add tree with carry-save 3:2 compression
    why: Current balanced tree synthesizes to serial carry-propagation: two 40-bit adds (s01, s23) feed a third. Carry-save reduces three adds (depth 2x single-add) to one: a constant-depth 3:2 compressor producing (sum, carry) pair, followed by one final carry-propagate add. Depth halves.
    record: 6 decided use(s), SEC pass 5/6, confidence 0.03, mean advantage +1.383, seen on mac_chain
- [PROVISIONAL] Sign-extension through intermediate wires in adder tree  ->  Inline sign-extension expressions to reduce intermediate nodes
    why: The current design explicitly creates four 40-bit sign-extended versions (s0–s3) of the 32-bit products before adding them. These intermediate wires add routing delay and potential gate duplication. Inlining the sign-extension concatenations directly into the addition expressions eliminates the intermediate nodes, allowing synthesis to better optimize the critical datapath through the accumulator tree.
    record: 2 decided use(s), SEC pass 2/2, confidence 0.02, mean advantage +1.414, seen on mac_chain
- [UNTESTED] arithmetic operator chain split across a module boundary  ->  keep the whole chain in one hierarchy so it can merge
    why: Hierarchy is a hard boundary for operator merging. A multiply in one module feeding an add in its parent cannot become a single compression tree, however obviously the two belong together.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] carry propagation across a wide accumulator  ->  accumulate in carry-save form and resolve with a single carry-propagate adder
    why: Each full-width add resolves a carry chain. Keeping the running total as a redundant (sum, carry) pair makes every intermediate add a constant-depth 3:2 compression, and only the last step pays for a carry chain.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] deep FSM or decode logic  ->  pre-decode the next state one cycle early and register the decoded form
    why: Decoding a binary state register into control signals puts a decoder in series with everything it controls. Registering the decoded (one-hot) form moves the decode off the consuming path without adding latency, because the decode of the *next* state is computable this cycle.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [PROVISIONAL] Deep multi-level ripple adder chain in accumulator  ->  Replace serial ripple levels with carry-save 3:2 compression
    why: Three cascaded 40-bit ripple additions (52 AOI/OAI cells, ~66 gate stages) was targeted for replacement with constant-depth 3:2 carry-save compression using XOR for sum and majority for carry. However, this breaks equivalence, suggesting the custom accumulation logic depends on intermediate carry propagation patterns or the accumulator state update rule is not commutative with carry representation.
    record: 1 decided use(s), SEC pass 0/1, confidence 0.00, seen on mac_chain
- [UNTESTED] hand-instantiated arithmetic component  ->  use the inferred operator so the tool picks the architecture
    why: An explicitly instantiated adder or multiplier is a black box with a fixed architecture. The inferred operator lets synthesis choose one suited to its context and merge it with its neighbours.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] high-fanout signal on the critical path  ->  replicate the driver so each load group has its own copy
    why: A net driving many loads needs a big driver or a buffer tree; both cost delay on the launching path. Duplicating the logic that produces it splits the load without changing the value.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] intermediate arithmetic result truncated between operators  ->  widen the intermediate so the operator chain stays one datapath
    why: Assigning a product-plus-sum to a narrower wire and widening again downstream splits one arithmetic block into two, each optimised alone, so no compression tree spans them. Tool-dependent: a flow without a datapath extractor gains nothing here, and there narrowing to the width the data provably occupies can instead be the winning move.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] long path through an operand shared by many consumers  ->  isolate the operand per consumer so an idle consumer stops loading it
    why: Operand isolation removes both switching power and capacitive load from the shared net, shortening the path that drives it.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] loop-carried dependency in a generate loop  ->  reduce with a balanced tree instead of a linear scan
    why: A generate loop whose body consumes the previous iteration's output elaborates into a chain as deep as the trip count. A priority cascade, a ripple carry and a shift chain are all written this way. The source shows one assignment and synthesis produces N levels, which is why reading the code does not reveal it.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] mixed signed and unsigned operands in one expression  ->  make signedness explicit and consistent across the expression
    why: A signed subexpression combined with an unsigned one forces two operator implementations where one would serve, and puts conversion logic on the path between them.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] mux-heavy selection logic  ->  flatten the serial mux chain into a single balanced select
    why: Nested conditionals synthesise to a chain of 2:1 muxes with depth equal to the nesting. A one-hot or case-based select of the same conditions gives one balanced level of selection instead.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] priority encoder or leading-one chain  ->  reduce with a balanced tree instead of a linear priority scan
    why: A linear priority chain has depth proportional to width. A tree reduction that combines (found, index) pairs pairwise has logarithmic depth and the same result.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] register placed inside an arithmetic chain  ->  register the datapath output and let retiming place it
    why: A flop between two operators stops them being merged, so each is implemented separately and no carry architecture can be chosen across the pair. Registering the end of the chain and letting retiming move the boundary gives the same latency and a better structure.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] reset logic in series with the datapath  ->  move the reset out of the data path and onto the register's own reset
    why: A reset ANDed into the data expression puts a gate in series with the whole datapath. Using the flop's reset port keeps the datapath clean.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] saturation or clamp after a long arithmetic path  ->  form the clamp condition beside the arithmetic and apply it as a select, not as a compare-then-select
    why: A clamp written as compare-then-mux serialises three things: the arithmetic, the comparison, and the selection. The condition usually depends on high bits and overflow flags that the arithmetic produces before its low bits settle.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] select network in series with a long arithmetic path  ->  speculate every outcome in parallel and select at the end
    why: The canonical critical path in a pipelined datapath is not the arithmetic alone but a selection standing in front of it: hazard detection, then operand forwarding, then the ALU, in series. Computing each candidate result concurrently and choosing with one late multiplexer takes the selection off the arithmetic path. A carry-select adder exploits the same shape.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] sign extension replicated before every operand of a wide add  ->  add the narrow values and sign-extend once at the end
    why: Sign-extending each product to the accumulator width before adding makes every adder full-width. Adding at the natural width and extending the single result gives the same value with narrower carry chains.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] unpipelined multiplier array in one cycle  ->  redistribute the existing registers across the array without changing the latency
    why: When the module already has registers before and after the multiply, the boundary between them can be moved into the array -- retiming -- so each stage carries less depth. Total latency is unchanged, which is what the equivalence check requires.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
- [UNTESTED] wide comparison at the end of the datapath  ->  precompute the comparison from partial results in parallel with the final arithmetic
    why: A compare that waits for the full sum adds its own carry chain on top of the datapath's. The deciding bits are usually available earlier -- the sign and the bits above the limit -- so the condition can be formed beside the sum instead of after it.
    record: 0 decided use(s), SEC pass 0/0, confidence 0.00
```

Model calls: 5 (planned 6), in 45 + cache 27825, out 58350, notional $0.4286.


