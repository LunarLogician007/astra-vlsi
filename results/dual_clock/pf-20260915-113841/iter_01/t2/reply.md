Looking at your target **T2**, I can see the bottleneck clearly: a serial accumulator chain summing 8 terms one after another. With TAPS=8, the current code does:

```
acc[0] = term[0]
acc[1] = acc[0] + term[1]
acc[2] = acc[1] + term[2]
...
acc[7] = acc[6] + term[7]
```

This creates 7 adders in series, each resolving a full ripple carry. The cell walk confirms this—20 alternating AOI/OAI gates are carry propagation through multiple adder depths.

Restructuring into a **balanced binary tree** reduces the depth from 7 to 3 levels:
- Level 0 (parallel): 4 adds → acc01, acc23, acc45, acc67
- Level 1 (parallel): 2 adds → acc0123, acc4567  
- Level 2: 1 add → final result

All 8 terms still sum to the same value—addition is associative—and no overflow occurs because 8 × 4K fits comfortably in 28 bits. The latency stays 3 cycles: shift stages 0–1, tree sum combinationally in stage 2, register in stage 3. Only the combinational depth shrinks.

```json
{
  "pattern": "Serial adder chain in 8-term accumulate",
  "strategy": "Rebalance into a binary tree to reduce depth",
  "rationale": "8 terms summed serially creates 7 adders cascaded, each with ripple carry delay. A binary tree (acc01+acc23+acc45+acc67 level 0 → acc0123+acc4567 level 1 → final level 2) achieves the same sum in 3 levels instead of 7, cutting combinational path depth by 4+ adder stages.",
  "target": "Lines 113–122: replaced serial `acc[g] = acc[g-1] + term[g]` loop with parallel tree using intermediate wires acc01, acc23, acc45, acc67, acc0123, acc4567, acc_final",
  "changed": true,
  "expected_slack_recovery_ns": 0.12,
  "area_cost": "none — same 7 adders, restructured for better synthesis and lower depth",
  "equivalence_argument": "Addition is associative: (a+b)+(c+d)+(e+f)+(g+h) = a+b+c+d+e+f+g+h = serial sum. Fixed 28-bit width at every intermediate avoids overflow issues (8×4K max is well within 28 bits). Reset: all wires reset to 0 naturally. Latency: 3-cycle pipeline depth preserved; tree computation is still combinational in cycle 2."
}
```

```verilog
// FILE: designs/netproc/rtl/dual_clock.v
// ---------------------------------------------------------------------------
// dual_clock — the smallest design on which the multi-clock loop can close.
//
// soc_bench is the objective-6 benchmark and is deliberately large; at ~50K
// cells with 24 multipliers a bounded SEC can refute a bad candidate quickly
// but cannot prove a good one in reasonable time (HANDOFF.md §6.4). So it
// demonstrates selection and rejection, and cannot demonstrate *promotion*.
//
// This design exists to close that gap, and nothing else. Two asynchronous
// domains, one generated clock, one clock-domain crossing, and one honest
// timing bottleneck per domain — small enough that `clk2fflogic` equivalence
// finishes in seconds, so a candidate can actually be proved and promoted.
//
// Deliberately NO MULTIPLIERS. A bounded check unrolls the design once per
// solver step, and unpipelined multipliers make that intractable: on a version
// of this design with four 12x12 multipliers, a broken candidate was refuted
// in 9 s but a good one could not be proved in 900 s. The bottleneck here is a
// serial adder chain instead — the same "serial where a tree belongs" fix,
// without the arithmetic that stops the proof closing.
//
// It is NOT a substitute for soc_bench: one crossing, no FSM, no divider
// ratios beyond 2, and three orders of magnitude too few cells.
//
// LATENCY CONTRACT: fast 3 cycles of clk_fast, slow 2 of clk_slow.
//
// DO NOT EDIT the cdc_ synchroniser or the divider — see
// docs/equivalence-contract.md. `protected` in config.json enforces it.
// ---------------------------------------------------------------------------
module dual_clock #(
    parameter integer W    = 12,
    parameter integer TAPS = 8,
    parameter integer SEL  = 24
) (
    input  wire                   clk_fast,
    input  wire                   clk_slow,
    input  wire                   rst_n,

    input  wire                   f_valid,
    input  wire signed [W-1:0]    f_a,
    input  wire signed [W-1:0]    f_b,
    output reg  signed [2*W+3:0]  f_acc,

    input  wire [SEL-1:0]         s_req,
    output reg  [4:0]             s_grant,
    output reg                    s_any
);

    // ---- clock divider (off limits) ---------------------------------------
    // A pure toggle flop, so `autoname` keeps the instance name on the Q net
    // and the SDC can hang a generated clock on it. See HANDOFF.md §7.
    reg clk_fast_div2_r;
    always @(posedge clk_fast or negedge rst_n)
        if (!rst_n) clk_fast_div2_r <= 1'b0;
        else        clk_fast_div2_r <= ~clk_fast_div2_r;
    wire clk_fast_div2 = clk_fast_div2_r;

    // ---- CDC (off limits) --------------------------------------------------
    // Two-flop synchroniser, slow domain -> fast domain.
    reg [1:0] cdc_fast_from_slow;
    always @(posedge clk_fast or negedge rst_n)
        if (!rst_n) cdc_fast_from_slow <= 2'b0;
        else        cdc_fast_from_slow <= {cdc_fast_from_slow[0], s_any};

    // =======================================================================
    // FAST DOMAIN — bottleneck: TAPS terms summed by a serial adder chain
    // instead of a balanced tree, so the depth is TAPS instead of log2(TAPS).
    // =======================================================================
    reg signed [W-1:0] fa_q [0:TAPS-1];
    reg signed [W-1:0] fb_q [0:TAPS-1];
    reg                f_valid_q;

    integer i;
    always @(posedge clk_fast or negedge rst_n) begin
        if (!rst_n) begin
            for (i = 0; i < TAPS; i = i + 1) begin
                fa_q[i] <= {W{1'b0}};
                fb_q[i] <= {W{1'b0}};
            end
            f_valid_q <= 1'b0;
        end else begin
            fa_q[0] <= f_a;
            fb_q[0] <= f_b;
            for (i = 1; i < TAPS; i = i + 1) begin
                fa_q[i] <= fa_q[i-1];
                fb_q[i] <= fb_q[i-1];
            end
            f_valid_q <= f_valid;
        end
    end

    // Deliberately serial: each add waits on the one before it, so the depth
    // is TAPS adders rather than log2(TAPS). Balancing it into a tree is the
    // fix, and it changes neither latency nor the result.
    wire signed [2*W+3:0] term [0:TAPS-1];
    genvar g;
    generate
        for (g = 0; g < TAPS; g = g + 1) begin : g_term
            assign term[g] = {{(W+4){fa_q[g][W-1]}}, fa_q[g]}
                           + {{(W+4){fb_q[g][W-1]}}, fb_q[g]};
        end
    endgenerate

    wire signed [2*W+3:0] acc01, acc23, acc45, acc67;
    assign acc01 = term[0] + term[1];
    assign acc23 = term[2] + term[3];
    assign acc45 = term[4] + term[5];
    assign acc67 = term[6] + term[7];

    wire signed [2*W+3:0] acc0123, acc4567;
    assign acc0123 = acc01 + acc23;
    assign acc4567 = acc45 + acc67;

    wire signed [2*W+3:0] acc_final;
    assign acc_final = acc0123 + acc4567;

    reg signed [2*W+3:0] f_acc_p;
    always @(posedge clk_fast or negedge rst_n)
        if (!rst_n) f_acc_p <= {(2*W+4){1'b0}};
        else if (f_valid_q) f_acc_p <= acc_final;

    // Output stage on the generated clock: half the frequency budget of
    // everything above it, which is why each path is ranked against its own.
    always @(posedge clk_fast_div2 or negedge rst_n)
        if (!rst_n) f_acc <= {(2*W+4){1'b0}};
        else f_acc <= f_acc_p + {{(2*W+3){1'b0}}, cdc_fast_from_slow[1]};

    // =======================================================================
    // SLOW DOMAIN — bottleneck: a SEL-way priority select written as a serial
    // mux cascade, so bit N waits on every bit below it. A tree is the fix.
    // =======================================================================
    wire [4:0] sel_chain [0:SEL];
    wire       any_chain [0:SEL];
    assign sel_chain[0] = 5'd0;
    assign any_chain[0] = 1'b0;
    generate
        for (g = 0; g < SEL; g = g + 1) begin : g_sel
            assign any_chain[g+1] = any_chain[g] | s_req[g];
            assign sel_chain[g+1] = any_chain[g] ? sel_chain[g]
                                  : (s_req[g] ? g[4:0] : sel_chain[g]);
        end
    endgenerate

    always @(posedge clk_slow or negedge rst_n)
        if (!rst_n) begin
            s_grant <= 5'd0;
            s_any   <= 1'b0;
        end else begin
            s_grant <= sel_chain[SEL];
            s_any   <= any_chain[SEL];
        end

endmodule
```