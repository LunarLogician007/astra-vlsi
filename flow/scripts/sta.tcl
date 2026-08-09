# ---------------------------------------------------------------------------
# Post-synthesis static timing analysis.
#   sta -exit flow/scripts/sta.tcl        (or: openroad -no_init -exit ...)
#
# Inputs (environment): ASTRA_FLOW, ASTRA_PDK_CONFIG, ASTRA_TOP, ASTRA_NETLIST,
#                       ASTRA_SDC, ASTRA_NPATHS, ASTRA_TAG
#
# Note on interpretation: there are no parasitics at this stage, so slack here
# is optimistic relative to post-route. It is still the right signal for the
# RTL optimisation loop — it is fast and it isolates *logic depth* from
# placement effects.
# ---------------------------------------------------------------------------
source [file join $::env(ASTRA_FLOW) config common.tcl]
source $::env(ASTRA_PDK_CONFIG)
source [file join $::env(ASTRA_FLOW) scripts sta_common.tcl]

set top     $::env(ASTRA_TOP)
set netlist $::env(ASTRA_NETLIST)
set sdc     $::env(ASTRA_SDC)
set npaths  [astra_env ASTRA_NPATHS 20]
set tag     [astra_env ASTRA_TAG post_synth]

foreach lib $LIB_FILES {
    read_liberty $lib
}
read_verilog $netlist
link_design $top
read_sdc $sdc

puts "###ASTRA_BEGIN ${tag}_units"
catch { report_units }
puts "###ASTRA_END ${tag}_units"

astra_timing_report $tag $npaths

astra_kv "$tag.pdk" $PDK_NAME
astra_kv "$tag.netlist" $netlist
astra_kv "$tag.sdc" $sdc
