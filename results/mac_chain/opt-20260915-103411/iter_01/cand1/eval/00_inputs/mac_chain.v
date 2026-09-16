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

    // ---- four multiplies ----
    wire signed [2*W-1:0] p0 = a0_q * b0_q;
    wire signed [2*W-1:0] p1 = a1_q * b1_q;
    wire signed [2*W-1:0] p2 = a2_q * b2_q;
    wire signed [2*W-1:0] p3 = a3_q * b3_q;

    // ---- reordered balanced accumulation: (p0+p2) || (p1+p3) then final sum
    // This exposes partial results earlier, allowing saturation logic to run in parallel
    wire signed [ACC-1:0] s0 = {{(ACC-2*W){p0[2*W-1]}}, p0};
    wire signed [ACC-1:0] p2_ext = {{(ACC-2*W){p2[2*W-1]}}, p2};
    wire signed [ACC-1:0] p1_ext = {{(ACC-2*W){p1[2*W-1]}}, p1};
    wire signed [ACC-1:0] p3_ext = {{(ACC-2*W){p3[2*W-1]}}, p3};

    wire signed [ACC-1:0] s02 = s0 + p2_ext;
    wire signed [ACC-1:0] p13 = p1_ext + p3_ext;
    wire signed [ACC-1:0] s3 = s02 + p13;

    // ---- saturation comparison ----
    wire over  = (s3 >  LIMIT);
    wire under = (s3 < -LIMIT);

    wire signed [ACC-1:0] sat_val = over  ?  LIMIT :
                                    under ? -LIMIT : s3;

    always @(posedge clk) begin
        if (!rst_n) begin
            acc_out   <= {ACC{1'b0}};
            sat_flag  <= 1'b0;
            valid_out <= 1'b0;
        end else begin
            acc_out   <= sat_val;
            sat_flag  <= over | under;
            valid_out <= valid_q;
        end
    end

endmodule
