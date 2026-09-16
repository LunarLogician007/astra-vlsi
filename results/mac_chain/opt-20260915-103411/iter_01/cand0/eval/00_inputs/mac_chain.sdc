# ---------------------------------------------------------------------------
# mac_chain timing constraints
#
# The default period (2.0 ns on nangate45) is intentionally tighter than the
# design can meet — a 16x16 multiplier plus a 3-deep adder chain plus a 40-bit
# saturation compare does not fit. That negative slack is the starting point
# for the optimisation loop.
# ---------------------------------------------------------------------------
set CLK_PERIOD 2
set CLK_PORT   clk

create_clock -name clk -period $CLK_PERIOD [get_ports $CLK_PORT]

set_clock_uncertainty 0.05 [get_clocks clk]
set_clock_transition  0.05 [get_clocks clk]

# OpenSTA has no `remove_from_collection` (that is a Synopsys command), so
# drop the clock port from the input list by name.
set inputs_no_clk {}
foreach p [all_inputs] {
    if {[get_full_name $p] ne $CLK_PORT} { lappend inputs_no_clk $p }
}

set_input_delay  -clock clk [expr {$CLK_PERIOD * 0.15}] $inputs_no_clk
set_output_delay -clock clk [expr {$CLK_PERIOD * 0.15}] [all_outputs]

set_input_transition 0.05 $inputs_no_clk
set_load             0.02 [all_outputs]

set_max_fanout    20  [current_design]
set_max_transition 0.5 [current_design]

# Reset is asynchronous to nothing in particular here; keep it out of the
# datapath timing picture so the reported critical path is the MAC itself.
set_false_path -from [get_ports rst_n]
