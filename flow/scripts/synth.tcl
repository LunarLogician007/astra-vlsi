# ---------------------------------------------------------------------------
# Yosys logic synthesis  (run with: yosys -c flow/scripts/synth.tcl)
#
# Inputs  (environment, set by tools/astra.py):
#   ASTRA_FLOW        flow/ directory
#   ASTRA_PDK_CONFIG  path to flow/config/<pdk>.tcl
#   ASTRA_TOP         top module name
#   ASTRA_RTL_FILES   space-separated Verilog/SystemVerilog sources
#   ASTRA_OUT_DIR     output directory (01_synth)
#   ASTRA_CLK_PERIOD  target clock period in ns (drives abc's delay target)
#   ASTRA_FLATTEN     1 to flatten the hierarchy before mapping
#
# Outputs: netlist.v, netlist.json, synth_stat.txt, and ASTRA_KV lines on
# stdout that tools/astra.py folds into metrics.json.
# ---------------------------------------------------------------------------
source [file join $::env(ASTRA_FLOW) config common.tcl]
source $::env(ASTRA_PDK_CONFIG)

set top       $::env(ASTRA_TOP)
set out_dir   $::env(ASTRA_OUT_DIR)
set rtl_files $::env(ASTRA_RTL_FILES)
set period_ns [astra_env ASTRA_CLK_PERIOD 10.0]
set flatten   [astra_env ASTRA_FLATTEN 1]

# abc takes its delay target in picoseconds.
set period_ps [expr {int($period_ns * 1000)}]

puts "###ASTRA_BEGIN synth_config"
puts "  pdk     : $PDK_NAME"
puts "  liberty : $LIB_FILE"
puts "  top     : $top"
puts "  period  : $period_ns ns ($period_ps ps target for abc)"
puts "  sources : $rtl_files"
puts "###ASTRA_END synth_config"

# --- read ------------------------------------------------------------------
foreach f $rtl_files {
    yosys read_verilog -sv -defer $f
}
yosys hierarchy -check -top $top

# --- generic synthesis -----------------------------------------------------
yosys proc
yosys opt -full
yosys fsm
yosys opt -full
yosys memory_map
yosys opt -full
yosys techmap
yosys opt -full

if {$flatten} {
    yosys flatten
    yosys opt -full
}

# --- technology mapping ----------------------------------------------------
yosys dfflibmap -liberty $LIB_FILE
# -D gives abc a delay target so it optimises for timing, not just area.
yosys abc -liberty $LIB_FILE -D $period_ps
yosys setundef -zero
yosys splitnets -ports
yosys opt_clean -purge

# --- write -----------------------------------------------------------------
file mkdir $out_dir
yosys write_verilog -noattr -noexpr -nohex -nodec [file join $out_dir netlist.v]
yosys write_json [file join $out_dir netlist.json]

puts "###ASTRA_BEGIN synth_stat"
yosys tee -o [file join $out_dir synth_stat.txt] stat -liberty $LIB_FILE
yosys stat -liberty $LIB_FILE
puts "###ASTRA_END synth_stat"

yosys check -mapped

puts "ASTRA_KV synth.netlist [file join $out_dir netlist.v]"
puts "ASTRA_KV synth.liberty $LIB_FILE"
puts "ASTRA_KV synth.pdk $PDK_NAME"
