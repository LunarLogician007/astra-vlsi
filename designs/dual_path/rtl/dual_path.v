// ---------------------------------------------------------------------------
// dual_path — two unrelated slow datapaths in one module.
//
// This design exists to test *path selection*, not to be realistic. The other
// designs in this repo cannot exercise it: mac_chain reports twenty paths that
// are all one logic cone, and alu32 meets timing. Neither can show whether a
// selector separates independent bottlenecks, because neither has two.
//
// So there are two, sharing only the clock and the reset:
//
//   A  arithmetic   3-tap 16x16 MAC with a *serial* accumulate chain.
//                   Synthesises to a carry-propagation path: a long run of
//                   XOR with AOI/OAI between.
//
//   B  control      40-way priority select written as a serial cascade.
//                   Synthesises to a mux chain: repeated MUX/AND-OR cells,
//                   and a high-fanout `req` broadcast feeding all of them.
//
// The two cones share no signal, no register and no RTL line, and their cell
// mixes are as different as this cell library allows. A selector that reports
// one target here, or two targets that resemble each other, is broken.
//
// Latency is 2 cycles in to out on both paths, and a rewrite must keep that.
// ---------------------------------------------------------------------------
module dual_path #(
    parameter integer W    = 16,
    parameter integer ACC  = 40,
    parameter integer WAYS = 40
) (
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire                    valid_in,

    // ---- datapath A: arithmetic ------------------------------------------
    input  wire signed [W-1:0]     a0, a1, a2,
    input  wire signed [W-1:0]     b0, b1, b2,
    output reg  signed [ACC-1:0]   acc_out,

    // ---- datapath B: control ---------------------------------------------
    input  wire [WAYS-1:0]         req,
    input  wire [8*WAYS-1:0]       bus,
    output reg  [7:0]              sel_out,
    output reg                     sel_hit,

    output reg                     valid_out
);

    // ---- input registers --------------------------------------------------
    reg signed [W-1:0]   a0_q, a1_q, a2_q;
    reg signed [W-1:0]   b0_q, b1_q, b2_q;
    reg [WAYS-1:0]       req_q;
    reg [8*WAYS-1:0]     bus_q;
    reg                  valid_q;

    always @(posedge clk) begin
        if (!rst_n) begin
            a0_q <= 0; a1_q <= 0; a2_q <= 0;
            b0_q <= 0; b1_q <= 0; b2_q <= 0;
            req_q <= {WAYS{1'b0}};
            bus_q <= {(8*WAYS){1'b0}};
            valid_q <= 1'b0;
        end else begin
            a0_q <= a0; a1_q <= a1; a2_q <= a2;
            b0_q <= b0; b1_q <= b1; b2_q <= b2;
            req_q <= req;
            bus_q <= bus;
            valid_q <= valid_in;
        end
    end

    // =======================================================================
    // A — serial accumulate. Chained on purpose: depth 2 adders where a tree
    //     of the same three terms is depth 2 as well, but the *widths* grow
    //     along the chain instead of staying narrow until the end.
    // =======================================================================
    wire signed [2*W-1:0] p0 = a0_q * b0_q;
    wire signed [2*W-1:0] p1 = a1_q * b1_q;
    wire signed [2*W-1:0] p2 = a2_q * b2_q;

    wire signed [ACC-1:0] t0 = {{(ACC-2*W){p0[2*W-1]}}, p0};
    wire signed [ACC-1:0] t1 = t0 + {{(ACC-2*W){p1[2*W-1]}}, p1};
    wire signed [ACC-1:0] t2 = t1 + {{(ACC-2*W){p2[2*W-1]}}, p2};

    // =======================================================================
    // B — 40-way priority select as a serial cascade. Way 0 wins, then 1, and
    //     so on, so every way waits for every lower one: 40 muxes in series.
    //     `req_q` also fans out to all 40 stages at once.
    // =======================================================================
    wire [8*(WAYS+1)-1:0] pri;
    wire [WAYS:0]         hit;

    assign pri[8*WAYS +: 8] = 8'd0;
    assign hit[WAYS]        = 1'b0;

    genvar i;
    generate
        for (i = WAYS-1; i >= 0; i = i - 1) begin : prio
            assign pri[8*i +: 8] = req_q[i] ? bus_q[8*i +: 8]
                                            : pri[8*(i+1) +: 8];
            assign hit[i]        = req_q[i] | hit[i+1];
        end
    endgenerate

    // ---- output registers -------------------------------------------------
    always @(posedge clk) begin
        if (!rst_n) begin
            acc_out   <= {ACC{1'b0}};
            sel_out   <= 8'd0;
            sel_hit   <= 1'b0;
            valid_out <= 1'b0;
        end else begin
            acc_out   <= t2;
            sel_out   <= pri[7:0];
            sel_hit   <= hit[0];
            valid_out <= valid_q;
        end
    end

endmodule
