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

    // Balanced tree over the TAPS = 8 terms: depth 3 adders instead of 8.
    wire signed [2*W+3:0] add_l1_0 = term[0] + term[1];
    wire signed [2*W+3:0] add_l1_1 = term[2] + term[3];
    wire signed [2*W+3:0] add_l1_2 = term[4] - term[5];
    wire signed [2*W+3:0] add_l1_3 = term[6] + term[7];
    wire signed [2*W+3:0] add_l2_0 = add_l1_0 + add_l1_1;
    wire signed [2*W+3:0] add_l2_1 = add_l1_2 + add_l1_3;
    wire signed [2*W+3:0] acc_tree = add_l2_0 + add_l2_1;

    reg signed [2*W+3:0] f_acc_p;
    always @(posedge clk_fast or negedge rst_n)
        if (!rst_n) f_acc_p <= {(2*W+4){1'b0}};
        else if (f_valid_q) f_acc_p <= acc_tree;

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
