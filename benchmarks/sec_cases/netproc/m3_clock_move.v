// ---------------------------------------------------------------------------
// netproc — the objective-6 benchmark, built to be *checkable*.
//
// soc_bench meets every structural requirement and cannot be certified: its 24
// unpipelined multipliers make a bounded equivalence check intractable, so the
// loop refutes bad candidates on it and times out rather than confirming good
// ones (HANDOFF.md §6.4). Cell count was never the problem. Multipliers were.
//
// So this design carries the same structure — five asynchronous masters,
// generated clocks at three ratios, crossings between all five, an FSM — and
// gets its bulk from logic a SAT solver can actually reason about at depth:
// XOR trees, mux networks, comparators and shift chains. No multiplier
// anywhere, deliberately.
//
// The timing bottlenecks are still real, and still the kind an RTL rewrite
// fixes without touching latency:
//
//   rx    a 32-stage serial CRC shift, one bit per stage, where a parallel
//         update over the whole word is the textbook fix
//   parse a 96-way priority select written as a serial mux cascade
//   look  a 256-entry CAM whose hit index resolves through a serial cascade
//   xfrm  a 512-bit parity built as a serial XOR chain, depth 512 not 9
//   tx    a serial accumulate over 16 terms instead of a balanced tree
//
// LATENCY CONTRACT (per domain, in that domain's own clocks):
//   rx 2   parse 2   look 3   xfrm 2   tx 3
//
// ===========================================================================
// DO NOT EDIT anything named cdc_* or clk_*_div* — see
// docs/equivalence-contract.md. `protected` in config.json enforces it
// mechanically, before synthesis.
// ===========================================================================
module netproc #(
    parameter integer DW   = 256,  // datapath width
    parameter integer SEL  = 96,   // parse-domain priority select width
    parameter integer CAM  = 256,  // look-domain tag entries
    parameter integer XW   = 512,  // xfrm-domain reduction width
    parameter integer TAPS = 16    // tx-domain accumulate terms
) (
    // ---- five independent asynchronous master clocks ----------------------
    input  wire              clk_rx,
    input  wire              clk_parse,
    input  wire              clk_look,
    input  wire              clk_xfrm,
    input  wire              clk_tx,
    input  wire              rst_n,

    input  wire              rx_valid,
    input  wire [7:0]        rx_byte,
    output reg  [31:0]       rx_crc,
    output reg               rx_frame_ok,
    output reg  [7:0]        rx_status,

    input  wire [SEL-1:0]    parse_req,
    output reg  [7:0]        parse_grant,

    input  wire              look_search,
    input  wire [31:0]       look_tag,
    output reg  [7:0]        look_idx,
    output reg               look_hit,

    input  wire              xfrm_valid,
    input  wire [XW-1:0]     xfrm_data,
    input  wire [5:0]        xfrm_rot,
    output reg  [DW-1:0]     xfrm_out,
    output reg               xfrm_par,

    input  wire              tx_valid,
    input  wire [DW-1:0]     tx_in,
    output reg  [DW+3:0]     tx_sum
);

    // =======================================================================
    // Clock dividers — ratios 2, 4 and 8, one generated clock per master.
    //
    // Pure toggle flops. A divider written as `clk_r <= cnt[1]` gets an
    // instance name from its fan-in cone -- brackets and all -- and OpenSTA
    // cannot hang a generated clock on a bracketed pin. See HANDOFF.md §7.
    // =======================================================================
    reg clk_rx_div2_r;
    reg clk_parse_div2_r, clk_parse_div4_r;
    reg clk_look_div2_r,  clk_look_div4_r, clk_look_div8_r;
    reg clk_xfrm_div2_r;
    reg clk_tx_div2_r;

    always @(posedge clk_rx or negedge rst_n)
        if (!rst_n) clk_rx_div2_r <= 1'b0; else clk_rx_div2_r <= ~clk_rx_div2_r;

    always @(posedge clk_parse or negedge rst_n)
        if (!rst_n) clk_parse_div2_r <= 1'b0;
        else        clk_parse_div2_r <= ~clk_parse_div2_r;
    always @(posedge clk_parse_div2_r or negedge rst_n)
        if (!rst_n) clk_parse_div4_r <= 1'b0;
        else        clk_parse_div4_r <= ~clk_parse_div4_r;

    always @(posedge clk_look or negedge rst_n)
        if (!rst_n) clk_look_div2_r <= 1'b0;
        else        clk_look_div2_r <= ~clk_look_div2_r;
    always @(posedge clk_look_div2_r or negedge rst_n)
        if (!rst_n) clk_look_div4_r <= 1'b0;
        else        clk_look_div4_r <= ~clk_look_div4_r;
    always @(posedge clk_look_div4_r or negedge rst_n)
        if (!rst_n) clk_look_div8_r <= 1'b0;
        else        clk_look_div8_r <= ~clk_look_div8_r;

    always @(posedge clk_xfrm or negedge rst_n)
        if (!rst_n) clk_xfrm_div2_r <= 1'b0;
        else        clk_xfrm_div2_r <= ~clk_xfrm_div2_r;

    always @(posedge clk_tx or negedge rst_n)
        if (!rst_n) clk_tx_div2_r <= 1'b0; else clk_tx_div2_r <= ~clk_tx_div2_r;

    // =======================================================================
    // CDC — DO NOT EDIT.
    // Two-flop synchronisers, plus one gray-coded multi-bit crossing.
    // =======================================================================
    reg [1:0] cdc_parse_from_rx, cdc_look_from_parse, cdc_xfrm_from_look;
    reg [1:0] cdc_tx_from_xfrm,  cdc_rx_from_tx;

    always @(posedge clk_parse or negedge rst_n)
        if (!rst_n) cdc_parse_from_rx <= 2'b0;
        else        cdc_parse_from_rx <= {cdc_parse_from_rx[0], rx_frame_ok};
    always @(posedge clk_look or negedge rst_n)
        if (!rst_n) cdc_look_from_parse <= 2'b0;
        else        cdc_look_from_parse <= {cdc_look_from_parse[0], parse_grant[0]};
    always @(posedge clk_xfrm or negedge rst_n)
        if (!rst_n) cdc_xfrm_from_look <= 2'b0;
        else        cdc_xfrm_from_look <= {cdc_xfrm_from_look[0], look_hit};
    always @(posedge clk_tx or negedge rst_n)
        if (!rst_n) cdc_tx_from_xfrm <= 2'b0;
        else        cdc_tx_from_xfrm <= {cdc_tx_from_xfrm[0], xfrm_par};
    always @(posedge clk_rx or negedge rst_n)
        if (!rst_n) cdc_rx_from_tx <= 2'b0;
        else        cdc_rx_from_tx <= {cdc_rx_from_tx[0], tx_sum[0]};

    // Gray-coded counter crossing rx -> look. Re-encoding this to binary would
    // pass every check the loop runs and be wrong silicon.
    reg [7:0] cdc_gray_rx;
    wire [7:0] cdc_gray_nxt = (cdc_gray_rx + 8'd1) ^ ((cdc_gray_rx + 8'd1) >> 1);
    always @(posedge clk_rx or negedge rst_n)
        if (!rst_n) cdc_gray_rx <= 8'b0;
        else if (rx_valid) cdc_gray_rx <= cdc_gray_nxt;

    reg [7:0] cdc_gray_look_m, cdc_gray_look_q;
    always @(posedge clk_look or negedge rst_n)
        if (!rst_n) begin cdc_gray_look_m <= 8'b0; cdc_gray_look_q <= 8'b0; end
        else begin
            cdc_gray_look_m <= cdc_gray_rx;
            cdc_gray_look_q <= cdc_gray_look_m;
        end

    // =======================================================================
    // RX DOMAIN — the FSM, plus a bit-serial CRC32 shift.
    // Bottleneck: the CRC advances one bit per stage, so the byte costs 32
    // dependent XOR/mux stages. A parallel update over the whole byte is the
    // fix and changes neither latency nor the result.
    // =======================================================================
    localparam [2:0] S_IDLE = 3'd0, S_SOF = 3'd1, S_LEN = 3'd2,
                     S_DATA = 3'd3, S_CRC = 3'd4, S_EOF = 3'd5, S_ERR = 3'd6;

    reg [2:0] rx_state, rx_next;
    reg [7:0] rx_len, rx_count;

    always @(*) begin
        rx_next = rx_state;
        case (rx_state)
            S_IDLE: if (rx_valid && rx_byte == 8'h7E) rx_next = S_SOF;
            S_SOF:  if (rx_valid) rx_next = (rx_byte == 8'h7E) ? S_SOF : S_LEN;
            S_LEN:  if (rx_valid) rx_next = (rx_byte == 8'h00) ? S_ERR : S_DATA;
            S_DATA: if (rx_valid && rx_count == rx_len) rx_next = S_CRC;
            S_CRC:  if (rx_valid) rx_next = S_EOF;
            S_EOF:  rx_next = S_IDLE;
            S_ERR:  if (rx_valid && rx_byte == 8'h7E) rx_next = S_SOF;
            default: rx_next = S_IDLE;
        endcase
    end

    wire [31:0] crc_chain [0:8];
    assign crc_chain[0] = rx_crc;
    genvar c;
    generate
        for (c = 0; c < 8; c = c + 1) begin : g_crc
            wire fb = crc_chain[c][31] ^ rx_byte[c];
            assign crc_chain[c+1] = {crc_chain[c][30:0], 1'b0}
                                  ^ (fb ? 32'h04C11DB7 : 32'h0);
        end
    endgenerate

    // The CRC result is named here, off the protected line below. That line
    // mentions cdc_rx_from_tx, so protect.py forbids changing it -- and a
    // rewrite of the shift chain above would have had to change it, if it
    // read crc_chain[8] directly. Naming the result keeps the chain editable
    // and the crossing untouched. parse/look/tx already get this for free
    // from a pipeline register; rx and xfrm are combinational into the
    // protected line, so a wire carries it instead. Latency is unchanged.
    wire [31:0] crc_next = crc_chain[8];

    always @(posedge clk_rx or negedge rst_n) begin
        if (!rst_n) begin
            rx_state <= S_IDLE; rx_len <= 8'b0; rx_count <= 8'b0;
            rx_crc <= 32'hFFFF_FFFF; rx_frame_ok <= 1'b0;
        end else begin
            rx_state <= rx_next;
            case (rx_state)
                S_LEN:  if (rx_valid) begin rx_len <= rx_byte; rx_count <= 8'b0; end
                S_DATA: if (rx_valid) rx_count <= rx_count + 8'd1;
                default: ;
            endcase
            if (rx_valid) rx_crc <= crc_next ^ {31'b0, cdc_rx_from_tx[1]};
            rx_frame_ok <= (rx_state == S_EOF);
        end
    end

    // Status on the divided clock: half the rx budget.
    always @(posedge clk_rx_div2_r or negedge rst_n)
        if (!rst_n) rx_status <= 8'b0;
        else rx_status <= {rx_state, rx_frame_ok, rx_count[3:0]};

    // =======================================================================
    // PARSE DOMAIN — SEL-way priority select as a serial mux cascade.
    // =======================================================================
    wire [7:0] p_sel [0:SEL];
    wire       p_any [0:SEL];
    assign p_sel[0] = 8'd0;
    assign p_any[0] = 1'b0;
    genvar i;
    generate
        for (i = 0; i < SEL; i = i + 1) begin : g_parse
            assign p_any[i+1] = p_any[i] | parse_req[i];
            assign p_sel[i+1] = p_any[i] ? p_sel[i]
                              : (parse_req[i] ? i[7:0] : p_sel[i]);
        end
    endgenerate

    reg [7:0] p_grant_p;
    always @(posedge clk_parse or negedge rst_n)
        if (!rst_n) p_grant_p <= 8'd0;
        else p_grant_p <= p_sel[SEL];

    // Output stage on the divide-by-4 clock: a quarter of the frequency
    // budget of the cascade above it, which is why each path is ranked
    // against its own clock rather than the design's fastest.
    always @(posedge clk_parse_div4_r or negedge rst_n)
        if (!rst_n) parse_grant <= 8'd0;
        else parse_grant <= p_grant_p ^ {7'b0, cdc_parse_from_rx[1]};

    // =======================================================================
    // LOOK DOMAIN — CAM tag match, hit index by serial priority cascade.
    // =======================================================================
    reg [31:0] tags [0:CAM-1];
    reg        look_search_q;

    integer m;
    always @(posedge clk_look or negedge rst_n) begin
        if (!rst_n) begin
            for (m = 0; m < CAM; m = m + 1) tags[m] <= 32'b0;
            look_search_q <= 1'b0;
        end else begin
            tags[0] <= look_tag;
            for (m = 1; m < CAM; m = m + 1) tags[m] <= tags[m-1];
            look_search_q <= look_search;
        end
    end

    wire [CAM-1:0] match;
    generate
        for (i = 0; i < CAM; i = i + 1) begin : g_cmp
            assign match[i] = (tags[i] == look_tag);
        end
    endgenerate

    wire [7:0] l_idx [0:CAM];
    wire       l_any [0:CAM];
    assign l_idx[0] = 8'd0;
    assign l_any[0] = 1'b0;
    generate
        for (i = 0; i < CAM; i = i + 1) begin : g_pri
            assign l_any[i+1] = l_any[i] | match[i];
            assign l_idx[i+1] = l_any[i] ? l_idx[i]
                              : (match[i] ? i[7:0] : l_idx[i]);
        end
    endgenerate

    reg [7:0] l_idx_p;
    reg       l_hit_p;
    always @(posedge clk_look_div2_r or negedge rst_n)
        if (!rst_n) begin l_idx_p <= 8'd0; l_hit_p <= 1'b0; end
        else if (look_search_q) begin
            l_idx_p <= l_idx[CAM];
            l_hit_p <= l_any[CAM];
        end

    always @(posedge clk_look_div8_r or negedge rst_n)
        if (!rst_n) begin look_idx <= 8'd0; look_hit <= 1'b0; end
        else begin
            look_idx <= l_idx_p ^ {7'b0, cdc_gray_look_q[0]};
            look_hit <= l_hit_p;
        end

    // =======================================================================
    // XFRM DOMAIN — a barrel rotate (mux network) feeding a serial XOR parity.
    // Bottleneck: the parity chain is XW deep instead of log2(XW).
    // =======================================================================
    reg [XW-1:0] xd_q;
    reg [5:0]    rot_q;
    reg          xv_q;
    always @(posedge clk_xfrm or negedge rst_n)
        if (!rst_n) begin xd_q <= {XW{1'b0}}; rot_q <= 6'b0; xv_q <= 1'b0; end
        else begin xd_q <= xfrm_data; rot_q <= xfrm_rot; xv_q <= xfrm_valid; end

    wire [DW-1:0] rot_in = xd_q[DW-1:0];
    wire [DW-1:0] rot_s0 = rot_q[0] ? {rot_in[DW-2:0], rot_in[DW-1]}   : rot_in;
    wire [DW-1:0] rot_s1 = rot_q[1] ? {rot_s0[DW-3:0], rot_s0[DW-1:DW-2]} : rot_s0;
    wire [DW-1:0] rot_s2 = rot_q[2] ? {rot_s1[DW-5:0], rot_s1[DW-1:DW-4]} : rot_s1;
    wire [DW-1:0] rot_s3 = rot_q[3] ? {rot_s2[DW-9:0], rot_s2[DW-1:DW-8]} : rot_s2;
    wire [DW-1:0] rot_s4 = rot_q[4] ? {rot_s3[DW-17:0], rot_s3[DW-1:DW-16]} : rot_s3;
    wire [DW-1:0] rot_s5 = rot_q[5] ? {rot_s4[DW-33:0], rot_s4[DW-1:DW-32]} : rot_s4;

    wire par_chain [0:XW];
    assign par_chain[0] = 1'b0;
    generate
        for (i = 0; i < XW; i = i + 1) begin : g_par
            assign par_chain[i+1] = par_chain[i] ^ xd_q[i];
        end
    endgenerate

    // Same reason as crc_next above: the xfrm_par line mentions
    // cdc_xfrm_from_look and so may not be edited, which would have frozen
    // the 512-deep reduction feeding it. Rewrite par_chain into a tree and
    // drive par_chain_out; the protected line never has to move.
    wire par_chain_out = par_chain[XW];

    always @(posedge clk_xfrm or negedge rst_n)
        if (!rst_n) xfrm_out <= {DW{1'b0}};
        else if (xv_q) xfrm_out <= rot_s5;

    always @(posedge clk_xfrm_div2_r or negedge rst_n)
        if (!rst_n) xfrm_par <= 1'b0;
        else xfrm_par <= par_chain_out ^ cdc_xfrm_from_look[1];

    // =======================================================================
    // TX DOMAIN — serial accumulate over TAPS terms instead of a tree.
    // =======================================================================
    reg [DW-1:0] tx_line [0:TAPS-1];
    reg          tx_valid_q;

    integer k;
    always @(posedge clk_tx or negedge rst_n) begin
        if (!rst_n) begin
            for (k = 0; k < TAPS; k = k + 1) tx_line[k] <= {DW{1'b0}};
            tx_valid_q <= 1'b0;
        end else begin
            tx_line[0] <= tx_in;
            for (k = 1; k < TAPS; k = k + 1) tx_line[k] <= tx_line[k-1];
            tx_valid_q <= tx_valid;
        end
    end

    wire [DW+3:0] t_acc [0:TAPS-1];
    generate
        for (i = 0; i < TAPS; i = i + 1) begin : g_tx
            if (i == 0) assign t_acc[0] = {4'b0, tx_line[0]};
            else        assign t_acc[i] = t_acc[i-1] + {4'b0, tx_line[i]};
        end
    endgenerate

    reg [DW+3:0] tx_sum_p;
    always @(posedge clk_tx or negedge rst_n)
        if (!rst_n) tx_sum_p <= {(DW+4){1'b0}};
        else if (tx_valid_q) tx_sum_p <= t_acc[TAPS-1];

    always @(posedge clk_tx_div2_r or negedge rst_n)
        if (!rst_n) tx_sum <= {(DW+4){1'b0}};
        else tx_sum <= tx_sum_p + {{(DW+3){1'b0}}, cdc_tx_from_xfrm[1]};

endmodule
