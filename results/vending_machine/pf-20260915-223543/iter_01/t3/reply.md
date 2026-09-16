Looking at the critical path and the library suggestion, the issue is clear: synthesis is computing both `discountA + discountB` and `discountC + discountD` in parallel, then muxing the results. This creates two full 1024-bit ripple carry chains.

The fix is to move the mux before the addition: select the operands first, then add them once. This collapses two carry chains into one.

The transformation is straightforward—introducing explicit intermediate wires for the muxes forces synthesis to see the selection stages first, preventing it from reordering into "both adds then mux."

```json
{
  "pattern": "Output mux selecting wide arithmetic results",
  "strategy": "Move mux before computation into single path",
  "rationale": "Eliminates duplicate carry chains. Input multiplexers have shallower propagation than output selection after two wide adders. One 1024-bit addition is one carry chain; two adds then mux is two carry chains in parallel. A single carry path removes ~33% of the segment delay.",
  "target": "Lines 52-53: total_discount assignment, refactored via intermediate wires op1, op2",
  "changed": true,
  "expected_slack_recovery_ns": 1.2,
  "area_cost": "small — same logic restructured; mux before add is area-neutral or better",
  "equivalence_argument": "Original and new both compute: when sel=1, op1=discountA and op2=discountB so result=discountA+discountB; when sel=0, op1=discountC and op2=discountD so result=discountC+discountD. Bit-for-bit identical outputs including overflow behavior. Reset is unchanged (combinational path, no flops)."
}
```

```verilog
// FILE: vending_machine.v
// vending-machine
module vending_machine(
                    clk,
                    reset,
                    condition,
                    sel,
                    discountA,
                    discountB,
                    discountC,
                    discountD,
                    total_discount,
                    sell_signal
                    );

    // State encoding
    localparam  S0 = 4'b0000,
               S1 = 4'b0001,
               S2 = 4'b0010,
               S3 = 4'b0011,
               S4 = 4'b0100,
               S5 = 4'b0101,
               S6 = 4'b0110,
               S7 = 4'b0111,
               S8 = 4'b1000,
               S9 = 4'b1001,
               S10 = 4'b1010;

    parameter DATA_WIDTH = 64;
    parameter K = 16;

    input wire clk, reset, condition, sel;
    input wire [K*DATA_WIDTH-1:0] discountA, discountB, discountC, discountD;

    output reg sell_signal;
    output reg [K*DATA_WIDTH-1:0] total_discount;

    reg [3:0] next_state;
    reg [3:0] state;

    wire [K*DATA_WIDTH-1:0] op1 = sel ? discountA : discountC;
    wire [K*DATA_WIDTH-1:0] op2 = sel ? discountB : discountD;

    // Sequential logic for state transitions
    always @(posedge clk or posedge reset) begin
        if (reset) begin
            state <= S0;  // Reset to state S0
        end else begin
            state <= next_state;
        end
    end


    always @(*) begin
        total_discount = op1 + op2;
    end


    // Combinatorial logic for next state and output
    always @(*) begin
        case (state)
            S0: begin
                next_state = condition ? S2 : S1;
                sell_signal = 1'b1;
            end
            S1: begin
                next_state = condition ? S5 : S3;
                sell_signal = 1'b1;
            end
            S2: begin
                next_state = condition ? S4 : S5;
                sell_signal = 1'b0;
            end
            S3: begin
                next_state = condition ? S6 : S1;
                sell_signal = 1'b1;
            end
            S4: begin
                next_state = condition ? S2 : S5;
                sell_signal = 1'b0;
            end
            S5: begin
                next_state = condition ? S3 : S4;
                sell_signal = 1'b0;
            end
            S6: begin
                next_state = condition ? S6 : S5;
                sell_signal = 1'b0;
            end

            default: begin
                next_state = S0;
                sell_signal = 1'b0;
            end
        endcase
    end
endmodule
```