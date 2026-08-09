// ---------------------------------------------------------------------------
// alu32 — 32-bit ALU with registered inputs and outputs.
//
// Baseline design: comfortably meets timing at the default period. Use it to
// sanity-check that the flow works end to end before pointing the optimisation
// loop at something that actually violates.
// ---------------------------------------------------------------------------
module alu32 (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire [3:0]  op,
    output reg  [31:0] result,
    output reg         zero,
    output reg         carry
);

    localparam OP_ADD = 4'd0, OP_SUB = 4'd1, OP_AND = 4'd2, OP_OR  = 4'd3,
               OP_XOR = 4'd4, OP_SLL = 4'd5, OP_SRL = 4'd6, OP_SRA = 4'd7,
               OP_SLT = 4'd8;

    reg [31:0] a_q, b_q;
    reg [3:0]  op_q;

    always @(posedge clk) begin
        if (!rst_n) begin
            a_q  <= 32'd0;
            b_q  <= 32'd0;
            op_q <= 4'd0;
        end else begin
            a_q  <= a;
            b_q  <= b;
            op_q <= op;
        end
    end

    wire [32:0] sum  = {1'b0, a_q} + {1'b0, b_q};
    wire [32:0] diff = {1'b0, a_q} - {1'b0, b_q};
    wire        slt  = ($signed(a_q) < $signed(b_q));

    reg  [32:0] alu_out;
    always @(*) begin
        case (op_q)
            OP_ADD:  alu_out = sum;
            OP_SUB:  alu_out = diff;
            OP_AND:  alu_out = {1'b0, a_q & b_q};
            OP_OR:   alu_out = {1'b0, a_q | b_q};
            OP_XOR:  alu_out = {1'b0, a_q ^ b_q};
            OP_SLL:  alu_out = {1'b0, a_q << b_q[4:0]};
            OP_SRL:  alu_out = {1'b0, a_q >> b_q[4:0]};
            OP_SRA:  alu_out = {1'b0, $signed(a_q) >>> b_q[4:0]};
            OP_SLT:  alu_out = {32'd0, slt};
            default: alu_out = 33'd0;
        endcase
    end

    always @(posedge clk) begin
        if (!rst_n) begin
            result <= 32'd0;
            zero   <= 1'b0;
            carry  <= 1'b0;
        end else begin
            result <= alu_out[31:0];
            zero   <= (alu_out[31:0] == 32'd0);
            carry  <= alu_out[32];
        end
    end

endmodule
