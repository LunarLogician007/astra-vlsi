```json
{
  "pattern": "Serial accumulator chain and unreachable saturation bounds",
  "strategy": "Rebalance additions into a tree; remove infeasible comparisons",
  "rationale": "Serial chain s0→s1→s2→s3 is 3 add-depths; balanced tree is 2. Saturation bounds (±2^37) exceed max output (±2^32) by 16x, so comparisons always false.",
  "target": "Lines 63–69: s0 through s3 accumulation; over/under comparisons and sat_val selection",
  "changed": true,
  "expected_slack_recovery_ns": 0.8,
  "area_cost": "none — rebalancing uses same operator count; only removes dead comparison logic",
  "equivalence_argument": "Rebalancing: (a+b)+c+d = (a+b)+(c+d) in unbounded arithmetic; no intermediate overflow in 40-bit (max magnitude ~2^32). Reset unaffected (all wires are combinational). Saturation removal: s3 range is ~[−2^32, 2^32]; LIMIT is ±2^37; comparisons provably always yield false, so sat_val defaults to s3 and sat_flag to 0, identical to original behavior."
}
```

```verilog
// ---------------------------------------------------------------------------
// mac_chain — 4-tap 16x16 multiply-accumulate with saturation, all in one
// combinational cycle.
//
// This is the *reference* design for the optimisation loop, and it is slow on
// purpose. The critical path runs:
//
//     input regs -> four 16x16 multipliers -> serial adder chain (not a tree)
//                -> saturation compare -> output register
//
// Three separate structural problems an RTL rewrite can attack — a serial
// accumulate chain instead of an adder tree, an unpipelined multiplier array,
// and a wide comparison sitting at the end of the path rather than in
// parallel with it. That makes it a good target for measuring whether a model
// found a real fix or just moved logic around.
//
// Any rewrite must keep the same latency (2 cycles in -> out) and the same
// function, or the equivalence check will reject it.
// ---------------------------------------------------------------------------
module mac_chain #(
    parameter integer W   = 16,
    parameter integer ACC = 40
) (
    input  wire                clk,
    input  wire                rst_n,
    input  wire                valid_in,
    input  wire signed [W-1:0] a0, a1, a2, a3,
    input  wire signed [W-1:0] b0, b1, b2, b3,
    output reg  signed [ACC-1:0] acc_out,
    output reg                   sat_flag,
    output reg                   valid_out
);

    localparam signed [ACC-1:0] SAT_MAX =  {1'b0, {(ACC-1){1'b1}}};
    localparam signed [ACC-1:0] SAT_MIN = -{1'b0, {(ACC-1){1'b1}}} - 1'b1;
    localparam signed [ACC-1:0] LIMIT   =  40'sd137438953471;

    // ---- input registers --------------------------------------------------
    reg signed [W-1:0] a0_q, a1_q, a2_q, a3_q;
    reg signed [W-1:0] b0_q, b1_q, b2_q, b3_q;
    reg                valid_q;

    always @(posedge clk) begin
        if (!rst_n) begin
            a0_q <= 0; a1_q <= 0; a2_q <= 0; a3_q <= 0;
            b0_q <= 0; b1_q <= 0; b2_q <= 0; b3_q <= 0;
            valid_q <= 1'b0;
        end else begin
            a0_q <= a0; a1_q <= a1; a2_q <= a2; a3_q <= a3;
            b0_q <= b0; b1_q <= b1; b2_q <= b2; b3_q <= b3;
            valid_q <= valid_in;
        end
    end

    // ---- combinational MAC: four multiplies, then accumulate ----
    wire signed [2*W-1:0] p0 = a0_q * b0_q;
    wire signed [2*W-1:0] p1 = a1_q * b1_q;
    wire signed [2*W-1:0] p2 = a2_q * b2_q;
    wire signed [2*W-1:0] p3 = a3_q * b3_q;

    wire signed [ACC-1:0] s0 = {{(ACC-2*W){p0[2*W-1]}}, p0};
    wire signed [ACC-1:0] p1_ext = {{(ACC-2*W){p1[2*W-1]}}, p1};
    wire signed [ACC-1:0] p2_ext = {{(ACC-2*W){p2[2*W-1]}}, p2};
    wire signed [ACC-1:0] p3_ext = {{(ACC-2*W){p3[2*W-1]}}, p3};

    wire signed [ACC-1:0] s1 = s0 + p1_ext;
    wire signed [ACC-1:0] s2 = p2_ext + p3_ext;
    wire signed [ACC-1:0] s3 = s1 + s2;

    // ---- saturation sits at the very end of the path ----------------------
    wire signed [ACC-1:0] sat_val = s3;

    always @(posedge clk) begin
        if (!rst_n) begin
            acc_out   <= {ACC{1'b0}};
            sat_flag  <= 1'b0;
            valid_out <= 1'b0;
        end else begin
            acc_out   <= sat_val;
            sat_flag  <= 1'b0;
            valid_out <= valid_q;
        end
    end

endmodule
```