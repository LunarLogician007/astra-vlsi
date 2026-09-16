Looking at Target T2, the bottleneck is a **serial accumulate chain** — 12 terms added one after another, with each addition creating a 40-bit ripple carry that blocks the next. The cell walk (39 XOR) and delay spread confirm it: the depth itself is the problem, not a single hot cell.

The RTL currently builds sys_s[0..11] serially:
```
sys_s[0] = sign_extend(sys_p[0])
sys_s[1] = sys_s[0] + sign_extend(sys_p[1])
...
sys_s[11] = sys_s[10] + sign_extend(sys_p[11])
```

This is 11 serial 40-bit additions, each with its own carry chain. The fix is to **rebalance into a binary tree** — reducing serial depth from 11 levels to log₂(12) ≈ 4 levels. Synthesis can then parallelize the independent branches.

This is equivalent because:
1. Same 12 products summed (same operands).
2. Saturation logic remains at the end, applied to the final sum.
3. Fixed-width intermediate overflow is handled identically in the end result by the saturation comparison (the invariant is the final sys_acc_p and sys_sat_p outputs, which depend only on the true sum).

```json
{
  "pattern": "Serial accumulate chain in 12-term MAC",
  "strategy": "Rebalance serial add chain into balanced binary tree",
  "rationale": "Breaking 11-level serial dependency into 4-level tree allows synthesis to parallelize carry chains across independent branches. Each 40-bit add still has ripple carry, but now they overlap; serial-path depth drops from 11×carry_depth to 4×carry_depth.",
  "target": "sys domain: sys_s[0..11] generation loop (lines 273-281)",
  "changed": true,
  "expected_slack_recovery_ns": 0.35,
  "area_cost": "none — same number of adders, same register count, tree wires are temporary",
  "equivalence_argument": "All 12 sign-extended products are summed to identical 40-bit precision. Saturation is applied to the final sum via comparison against SYS_LIMIT. Reordering additions in a binary tree produces the same final sys_acc_p and sys_sat_p because the saturator operates on the same numerical result; fixed-width overflow in intermediate nodes is absorbed by the final comparison and mux, not used downstream."
}
```

```verilog
// FILE: soc_bench.v
// ---------------------------------------------------------------------------
// soc_bench — the objective-6 benchmark.
//
// Five independent asynchronous master clocks, one generated (divided) clock
// per master, clock-domain crossings between all five, and ~50K standard
// cells on nangate45. Every requirement in HANDOFF.md section 6.2 is met by
// construction, and each is load-bearing for something the flow needs to
// prove it can do:
//
//   5 async masters      the multi-clock data model (docs/multi-clock.md) —
//                        each path must be normalised against ITS OWN clock
//   generated clocks     divider ratios 2, 4, 8 and 2, so a generated clock's
//                        period is not its master's
//   CDC                  the equivalence contract (docs/equivalence-contract.md)
//                        — the boundaries the SEC partitioner must cut
//   ~50K cells           the scale at which bounded SEC times out routinely
//   an FSM               objective 3's missing half; nothing else in designs/
//                        contains a state machine at all
//
// LATENCY CONTRACT (per domain, in that domain's own clocks):
//   sys  3   dsp  3   io  2 (+FSM)   mem  3   aux  2
// A rewrite must preserve these exactly. Adding or removing a pipeline stage
// breaks the sequential-equivalence gate, which is why it is forbidden.
//
// ===========================================================================
// DO NOT EDIT THE CDC LOGIC.
// ===========================================================================
// Every synchroniser below is marked `cdc_`. Per docs/equivalence-contract.md
// the per-domain equivalence proof CUTS these boundaries, so a change here is
// invisible to SEC: removing a synchroniser stage, re-encoding a gray-coded
// bus, or "optimising" a handshake would pass every check and still be wrong
// silicon. None of it is on a critical path and none of it is a timing fix.
// It is out of the optimiser's editable scope, structurally — the same way
// pipelining is.
//
// The real timing targets are the five datapaths, and every one of them is
// slow for a reason that RTL can fix without touching latency. They are
// declared by hand in config.json so the selector can be scored against them.
// ---------------------------------------------------------------------------
module soc_bench #(
    parameter integer W        = 16,   // multiplier operand width
    parameter integer ACC      = 40,   // accumulator width
    parameter integer SYS_TAPS = 12,   // sys-domain MAC taps
    parameter integer DSP_TAPS = 12,   // dsp-domain correlator taps
    parameter integer CAM      = 48,   // mem-domain tag entries
    parameter integer SEL      = 64,   // io-domain priority select width
    parameter integer XW       = 256   // aux-domain reduction width
) (
    // ---- five independent asynchronous master clocks ----------------------
    input  wire                  clk_sys,
    input  wire                  clk_dsp,
    input  wire                  clk_io,
    input  wire                  clk_mem,
    input  wire                  clk_aux,
    input  wire                  rst_sys_n,
    input  wire                  rst_dsp_n,
    input  wire                  rst_io_n,
    input  wire                  rst_mem_n,
    input  wire                  rst_aux_n,

    // ---- sys domain -------------------------------------------------------
    input  wire                  sys_valid,
    input  wire signed [W-1:0]   sys_a,
    input  wire signed [W-1:0]   sys_b,
    output reg  signed [ACC-1:0] sys_acc,
    output reg                   sys_sat,

    // ---- dsp domain -------------------------------------------------------
    input  wire                  dsp_valid,
    input  wire signed [W-1:0]   dsp_sample,
    input  wire                  dsp_coef_we,
    input  wire signed [W-1:0]   dsp_coef_in,
    output reg  signed [ACC-1:0] dsp_corr,

    // ---- io domain --------------------------------------------------------
    input  wire                  io_rx_valid,
    input  wire [7:0]            io_rx_byte,
    input  wire [SEL-1:0]        io_req,
    output reg  [7:0]            io_status,
    output reg  [6:0]            io_grant,
    output reg                   io_frame_ok,

    // ---- mem domain -------------------------------------------------------
    input  wire                  mem_search,
    input  wire [31:0]           mem_tag,
    output reg  [5:0]            mem_hit_idx,
    output reg                   mem_hit,

    // ---- aux domain -------------------------------------------------------
    input  wire                  aux_valid,
    input  wire [XW-1:0]         aux_data,
    output reg  [31:0]           aux_crc,
    output reg                   aux_parity
);

    // =======================================================================
    // Clock dividers — one generated clock per master, four distinct ratios.
    //
    // These are real dividers, so the divided clocks are genuinely generated:
    // their period is the master's times the ratio, which is the case the
    // single-period data model got wrong. Declared in the SDC via
    // @ASTRA_CLOCK_DEFS@, sourced from the master port.
    // =======================================================================
    // Ratios 2, 4 and 8. Every divided clock is a scalar flop clocked by its
    // OWN MASTER — a counter supplies the ratio, and the clock itself is
    // re-registered on the master rather than rippled.
    //
    // That shape is deliberate, for two reasons. It is better practice than a
    // ripple divider (one flop of clock skew, not N). And it is the only
    // shape whose post-synthesis name is predictable: yosys `autoname` names a
    // cell after a net it touches, and for a flop clocked by another divider
    // it picks the *clock* net, yielding `clk_dsp_div2_r_DFFR_X1_CK` for what
    // is actually the div4 flop. Clocked by the master, every one of these is
    // `<name>_DFFR_X1_Q`, which is what config.json hangs the generated
    // clocks on.
    // Every stage is a PURE TOGGLE flop -- D is nothing but an inverter on Q.
    // That is not a style preference, it is the only shape whose synthesised
    // instance name the SDC can reach. `autoname` names a cell after a net in
    // its fan-in cone when there is one, so a divider written as
    // `clk_r <= cnt[1]` or `if (cnt[0]) clk_r <= ~clk_r` comes out as
    // `dsp_div_cnt[0]_MUX2_X1_S_Z_DFFR_X1_D` -- and an instance name
    // containing brackets is unusable in SDC, because OpenSTA reads the
    // bracket as a bus subscript and dies with `Error: stoi`. A pure toggle
    // has no such cone, so the name stays bracket-free.
    //
    // Ratios 2, 4 and 8 therefore come from a ripple: each stage is clocked
    // by the stage above it. The generated-clock chain in config.json mirrors
    // that, and periods are derived along it.
    reg clk_sys_div2_r;
    reg clk_dsp_div2_r, clk_dsp_div4_r;
    reg clk_io_div2_r;
    reg clk_mem_div2_r, clk_mem_div4_r, clk_mem_div8_r;
    reg clk_aux_div2_r;

    always @(posedge clk_sys or negedge rst_sys_n)
        if (!rst_sys_n) clk_sys_div2_r <= 1'b0;
        else            clk_sys_div2_r <= ~clk_sys_div2_r;

    always @(posedge clk_dsp or negedge rst_dsp_n)
        if (!rst_dsp_n) clk_dsp_div2_r <= 1'b0;
        else            clk_dsp_div2_r <= ~clk_dsp_div2_r;
    always @(posedge clk_dsp_div2_r or negedge rst_dsp_n)
        if (!rst_dsp_n) clk_dsp_div4_r <= 1'b0;
        else            clk_dsp_div4_r <= ~clk_dsp_div4_r;

    always @(posedge clk_io or negedge rst_io_n)
        if (!rst_io_n) clk_io_div2_r <= 1'b0;
        else           clk_io_div2_r <= ~clk_io_div2_r;

    always @(posedge clk_mem or negedge rst_mem_n)
        if (!rst_mem_n) clk_mem_div2_r <= 1'b0;
        else            clk_mem_div2_r <= ~clk_mem_div2_r;
    always @(posedge clk_mem_div2_r or negedge rst_mem_n)
        if (!rst_mem_n) clk_mem_div4_r <= 1'b0;
        else            clk_mem_div4_r <= ~clk_mem_div4_r;
    always @(posedge clk_mem_div4_r or negedge rst_mem_n)
        if (!rst_mem_n) clk_mem_div8_r <= 1'b0;
        else            clk_mem_div8_r <= ~clk_mem_div8_r;

    always @(posedge clk_aux or negedge rst_aux_n)
        if (!rst_aux_n) clk_aux_div2_r <= 1'b0;
        else            clk_aux_div2_r <= ~clk_aux_div2_r;

    wire clk_sys_div2 = clk_sys_div2_r;
    wire clk_dsp_div4 = clk_dsp_div4_r;
    wire clk_io_div2  = clk_io_div2_r;
    wire clk_mem_div8 = clk_mem_div8_r;
    wire clk_aux_div2 = clk_aux_div2_r;

    // =======================================================================
    // CDC — DO NOT EDIT. See the header.
    // =======================================================================

    // Two-flop synchronisers for single-bit control. Every crossing in this
    // design goes through one of these; none is on a critical path.
    reg [1:0] cdc_sys_from_io, cdc_dsp_from_sys, cdc_io_from_mem;
    reg [1:0] cdc_mem_from_aux, cdc_aux_from_dsp, cdc_sys_from_mem;

    always @(posedge clk_sys or negedge rst_sys_n)
        if (!rst_sys_n) begin cdc_sys_from_io <= 2'b0; cdc_sys_from_mem <= 2'b0; end
        else begin
            cdc_sys_from_io  <= {cdc_sys_from_io[0],  io_frame_ok};
            cdc_sys_from_mem <= {cdc_sys_from_mem[0], mem_hit};
        end

    always @(posedge clk_dsp or negedge rst_dsp_n)
        if (!rst_dsp_n) cdc_dsp_from_sys <= 2'b0;
        else            cdc_dsp_from_sys <= {cdc_dsp_from_sys[0], sys_sat};

    always @(posedge clk_io or negedge rst_io_n)
        if (!rst_io_n) cdc_io_from_mem <= 2'b0;
        else           cdc_io_from_mem <= {cdc_io_from_mem[0], mem_hit};

    always @(posedge clk_mem or negedge rst_mem_n)
        if (!rst_mem_n) cdc_mem_from_aux <= 2'b0;
        else            cdc_mem_from_aux <= {cdc_mem_from_aux[0], aux_parity};

    always @(posedge clk_aux or negedge rst_aux_n)
        if (!rst_aux_n) cdc_aux_from_dsp <= 2'b0;
        else            cdc_aux_from_dsp <= {cdc_aux_from_dsp[0], dsp_corr[0]};

    // Multi-bit crossing, gray coded so at most one bit changes per step.
    // Re-encoding this to binary would pass SEC and be wrong silicon.
    reg  [7:0] cdc_gray_sys;
    wire [7:0] cdc_gray_next = (cdc_gray_sys + 8'd1) ^ ((cdc_gray_sys + 8'd1) >> 1);
    always @(posedge clk_sys or negedge rst_sys_n)
        if (!rst_sys_n) cdc_gray_sys <= 8'b0;
        else if (sys_valid) cdc_gray_sys <= cdc_gray_next;

    reg [7:0] cdc_gray_mem_m, cdc_gray_mem_q;
    always @(posedge clk_mem or negedge rst_mem_n)
        if (!rst_mem_n) begin cdc_gray_mem_m <= 8'b0; cdc_gray_mem_q <= 8'b0; end
        else begin
            cdc_gray_mem_m <= cdc_gray_sys;
            cdc_gray_mem_q <= cdc_gray_mem_m;
        end

    // =======================================================================
    // SYS DOMAIN — clk_sys (fastest). Bottleneck: a serial accumulate chain
    // over SYS_TAPS unpipelined multipliers, with the saturation compare
    // sitting at the END of that chain rather than beside it.
    //
    // Fixable in RTL without changing latency: balance the accumulate into a
    // tree, and compute the saturation bound in parallel with the sum.
    // =======================================================================
    reg signed [W-1:0] sys_a_q [0:SYS_TAPS-1];
    reg signed [W-1:0] sys_b_q [0:SYS_TAPS-1];
    reg                sys_valid_q;

    integer si;
    always @(posedge clk_sys or negedge rst_sys_n) begin
        if (!rst_sys_n) begin
            for (si = 0; si < SYS_TAPS; si = si + 1) begin
                sys_a_q[si] <= {W{1'b0}};
                sys_b_q[si] <= {W{1'b0}};
            end
            sys_valid_q <= 1'b0;
        end else begin
            sys_a_q[0] <= sys_a;
            sys_b_q[0] <= sys_b;
            for (si = 1; si < SYS_TAPS; si = si + 1) begin
                sys_a_q[si] <= sys_a_q[si-1];
                sys_b_q[si] <= sys_b_q[si-1];
            end
            sys_valid_q <= sys_valid;
        end
    end

    wire signed [2*W-1:0] sys_p [0:SYS_TAPS-1];
    genvar sg;
    generate
        for (sg = 0; sg < SYS_TAPS; sg = sg + 1) begin : g_sys_mul
            assign sys_p[sg] = sys_a_q[sg] * sys_b_q[sg];
        end
    endgenerate

    // Balanced tree instead of serial chain.
    wire signed [ACC-1:0] sys_s [0:SYS_TAPS-1];
    wire signed [ACC-1:0] sys_l1 [0:5];
    wire signed [ACC-1:0] sys_l2 [0:2];
    wire signed [ACC-1:0] sys_l3 [0:1];

    generate
        for (sg = 0; sg < SYS_TAPS; sg = sg + 1) begin : g_sys_ext
            assign sys_s[sg] = {{(ACC-2*W){sys_p[sg][2*W-1]}}, sys_p[sg]};
        end
    endgenerate

    generate
        for (sg = 0; sg < 6; sg = sg + 1) begin : g_sys_l1
            assign sys_l1[sg] = sys_s[2*sg] + sys_s[2*sg+1];
        end
    endgenerate

    generate
        for (sg = 0; sg < 3; sg = sg + 1) begin : g_sys_l2
            assign sys_l2[sg] = sys_l1[2*sg] + sys_l1[2*sg+1];
        end
    endgenerate

    assign sys_l3[0] = sys_l2[0] + sys_l2[1];
    assign sys_l3[1] = sys_l2[2];

    wire signed [ACC-1:0] sys_tree_sum = sys_l3[0] + sys_l3[1];

    localparam signed [ACC-1:0] SYS_LIMIT = 40'sd137438953471;
    wire sys_over  = (sys_tree_sum >  SYS_LIMIT);
    wire sys_under = (sys_tree_sum < -SYS_LIMIT);
    wire signed [ACC-1:0] sys_sat_val = sys_over  ?  SYS_LIMIT :
                                        sys_under ? -SYS_LIMIT :
                                                     sys_tree_sum;

    reg signed [ACC-1:0] sys_acc_p;
    reg                  sys_sat_p;
    always @(posedge clk_sys or negedge rst_sys_n)
        if (!rst_sys_n) begin sys_acc_p <= {ACC{1'b0}}; sys_sat_p <= 1'b0; end
        else if (sys_valid_q) begin
            sys_acc_p <= sys_sat_val;
            sys_sat_p <= sys_over | sys_under;
        end

    // Post-scale on the generated (divided) clock: a path here has HALF the
    // frequency budget of everything above, which is the whole point of
    // ranking each path against its own clock.
    always @(posedge clk_sys_div2 or negedge rst_sys_n)
        if (!rst_sys_n) begin sys_acc <= {ACC{1'b0}}; sys_sat <= 1'b0; end
        else begin
            sys_acc <= sys_acc_p + {{(ACC-2){1'b0}}, cdc_sys_from_io[1],
                                     cdc_sys_from_mem[1]};
            sys_sat <= sys_sat_p;
        end

    // =======================================================================
    // DSP DOMAIN — clk_dsp. Bottleneck: a correlator whose taps are summed
    // serially, plus a full-width shift of every sample each cycle.
    // =======================================================================
    reg signed [W-1:0] dsp_line [0:DSP_TAPS-1];
    reg                dsp_valid_q;

    integer di;
    always @(posedge clk_dsp or negedge rst_dsp_n) begin
        if (!rst_dsp_n) begin
            for (di = 0; di < DSP_TAPS; di = di + 1)
                dsp_line[di] <= {W{1'b0}};
            dsp_valid_q <= 1'b0;
        end else begin
            dsp_line[0] <= dsp_sample;
            for (di = 1; di < DSP_TAPS; di = di + 1)
                dsp_line[di] <= dsp_line[di-1];
            dsp_valid_q <= dsp_valid;
        end
    end

    // Coefficients are programmable, so these are genuine variable-by-variable
    // multipliers. Constant coefficients fold away in synthesis and the tap
    // stops costing anything, which makes the design smaller than it looks.
    reg signed [W-1:0] dsp_coef_q [0:DSP_TAPS-1];
    integer dc;
    always @(posedge clk_dsp or negedge rst_dsp_n) begin
        if (!rst_dsp_n) begin
            for (dc = 0; dc < DSP_TAPS; dc = dc + 1)
                dsp_coef_q[dc] <= {W{1'b0}};
        end else if (dsp_coef_we) begin
            dsp_coef_q[0] <= dsp_coef_in;
            for (dc = 1; dc < DSP_TAPS; dc = dc + 1)
                dsp_coef_q[dc] <= dsp_coef_q[dc-1];
        end
    end

    wire signed [2*W-1:0] dsp_p [0:DSP_TAPS-1];
    genvar dg;
    generate
        for (dg = 0; dg < DSP_TAPS; dg = dg + 1) begin : g_dsp_mul
            assign dsp_p[dg] = dsp_line[dg] * dsp_coef_q[dg];
        end
    endgenerate

    wire signed [ACC-1:0] dsp_s [0:DSP_TAPS-1];
    generate
        for (dg = 0; dg < DSP_TAPS; dg = dg + 1) begin : g_dsp_add
            if (dg == 0)
                assign dsp_s[0] = {{(ACC-2*W){dsp_p[0][2*W-1]}}, dsp_p[0]};
            else
                assign dsp_s[dg] = dsp_s[dg-1]
                                 + {{(ACC-2*W){dsp_p[dg][2*W-1]}}, dsp_p[dg]};
        end
    endgenerate

    reg signed [ACC-1:0] dsp_corr_p;
    always @(posedge clk_dsp or negedge rst_dsp_n)
        if (!rst_dsp_n) dsp_corr_p <= {ACC{1'b0}};
        else if (dsp_valid_q) dsp_corr_p <= dsp_s[DSP_TAPS-1];

    always @(posedge clk_dsp_div4 or negedge rst_dsp_n)
        if (!rst_dsp_n) dsp_corr <= {ACC{1'b0}};
        else dsp_corr <= dsp_corr_p ^ {{(ACC-1){1'b0}}, cdc_dsp_from_sys[1]};

    // =======================================================================
    // IO DOMAIN — clk_io (slowest). Contains the FSM, and a SEL-way priority
    // select written as a serial mux cascade.
    //
    // The FSM is objective 3's missing testbed: nothing else in designs/ has
    // a state machine, so "FSM optimization" could not be tested at all.
    // =======================================================================
    localparam [2:0] S_IDLE = 3'd0,
                     S_SOF  = 3'd1,
                     S_LEN  = 3'd2,
                     S_DATA = 3'd3,
                     S_CRC  = 3'd4,
                     S_EOF  = 3'd5,
                     S_ERR  = 3'd6;

    reg [2:0] io_state, io_next;
    reg [7:0] io_len, io_count, io_check;

    always @(*) begin
        io_next = io_state;
        case (io_state)
            S_IDLE: if (io_rx_valid && io_rx_byte == 8'h7E) io_next = S_SOF;
            S_SOF:  if (io_rx_valid) io_next = (io_rx_byte == 8'h7E) ? S_SOF : S_LEN;
            S_LEN:  if (io_rx_valid) io_next = (io_rx_byte == 8'h00) ? S_ERR : S_DATA;
            S_DATA: if (io_rx_valid && io_count == io_len) io_next = S_CRC;
            S_CRC:  if (io_rx_valid) io_next = (io_rx_byte == io_check) ? S_EOF : S_ERR;
            S_EOF:  io_next = S_IDLE;
            S_ERR:  if (io_rx_valid && io_rx_byte == 8'h7E) io_next = S_SOF;
            default: io_next = S_IDLE;
        endcase
    end

    always @(posedge clk_io or negedge rst_io_n) begin
        if (!rst_io_n) begin
            io_state <= S_IDLE;
            io_len <= 8'b0; io_count <= 8'b0; io_check <= 8'b0;
            io_frame_ok <= 1'b0;
        end else begin
            io_state <= io_next;
            case (io_state)
                S_LEN:  if (io_rx_valid) begin
                            io_len   <= io_rx_byte;
                            io_count <= 8'b0;
                            io_check <= 8'b0;
                        end
                S_DATA: if (io_rx_valid) begin
                            io_count <= io_count + 8'd1;
                            io_check <= io_check ^ io_rx_byte;
                        end
                default: ;
            endcase
            io_frame_ok <= (io_state == S_EOF);
        end
    end

    // Priority select as a serial cascade: bit i's grant waits on every bit
    // below it. SEL deep, and the natural fix is a tree.
    wire [6:0] io_sel_chain [0:SEL];
    wire       io_any_chain [0:SEL];
    assign io_sel_chain[0] = 7'd0;
    assign io_any_chain[0] = 1'b0;
    genvar ig;
    generate
        for (ig = 0; ig < SEL; ig = ig + 1) begin : g_io_sel
            assign io_any_chain[ig+1] = io_any_chain[ig] | io_req[ig];
            assign io_sel_chain[ig+1] = io_any_chain[ig] ? io_sel_chain[ig]
                                      : (io_req[ig] ? ig[6:0] : io_sel_chain[ig]);
        end
    endgenerate

    always @(posedge clk_io or negedge rst_io_n)
        if (!rst_io_n) io_grant <= 7'd0;
        else           io_grant <= io_sel_chain[SEL];

    always @(posedge clk_io_div2 or negedge rst_io_n)
        if (!rst_io_n) io_status <= 8'b0;
        else io_status <= {io_state, cdc_io_from_mem[1], io_check[3:0]};

    // =======================================================================
    // MEM DOMAIN — clk_mem. Bottleneck: CAM tag match where every entry is
    // compared at full width and the hit index is resolved by a serial
    // priority cascade over the match vector.
    // =======================================================================
    reg [31:0] mem_tags [0:CAM-1];
    reg        mem_search_q;

    integer mi;
    always @(posedge clk_mem or negedge rst_mem_n) begin
        if (!rst_mem_n) begin
            for (mi = 0; mi < CAM; mi = mi + 1)
                mem_tags[mi] <= {32{1'b0}};
            mem_search_q <= 1'b0;
        end else begin
            mem_tags[0] <= mem_tag;
            for (mi = 1; mi < CAM; mi = mi + 1)
                mem_tags[mi] <= mem_tags[mi-1];
            mem_search_q <= mem_search;
        end
    end

    wire [CAM-1:0] mem_match;
    genvar mg;
    generate
        for (mg = 0; mg < CAM; mg = mg + 1) begin : g_mem_cmp
            assign mem_match[mg] = (mem_tags[mg] == mem_tag);
        end
    endgenerate

    wire [5:0] mem_idx_chain [0:CAM];
    wire       mem_any_chain [0:CAM];
    assign mem_idx_chain[0] = 6'd0;
    assign mem_any_chain[0] = 1'b0;
    generate
        for (mg = 0; mg < CAM; mg = mg + 1) begin : g_mem_pri
            assign mem_any_chain[mg+1] = mem_any_chain[mg] | mem_match[mg];
            assign mem_idx_chain[mg+1] = mem_any_chain[mg] ? mem_idx_chain[mg]
                                       : (mem_match[mg] ? mg[5:0]
                                                        : mem_idx_chain[mg]);
        end
    endgenerate

    reg [5:0] mem_idx_p;
    reg       mem_hit_p;
    always @(posedge clk_mem or negedge rst_mem_n)
        if (!rst_mem_n) begin mem_idx_p <= 6'd0; mem_hit_p <= 1'b0; end
        else if (mem_search_q) begin
            mem_idx_p <= mem_idx_chain[CAM];
            mem_hit_p <= mem_any_chain[CAM];
        end

    always @(posedge clk_mem_div8 or negedge rst_mem_n)
        if (!rst_mem_n) begin mem_hit_idx <= 6'd0; mem_hit <= 1'b0; end
        else begin
            mem_hit_idx <= mem_idx_p ^ {5'b0, cdc_mem_from_aux[1]};
            mem_hit     <= mem_hit_p;
        end

    // =======================================================================
    // AUX DOMAIN — clk_aux. Bottleneck: a CRC32 over a wide word combined
    // with an XW-bit parity reduction written as a serial XOR chain.
    // A balanced XOR tree is the textbook fix and does not change latency.
    // =======================================================================
    reg [XW-1:0] aux_data_q;
    reg          aux_valid_q;
    always @(posedge clk_aux or negedge rst_aux_n)
        if (!rst_aux_n) begin aux_data_q <= {XW{1'b0}}; aux_valid_q <= 1'b0; end
        else begin aux_data_q <= aux_data; aux_valid_q <= aux_valid; end

    // Serial XOR reduction: depth XW instead of log2(XW).
    wire aux_par_chain [0:XW];
    assign aux_par_chain[0] = 1'b0;
    genvar xg;
    generate
        for (xg = 0; xg < XW; xg = xg + 1) begin : g_aux_par
            assign aux_par_chain[xg+1] = aux_par_chain[xg] ^ aux_data_q[xg];
        end
    endgenerate

    // CRC32 (IEEE 802.3 polynomial), bit-serial over the low 32 bits — again
    // a chain, one bit at a time, rather than a parallel update.
    wire [31:0] aux_crc_chain [0:32];
    assign aux_crc_chain[0] = aux_crc;
    genvar cg;
    generate
        for (cg = 0; cg < 32; cg = cg + 1) begin : g_aux_crc
            wire fb = aux_crc_chain[cg][31] ^ aux_data_q[cg];
            assign aux_crc_chain[cg+1] =
                {aux_crc_chain[cg][30:0], 1'b0} ^ (fb ? 32'h04C11DB7 : 32'h0);
        end
    endgenerate

    always @(posedge clk_aux or negedge rst_aux_n)
        if (!rst_aux_n) aux_crc <= 32'hFFFF_FFFF;
        else if (aux_valid_q) aux_crc <= aux_crc_chain[32];

    always @(posedge clk_aux_div2 or negedge rst_aux_n)
        if (!rst_aux_n) aux_parity <= 1'b0;
        else aux_parity <= aux_par_chain[XW] ^ cdc_aux_from_dsp[1];

endmodule
```