# ---------------------------------------------------------------------------
# vending_machine timing constraints -- one clock.
#
# The design has two kinds of path, and both are constrained here:
#
#   * register to register: the 4-bit FSM, state -> next_state -> state.
#   * input to output, purely combinational: total_discount is two
#     K*DATA_WIDTH = 1024-bit adders (A+B and C+D) selected by `sel`, with no
#     register anywhere on the way. That path is timed only by the input and
#     output delays below, so it gets (1 - 0.15 - 0.15) = 70% of the period.
#     It is the critical path, and the optimisation target.
#
# The period itself comes from config.json via @CLK_PERIOD@.
# ---------------------------------------------------------------------------
set CLK_PERIOD 5
set CLK_PORT   clk
set RST_PORT   reset

create_clock -name clk -period $CLK_PERIOD [get_ports $CLK_PORT]

set_clock_uncertainty 0.05 [get_clocks clk]
set_clock_transition  0.05 [get_clocks clk]

# OpenSTA has no `remove_from_collection` (that is a Synopsys command), and it
# cannot parse a bus subscript in a get_ports pattern, so the clock and reset
# ports are dropped from the input list by name.
set data_inputs {}
foreach p [all_inputs] {
    set n [get_full_name $p]
    if {$n ne $CLK_PORT && $n ne $RST_PORT} { lappend data_inputs $p }
}

set_input_delay  -clock clk [expr {$CLK_PERIOD * 0.15}] $data_inputs
set_output_delay -clock clk [expr {$CLK_PERIOD * 0.15}] [all_outputs]

set_input_transition 0.05 $data_inputs
set_load             0.02 [all_outputs]

set_max_fanout    20  [current_design]
set_max_transition 0.5 [current_design]

# `reset` is an active-high asynchronous reset. Keep it out of the timing
# picture so the reported critical path is the datapath, not reset recovery.
set_false_path -from [get_ports $RST_PORT]
