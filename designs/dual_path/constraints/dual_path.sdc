# ---------------------------------------------------------------------------
# dual_path timing constraints
#
# @CLK_PERIOD@ is substituted by tools/astra.py, so `astra syn dual_path --period 1.0`
# tightens the SDC and abc's delay target together. Edit the placeholder out
# and hard-code a number if you would rather pin it.
# ---------------------------------------------------------------------------
set CLK_PERIOD @CLK_PERIOD@
set CLK_PORT   clk

create_clock -name clk -period $CLK_PERIOD [get_ports $CLK_PORT]

# Modest margin so post-synthesis numbers are not wildly optimistic.
set_clock_uncertainty 0.05 [get_clocks clk]
set_clock_transition  0.05 [get_clocks clk]

# OpenSTA has no `remove_from_collection` (that is a Synopsys command), so
# drop the clock port from the input list by name.
set inputs_no_clk {}
foreach p [all_inputs] {
    if {[get_full_name $p] ne $CLK_PORT} { lappend inputs_no_clk $p }
}

set_input_delay  -clock clk [expr {$CLK_PERIOD * 0.20}] $inputs_no_clk
set_output_delay -clock clk [expr {$CLK_PERIOD * 0.20}] [all_outputs]

# PDK-neutral drive/load model: no library cell names, so the same SDC works
# on nangate45 and sky130hd.
set_input_transition 0.05 $inputs_no_clk
set_load             0.02 [all_outputs]

set_max_fanout    20  [current_design]
set_max_transition 0.5 [current_design]
